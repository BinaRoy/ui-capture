"""HarmonyOS / OpenHarmony adapter using `hdc` + `uitest dumpLayout`.

Pipeline mirrors the Android adapter but targets ArkUI / Cangjie projects.

Capture:
  - screenshot: `hdc shell snapshot_display -f /data/local/tmp/<f>.png`
  - hierarchy: `hdc shell uitest dumpLayout` → JSON on device → `hdc file recv`
  - normalize: JSON → UI-IR matching schema/hierarchy.schema.json
  - scroll stitching: `uitest uiInput swipe` anchor-based (mirrors Android adapter)

discover_hints scans Cangjie source for:
  1. module.json5 abilities
  2. @Entry annotated classes
  3. @Builder pageMap if/else chains → all NavDestination pages (ArkUI nav pattern)

This is B.1 stage 1 — uses uitest dumpLayout (basic fields). B.3 will upgrade
to `hidumper + ArkUI debug` for visual properties (color / font / margin).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from .base import (
    Adapter, AdapterError, CaptureResult, PageHint, PageIdentity,
)


# --------------------------------------------------------------- locate hdc

def _find_hdc() -> Optional[str]:
    """Find an `hdc` executable. Search PATH first, then known DevEco Studio
    install locations. Result cached at instance level (see __init__)."""
    via_path = shutil.which("hdc")
    if via_path:
        return via_path
    candidates = [
        "/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/toolchains/hdc",
        os.path.expanduser("~/Library/Huawei/Sdk/openharmony/*/toolchains/hdc"),
        "C:\\Program Files\\Huawei\\DevEco Studio\\sdk\\default\\openharmony\\toolchains\\hdc.exe",
    ]
    for c in candidates:
        if "*" in c:
            import glob
            for found in sorted(glob.glob(c), reverse=True):
                if os.access(found, os.X_OK):
                    return found
            continue
        if Path(c).exists() and os.access(c, os.X_OK):
            return c
    return None


# ----------------------------------------------------------- type → kind map

_TYPE_TO_KIND = {
    # ArkUI containers
    "Column":            "linear",
    "Row":               "linear",
    "Stack":             "frame",
    "Flex":              "linear",
    "RelativeContainer": "relative",
    "WindowScene":       "frame",
    "EffectComponent":   "frame",
    "__Common__":        "frame",
    # Grid family
    "Grid":              "list",
    "GridItem":          "view",
    "GridRow":           "linear",
    "GridCol":           "view",
    # List family
    "List":              "list",
    "ListItem":          "view",
    "ListItemGroup":     "view",
    # Pager-like
    "Swiper":            "pager",
    "Tabs":              "tabs",
    "TabContent":        "view",
    "TabBar":            "tabs",
    # Scroll family
    "Scroll":            "scroll",
    "Refresh":           "scroll",
    "WaterFlow":         "scroll",
    # Leaf widgets
    "Text":              "text",
    "Span":              "text",
    "TextClock":         "text",
    "TextTimer":         "text",
    "Image":             "image",
    "ImageAnimator":     "image",
    "ImageSpan":         "image",
    "Button":            "button",
    "TextInput":         "input",
    "TextArea":          "input",
    "Search":            "input",
    "Toggle":            "switch",
    "Checkbox":          "checkbox",
    "CheckboxGroup":     "checkbox",
    "Radio":             "radio",
    "Slider":            "slider",
    "Progress":          "progress",
    "Rating":            "slider",
    "Web":               "web",
    "Video":             "media",
    "XComponent":        "media",
    "RichEditor":        "input",
    # Navigation chrome
    "Navigation":        "frame",
    "NavRouter":         "view",
    "NavDestination":    "view",
}


def _type_to_kind(t: Optional[str]) -> str:
    if not t:
        return "view"
    return _TYPE_TO_KIND.get(t, "view")


# ------------------------------------------------- Cangjie source helpers

# Matches: if (name == "PageName") or else if (name == "PageName")
_PAGE_MAP_RE = re.compile(r'if\s*\(name\s*==\s*"(\w+)"\)')
_PUSH_PATH_RE = re.compile(r'pageStack\.pushPathByName\s*\(\s*"(\w+)"')
_CLASS_DEF_RE = re.compile(r'\bclass\s+(\w+)\b')
# Conservative: only match component-looking suffixes to limit false positives.
_COND_CHILD_RE = re.compile(r'\b([A-Z]\w*(?:Page|View|Screen|Tab|Sheet))\s*\(')

# Matches: class PageName (first occurrence in a .cj file)
def _find_class_file(class_name: str, source_root: Path) -> Optional[str]:
    """Return source-root-relative path of the .cj file declaring `class_name`."""
    pat = re.compile(rf"\bclass\s+{re.escape(class_name)}\b")
    _SKIP = {"build", "node_modules", ".hvigor", "oh_modules"}
    for path in sorted(source_root.rglob("*.cj")):
        if any(part in _SKIP for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if pat.search(text):
            try:
                return path.resolve().relative_to(source_root.resolve()).as_posix()
            except ValueError:
                return path.as_posix()
    return None


# ----------------------------------------------------------- bounds parse

_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


def _parse_bounds(s: str) -> Optional[list[int]]:
    if not s:
        return None
    m = _BOUNDS_RE.match(s.strip())
    if not m:
        return None
    return [int(m.group(i)) for i in (1, 2, 3, 4)]


def _bool_attr(v) -> Optional[bool]:
    """uitest dumpLayout encodes booleans as 'true' / 'false' / '' strings."""
    if isinstance(v, bool):
        return v
    if v == "true":
        return True
    if v == "false":
        return False
    return None


# ------------------------------------------------------------------ adapter

class HarmonyAdapter(Adapter):
    platform = "harmony"

    # capture stability — match Android adapter's polling cadence so async
    # ArkUI data loads have a chance to settle before we dump.
    STABLE_MAX_WAIT_S = 10.0
    STABLE_SETTLE_REPEATS = 2
    STABLE_POLL_INTERVAL_S = 0.5

    # Scroll stitching — mirrors Android adapter's anchor-based algorithm.
    # `uitest uiInput swipe x1 y1 x2 y2 speed` where speed is px/sec.
    SCROLL_MAX_STEPS = 10
    SCROLL_MIN_DELTA_PX = 20
    SCROLL_SWIPE_SPEED = 1200   # px/sec — moderate pace for uitest stability
    SCROLL_SETTLE_S = 0.8

    def __init__(self) -> None:
        self.hdc = _find_hdc()
        # Default device id — single-target case. If multiple devices, caller
        # can set HMOS_DEVICE_ID env var (similar to Android's ANDROID_SERIAL).
        self.device = os.environ.get("HMOS_DEVICE_ID") or ""

    # ---------------------------------------------------- infrastructure

    def check_infrastructure(self) -> tuple[bool, str]:
        if not self.hdc:
            return (False, "hdc not found. Install DevEco Studio or put hdc on PATH.")
        try:
            r = self._run_hdc(["list", "targets"], timeout=10)
        except subprocess.TimeoutExpired:
            return (False, "hdc timed out — daemon may be stuck. Try `hdc kill && hdc start`.")
        if r.returncode != 0:
            return (False, f"hdc list targets failed: {r.stderr.strip() or r.stdout.strip()}")
        targets = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
        # hdc prints "[Empty]" literally when there's no device.
        targets = [t for t in targets if t != "[Empty]"]
        if not targets:
            return (False, "no HarmonyOS device/emulator connected. "
                           "Start a DevEco emulator, then re-run.")
        if not self.device:
            self.device = targets[0]
        return (True, f"hdc OK; target={self.device}")

    # ---------------------------------------------------- discover_hints

    def discover_hints(self, source_root: Path) -> list[PageHint]:
        """Scan Cangjie source files for `@Entry` / `@Component` annotated
        classes, plus look at module.json5 for declared abilities.

        ArkUI/Cangjie convention: a `@Entry @Component` class defines a route
        page; `@Component` classes are reusable widgets. We treat @Entry as
        page candidates; @Component is too noisy (every reusable block).
        """
        hints: dict[str, PageHint] = {}

        # Strategy 1: module.json5 abilities — top-level pages exposed by the
        # bundle. Tolerant of single-quoted JSON5 / trailing commas.
        for mj in source_root.rglob("module.json5"):
            try:
                text = mj.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for m in re.finditer(r'"?name"?\s*:\s*"([\w.]+Ability)"', text):
                name = m.group(1)
                short = name.rsplit(".", 1)[-1]
                hints.setdefault(short, PageHint(
                    slug=self._slugify(short),
                    source_file=self._rel(mj, source_root),
                    class_name=short,
                    origin="module_json",
                    note=f"ability declared in {mj.name}",
                ))

        # Load all .cj source text once — reused by strategies 2/3/4.
        _SKIP = {"build", "node_modules", ".hvigor", "oh_modules"}
        cj_files: list[tuple[Path, str]] = []
        for path in source_root.rglob("*.cj"):
            if any(part in _SKIP for part in path.parts):
                continue
            try:
                cj_files.append((path, path.read_text(encoding="utf-8", errors="ignore")))
            except OSError:
                continue

        # Pre-index: every class definition, every page name that gets pushed,
        # and where each *Page-suffixed class gets directly instantiated.
        # Drives orphan-detection (Layer 1 ∩ ¬Layer 3 ∩ ¬instantiation) and
        # conditional-render detection (instantiated inline somewhere).
        pushed_names: set[str] = set()
        class_names: set[str] = set()
        entry_pat = re.compile(
            r"@Entry\b[^{}]*?class\s+(\w+)",
            re.MULTILINE | re.DOTALL,
        )
        instantiated_at: dict[str, Path] = {}  # name → first file that calls Name()
        for path, text in cj_files:
            pushed_names.update(_PUSH_PATH_RE.findall(text))
            class_names.update(_CLASS_DEF_RE.findall(text))
            for m in _COND_CHILD_RE.finditer(text):
                name = m.group(1)
                instantiated_at.setdefault(name, path)

        # Strategy 2: @Entry annotated Cangjie classes — these are routable pages.
        for path, text in cj_files:
            for m in entry_pat.finditer(text):
                cls = m.group(1)
                hints.setdefault(cls, PageHint(
                    slug=self._slugify(cls),
                    source_file=self._rel(path, source_root),
                    class_name=cls,
                    origin="entry_annotation",
                    note="@Entry @Component (Cangjie)",
                ))

        # Strategy 3: @Builder func pageMap — ArkUI NavDestination pattern.
        # Classify each pageMap page by reachability:
        #   - pushed via pageStack.pushPathByName       → nav_destination
        #   - not pushed, but instantiated inline       → conditional_render
        #   - neither                                   → orphan
        for path, text in cj_files:
            if "@Builder" not in text or "pageMap" not in text:
                continue
            for m in _PAGE_MAP_RE.finditer(text):
                page_name = m.group(1)
                if page_name in hints:
                    continue
                class_file = _find_class_file(page_name, source_root)
                if page_name in pushed_names:
                    origin = "nav_destination"
                    note = f"NavDestination page from pageMap in {path.name}"
                elif page_name in instantiated_at:
                    origin = "conditional_render"
                    note = f"pageMap entry rendered inline at {instantiated_at[page_name].name}"
                else:
                    origin = "orphan"
                    note = f"pageMap entry with no pushPathByName / instantiation (orphan); declared in {path.name}"
                hints[page_name] = PageHint(
                    slug=self._slugify(page_name),
                    source_file=class_file or self._rel(path, source_root),
                    class_name=page_name,
                    origin=origin,
                    note=note,
                )

        # Strategy 4: page-suffix classes instantiated inline but NOT in pageMap.
        # Picks up sub-pages that bypass NavPathStack entirely (e.g. inline
        # state-driven swaps inside another @Component).
        for name, path in instantiated_at.items():
            if name in hints:
                continue
            if name not in class_names:
                continue
            class_file = _find_class_file(name, source_root)
            hints[name] = PageHint(
                slug=self._slugify(name),
                source_file=class_file or self._rel(path, source_root),
                class_name=name,
                origin="conditional_render",
                note=f"page-suffix class instantiated inline at {path.name} (not in pageMap)",
            )

        return sorted(hints.values(), key=lambda h: (h.source_file, h.class_name))

    # ---------------------------------------------------- nav script

    def render_nav_script(self, hints: list[PageHint], out_path: Path) -> None:
        if out_path.exists():
            return  # never overwrite human-edited script
        out_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "#!/usr/bin/env bash",
            "# HarmonyOS nav script — fill in tap coordinates manually using uitest layout.",
            "# Convention: emit `capture_page <slug>` markers; the orchestrator reads them.",
            "",
            'set -euo pipefail',
            'HDC="${HDC:-hdc}"',
            "BUNDLE='<your.bundle.name>'  # TODO: set to module.json5 bundleName",
            "ABILITY='EntryAbility'        # TODO: set to your main ability",
            "",
            "# Cold-start the app so each run starts from the launcher, not from",
            "# whatever NavDestination was on top last time. Skip this only if",
            "# your project has no nav stack (e.g. a single-page app).",
            '"$HDC" shell aa force-stop "$BUNDLE" 2>/dev/null || true',
            "sleep 1",
            "$HDC shell aa start -a \"$ABILITY\" -b \"$BUNDLE\"",
            "sleep 4",
            "",
        ]
        for h in hints:
            lines.append(f"# {h.class_name} — {h.origin}: {h.source_file}")
            if "Ability" in h.class_name:
                lines.append(
                    f"# Launcher ability — already shown after `aa start`. "
                    f"If this is the default screen, just emit capture marker:")
                lines.append(f"echo 'capture_page {h.slug}'")
            else:
                lines.append("# TODO: tap into this page. Get coordinates from `hdc shell uitest dumpLayout`.")
                lines.append(f"# $HDC shell uitest uiInput click <x> <y>")
                lines.append("# sleep 2")
                lines.append(f"echo 'capture_page {h.slug}'")
                lines.append("# $HDC shell uitest uiInput keyEvent 2  # back")
                lines.append("# sleep 2")
            lines.append("")
        out_path.write_text("\n".join(lines), encoding="utf-8")
        try:
            out_path.chmod(0o755)
        except OSError:
            pass

    # ---------------------------------------------------- probe_identity

    def probe_identity(self) -> PageIdentity:
        """Best-effort identity: top window's ability + page path from a quick dump."""
        try:
            dump = self._dump_layout_text()
            data = json.loads(dump) if dump else {}
        except (subprocess.TimeoutExpired, json.JSONDecodeError):
            return PageIdentity(top_component="unknown")
        # Walk the tree to find the first node with abilityName / bundleName set
        bundle = ability = page_path = ""
        def walk(n):
            nonlocal bundle, ability, page_path
            if not isinstance(n, dict):
                return
            a = n.get("attributes") or {}
            if not ability and a.get("abilityName"):
                ability = a["abilityName"]
            if not bundle and a.get("bundleName"):
                bundle = a["bundleName"]
            if not page_path and a.get("pagePath"):
                page_path = a["pagePath"]
            for c in n.get("children") or []:
                walk(c)
        walk(data)
        return PageIdentity(
            top_component=ability or "unknown",
            current_fragment=(page_path or None),
            extra={"bundle": bundle, "page_path": page_path},
        )

    # ----------------------------------------------------------- capture

    def capture(self, page_id: str, out_dir: Path) -> CaptureResult:
        t0 = time.time()
        out_dir.mkdir(parents=True, exist_ok=True)

        # Wait for the page to stabilize (same model as Android adapter).
        status, polls, elapsed_ms = self._wait_until_stable()
        print(f"[harmony] {page_id} stability={status} elapsed={elapsed_ms}ms polls={polls}")

        identity = self.probe_identity()

        # Screenshot
        screenshot_path = out_dir / "screenshot.png"
        ok = self._snapshot_display(screenshot_path)
        if not ok:
            return CaptureResult(
                status="failed", identity=identity, raw_path=None,
                screenshot_path=None, hierarchy=None,
                error="snapshot_display failed",
                duration_ms=int((time.time() - t0) * 1000),
            )

        # Hierarchy dump
        raw_path = out_dir / "raw.json"
        dump_text = self._dump_layout_text()
        if not dump_text:
            return CaptureResult(
                status="failed", identity=identity, raw_path=None,
                screenshot_path=screenshot_path, hierarchy=None,
                error="uitest dumpLayout failed or empty",
                duration_ms=int((time.time() - t0) * 1000),
            )
        raw_path.write_text(dump_text, encoding="utf-8")

        try:
            hierarchy = self.normalize(raw_path)
        except Exception as exc:
            return CaptureResult(
                status="failed", identity=identity, raw_path=raw_path,
                screenshot_path=screenshot_path, hierarchy=None,
                error=f"normalize failed: {exc}",
                duration_ms=int((time.time() - t0) * 1000),
            )

        # Attempt scroll stitching if page has scrollable content
        if self._has_scrollable(hierarchy):
            fullpage = self._capture_fullpage(page_id, out_dir, screenshot_path, hierarchy)
            if fullpage is not None:
                hierarchy = fullpage

        return CaptureResult(
            status="captured",
            identity=identity,
            raw_path=raw_path,
            screenshot_path=screenshot_path,
            hierarchy=hierarchy,
            duration_ms=int((time.time() - t0) * 1000),
        )

    # --------------------------------------------------------- normalize

    def normalize(self, raw_path: Path) -> dict:
        """ArkUI uitest dumpLayout JSON → UI-IR. Matches schema/hierarchy.schema.json."""
        data = json.loads(raw_path.read_text(encoding="utf-8"))

        children = data.get("children") or []
        # Some dumps put the real tree under "children" of a synthetic root with
        # no attributes — keep both shapes working by detecting and unwrapping.
        if not children and data.get("attributes"):
            children = [data]

        return {
            "kind": "root",
            "platform": "harmony",
            "children": [self._node_to_ir(c) for c in children],
        }

    def _node_to_ir(self, node: dict) -> dict:
        a = node.get("attributes") or {}
        out: dict = {"kind": _type_to_kind(a.get("type"))}
        if a.get("type"):
            out["class"] = a["type"]
        # id / accessibilityId / key are all stable handles; prefer id if set
        nid = a.get("id") or a.get("accessibilityId") or a.get("key")
        if nid:
            out["id"] = nid
        text = a.get("text") or a.get("originalText") or ""
        if text:
            out["text"] = text
        desc = a.get("description")
        if desc:
            out["content_desc"] = desc
        bounds = _parse_bounds(a.get("bounds", ""))
        if bounds:
            out["bounds"] = bounds

        # Structured state — schema-aligned. Keep both true and false so diff
        # can detect transitions (per A.1 schema).
        state_map = {
            "enabled":        _bool_attr(a.get("enabled")),
            "checked":        _bool_attr(a.get("checked")),
            "checkable":      _bool_attr(a.get("checkable")),
            "clickable":      _bool_attr(a.get("clickable")),
            "long_clickable": _bool_attr(a.get("longClickable")),
            "focused":        _bool_attr(a.get("focused")),
            "selected":       _bool_attr(a.get("selected")),
            "scrollable":     _bool_attr(a.get("scrollable")),
        }
        state = {k: v for k, v in state_map.items() if v is not None}
        if state:
            out["state"] = state

        # Legacy flags array — match Android adapter's convention for downstream
        # consumers that still scan `flags` rather than `state`.
        for k, v in state.items():
            if v is True:
                out.setdefault("flags", []).append(k.replace("_", "-"))

        # Stage-1 visual properties — uitest dumpLayout exposes a small subset.
        # Schema-aligned `style` block; richer properties come from hidumper (B.3).
        style: dict = {}
        if a.get("backgroundColor"):
            style["background"] = a["backgroundColor"]
        if a.get("opacity") not in (None, "", "1", "1.0"):
            try:
                style["opacity"] = float(a["opacity"])
            except ValueError:
                pass
        if style:
            out["style"] = style

        children = node.get("children") or []
        if children:
            out["children"] = [self._node_to_ir(c) for c in children]
        return out

    # ------------------------------------------------- scroll stitching

    # Kinds that scroll vertically and warrant fullpage stitching.
    # Excluded: pager (Swiper — horizontal), tabs (Tabs — horizontal).
    _VERTICAL_SCROLL_KINDS = frozenset({"scroll", "list"})

    def _has_scrollable(self, hierarchy: dict) -> bool:
        """True if any vertical-scroll container exists (Scroll / List kinds)."""
        def walk(n: dict) -> bool:
            if n.get("kind") in self._VERTICAL_SCROLL_KINDS:
                if isinstance(n.get("state"), dict) and n["state"].get("scrollable"):
                    return True
            return any(walk(c) for c in n.get("children") or [])
        return any(walk(c) for c in hierarchy.get("children") or [])

    def _anchor_key(self, n: dict) -> Optional[str]:
        nid = n.get("id") or ""
        text = (n.get("text") or "").strip()
        cls = n.get("class", "")
        if nid:
            return f"id:{nid}|{cls}"
        if len(text) >= 3:
            return f"tx:{text}|{cls}"
        return None

    def _anchor_map(self, hierarchy: dict) -> dict[str, int]:
        """Map stable node key → y-midpoint. Used to measure scroll delta."""
        out: dict[str, int] = {}
        def walk(n: dict) -> None:
            key = self._anchor_key(n)
            b = n.get("bounds")
            if key and b and len(b) == 4:
                out[key] = (b[1] + b[3]) // 2
            for c in n.get("children") or []:
                walk(c)
        for c in hierarchy.get("children") or []:
            walk(c)
        return out

    @staticmethod
    def _median_anchor_delta(prev: dict[str, int], curr: dict[str, int]) -> Optional[int]:
        """Positive delta = content moved up (page scrolled down)."""
        common = set(prev) & set(curr)
        if not common:
            return None
        deltas = sorted(prev[k] - curr[k] for k in common)
        # Filter near-zero (sticky elements) before taking median
        moving = [d for d in deltas if abs(d) > 2]
        if not moving:
            return 0
        return moving[len(moving) // 2]

    def _translate_subtree(self, node: dict, offset: int) -> dict:
        new = {k: v for k, v in node.items() if k != "children"}
        b = node.get("bounds")
        if b and isinstance(b, list) and len(b) == 4:
            new["bounds"] = [b[0], b[1] + offset, b[2], b[3] + offset]
        new["children"] = [self._translate_subtree(c, offset) for c in node.get("children") or []]
        return new

    def _node_stable_key(self, n: dict) -> Optional[str]:
        return self._anchor_key(n)

    def _collect_keys(self, node: dict, out: set) -> None:
        k = self._node_stable_key(node)
        if k:
            out.add(k)
        for c in node.get("children") or []:
            self._collect_keys(c, out)

    def _collect_translated_new(self, node: dict, offset: int,
                                seen: set, out: list) -> None:
        k = self._node_stable_key(node)
        if k and k not in seen:
            translated = self._translate_subtree(node, offset)
            self._collect_keys(translated, seen)
            out.append(translated)
            return
        for c in node.get("children") or []:
            self._collect_translated_new(c, offset, seen, out)

    def _stitch_images(self, image_paths: list[Path], offsets: list[int],
                       viewport_h: int) -> "Image":
        from PIL import Image as PILImage
        total_h = viewport_h + (offsets[-1] if offsets else 0)
        first = PILImage.open(image_paths[0])
        w = first.width
        canvas = PILImage.new("RGB", (w, total_h), (255, 255, 255))
        canvas.paste(first, (0, 0))
        for img_path, offset in zip(image_paths[1:], offsets[1:]):
            img = PILImage.open(img_path)
            canvas.paste(img, (0, offset))
        return canvas

    def _capture_fullpage(self, page_id: str, out_dir: Path,
                          viewport_shot: Path, base_hierarchy: dict) -> Optional[dict]:
        """Anchor-based scroll stitching using uitest uiInput swipe.

        Returns the unified hierarchy dict (with translated absolute bounds) or
        None if stitching fails (caller falls back to single-viewport result).
        """
        try:
            from PIL import Image as PILImage  # noqa: F401
        except ImportError:
            return None  # Pillow not available — skip stitching

        # Derive viewport dimensions from first screenshot
        try:
            from PIL import Image as PILImage
            img0 = PILImage.open(viewport_shot)
            viewport_w, viewport_h = img0.size
        except Exception:
            return None

        scroll_dir = out_dir / "scroll"
        scroll_dir.mkdir(exist_ok=True)

        # Step 0 — initial viewport (symlink to viewport screenshot)
        step0_link = scroll_dir / "00.png"
        try:
            step0_link.symlink_to(Path("..") / viewport_shot.name)
        except (OSError, NotImplementedError):
            step0_link.write_bytes(viewport_shot.read_bytes())

        import copy
        steps: list[dict] = [{"offset": 0, "hierarchy": base_hierarchy,
                               "image_path": step0_link}]
        cumulative = 0

        swipe_x = viewport_w // 2
        swipe_from_y = int(viewport_h * 0.80)
        swipe_to_y   = int(viewport_h * 0.20)

        anchors_prev = self._anchor_map(base_hierarchy)
        termination_reason = "max_steps_reached"

        for step in range(1, self.SCROLL_MAX_STEPS + 1):
            r = self._run_hdc([
                "shell", "uitest", "uiInput", "swipe",
                str(swipe_x), str(swipe_from_y),
                str(swipe_x), str(swipe_to_y),
                str(self.SCROLL_SWIPE_SPEED),
            ], timeout=20)
            if r.returncode != 0:
                termination_reason = "swipe_failed"
                break
            time.sleep(self.SCROLL_SETTLE_S)

            # Fresh dump after swipe
            dump_text = self._dump_layout_text()
            if not dump_text:
                termination_reason = "dump_failed"
                break
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".json", delete=False,
                                             mode="w", encoding="utf-8") as tf:
                tf.write(dump_text)
                tmp = Path(tf.name)
            try:
                step_h = self.normalize(tmp)
            except Exception:
                tmp.unlink(missing_ok=True)
                termination_reason = "normalize_failed"
                break
            finally:
                tmp.unlink(missing_ok=True)

            # Raw dump saved for provenance
            (scroll_dir / f"{step:02d}.json").write_text(dump_text, encoding="utf-8")

            # Screenshot after swipe
            ok = self._snapshot_display(scroll_dir / f"{step:02d}.png")
            if not ok:
                termination_reason = "screencap_failed"
                break

            anchors_now = self._anchor_map(step_h)
            delta = self._median_anchor_delta(anchors_prev, anchors_now)

            # Overlap check — if >90% anchors still present and delta < 10% viewport,
            # we've hit the page bottom.
            common = set(anchors_prev) & set(anchors_now)
            overlap_ratio = (len(common) / len(anchors_prev)) if anchors_prev else 0.0

            if delta is None:
                termination_reason = "all_anchors_lost"
                (scroll_dir / f"{step:02d}.png").unlink(missing_ok=True)
                (scroll_dir / f"{step:02d}.json").unlink(missing_ok=True)
                break
            if delta < self.SCROLL_MIN_DELTA_PX:
                termination_reason = "delta_below_threshold"
                (scroll_dir / f"{step:02d}.png").unlink(missing_ok=True)
                (scroll_dir / f"{step:02d}.json").unlink(missing_ok=True)
                break
            if overlap_ratio > 0.9 and delta < viewport_h * 0.1:
                termination_reason = "high_overlap_bottom_reached"
                (scroll_dir / f"{step:02d}.png").unlink(missing_ok=True)
                (scroll_dir / f"{step:02d}.json").unlink(missing_ok=True)
                break

            cumulative += delta
            steps.append({"offset": cumulative, "hierarchy": step_h,
                           "image_path": scroll_dir / f"{step:02d}.png"})
            anchors_prev = anchors_now

        if len(steps) < 2:
            return None  # no extra content revealed — single viewport is sufficient

        # Stitch screenshot
        page_total_height = viewport_h + steps[-1]["offset"]
        try:
            stitched = self._stitch_images(
                [s["image_path"] for s in steps],
                [s["offset"] for s in steps],
                viewport_h,
            )
            fullpage_path = out_dir / "screenshot_fullpage.png"
            stitched.save(fullpage_path, format="PNG")
        except Exception:
            pass  # stitching failed — hierarchy still unified below

        # Merge hierarchies
        import copy
        merged = copy.deepcopy(base_hierarchy)
        seen_keys: set = set()
        self._collect_keys(merged, seen_keys)

        for step in steps[1:]:
            offset = step["offset"]
            new_nodes: list = []
            self._collect_translated_new(step["hierarchy"], offset, seen_keys, new_nodes)
            if new_nodes:
                # Attach to deepest scrollable container, or root children
                scroll_target = self._find_scroll_target(merged) or merged
                scroll_target.setdefault("children", []).extend(new_nodes)

        merged["scrolled"] = True
        merged["scroll_steps"] = len(steps) - 1
        merged["viewport_height"] = viewport_h
        merged["page_total_height"] = page_total_height
        merged["termination_reason"] = termination_reason

        # Restore page to top so subsequent nav-script taps land on the same
        # coordinates that were valid when the user wrote the script (launcher
        # view). Without this, fullpage stitching leaves the page scrolled
        # down and the next tap hits whatever happens to be at (x,y) in the
        # scrolled state — typically a weather metric tile, not the TitleBar
        # icon the script intended.
        # Swipe the inverse direction (top 20% → bottom 80%) once per scroll
        # step we issued, plus one extra for safety.
        swipe_back_y_from = int(viewport_h * 0.20)
        swipe_back_y_to   = int(viewport_h * 0.80)
        for _ in range(len(steps) - 1 + 1):  # scroll_steps + 1 safety
            self._run_hdc([
                "shell", "uitest", "uiInput", "swipe",
                str(swipe_x), str(swipe_back_y_from),
                str(swipe_x), str(swipe_back_y_to),
                str(self.SCROLL_SWIPE_SPEED),
            ], timeout=20)
            time.sleep(self.SCROLL_SETTLE_S)
        return merged

    def _find_scroll_target(self, hierarchy: dict) -> Optional[dict]:
        """Return the deepest scrollable container node, or None."""
        best: list = []
        def walk(n: dict, depth: int) -> None:
            if isinstance(n.get("state"), dict) and n["state"].get("scrollable"):
                best.append((depth, n))
            for c in n.get("children") or []:
                walk(c, depth + 1)
        for c in hierarchy.get("children") or []:
            walk(c, 0)
        if not best:
            return None
        best.sort(key=lambda x: x[0], reverse=True)
        return best[0][1]

    # --------------------------------------------------------- internals

    def _run_hdc(self, args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess:
        cmd = [self.hdc]
        if self.device:
            cmd += ["-t", self.device]
        cmd += args
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    def _dump_layout_text(self) -> str:
        """Run `hdc shell uitest dumpLayout`, recv the result file, return JSON text.

        uitest prints `DumpLayout saved to:/data/local/tmp/<file>.json` to stdout —
        we parse the path out and pull the file off the device. Cheaper than
        piping through `cat`, and gives us a stable JSON without escaping.
        """
        r = self._run_hdc(["shell", "uitest", "dumpLayout"], timeout=30)
        if r.returncode != 0:
            return ""
        m = re.search(r"saved to:\s*(\S+\.json)", r.stdout)
        if not m:
            return ""
        device_path = m.group(1)
        # `hdc file recv` to a tempfile, read, then leave it (cleanup is cheap).
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
            local = Path(tf.name)
        pull = self._run_hdc(["file", "recv", device_path, str(local)], timeout=20)
        if pull.returncode != 0 or not local.exists():
            return ""
        try:
            return local.read_text(encoding="utf-8")
        finally:
            try:
                local.unlink()
            except OSError:
                pass

    def _snapshot_display(self, out_path: Path) -> bool:
        """snapshot_display on HarmonyOS only writes .jpeg, not .png. We always
        recv to a .jpeg tempfile then move to the caller's out_path (which by
        convention is named screenshot.png to match other adapters). The file
        contents are JPEG; consumers reading the bytes via PIL / browser handle
        the format from the magic bytes, so the .png extension is a harmless
        misnomer kept for inter-adapter uniformity.
        """
        device_path = "/data/local/tmp/ui_capture_shot.jpeg"
        r = self._run_hdc(["shell", "snapshot_display", "-f", device_path], timeout=30)
        if r.returncode != 0:
            return False
        # Combined stdout+stderr message ("fileType: ...\n"); the file exists
        # iff snapshot_display actually wrote it.
        pull = self._run_hdc(["file", "recv", device_path, str(out_path)], timeout=30)
        return pull.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0

    def _wait_until_stable(self) -> tuple[str, int, int]:
        """Repeatedly dump layout, MD5-compare. Return ('stable'|'timeout', polls, elapsed_ms).

        Same shape as Android adapter's stability gate — gives async ArkUI data
        loads (LazyForEach, network fetches) time to settle before capture.
        """
        t0 = time.time()
        deadline = t0 + self.STABLE_MAX_WAIT_S
        prev = None
        stable_count = 0
        polls = 0
        while time.time() < deadline:
            polls += 1
            text = self._dump_layout_text()
            h = hashlib.md5(text.encode("utf-8", errors="ignore")).hexdigest() if text else ""
            if h and h == prev:
                stable_count += 1
                if stable_count >= self.STABLE_SETTLE_REPEATS:
                    return ("stable", polls, int((time.time() - t0) * 1000))
            else:
                stable_count = 0
                prev = h
            time.sleep(self.STABLE_POLL_INTERVAL_S)
        return ("timeout", polls, int((time.time() - t0) * 1000))

    # --------------------------------------------------------- helpers

    @staticmethod
    def _slugify(name: str) -> str:
        return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "page"

    @staticmethod
    def _rel(path: Path, root: Path) -> str:
        try:
            return path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return path.as_posix()
