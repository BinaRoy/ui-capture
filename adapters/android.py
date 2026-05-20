"""
Android adapter: drives adb + uiautomator dump.

Identity probing strategy:
- top Activity from `dumpsys activity activities` (the resumed activity)
- current Fragment from `dumpsys activity <ComponentName>` (FragmentManager state)

Capture strategy:
- screenshot: `adb exec-out screencap -p`
- raw hierarchy: `adb shell uiautomator dump /sdcard/<file>.xml` then pull
- normalize: parse uiautomator XML to UI-IR

Static discovery:
- res/navigation/*.xml NavGraph destinations
- AndroidManifest.xml activities
- Fragment subclass scan (kotlin + java) — best-effort regex
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import shlex
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable, Optional


def _resolve_adb(explicit: str) -> str:
    """Find adb. Order: explicit (if absolute or found in PATH) → ANDROID_HOME/SDK_ROOT
    → common install locations. Returns whatever we find first, even if not executable —
    the caller will surface a clearer error than 'command not found'.
    """
    # If caller passed an absolute path, trust it
    if "/" in explicit or "\\" in explicit:
        return explicit
    # If found in PATH, use that
    found = shutil.which(explicit)
    if found:
        return found
    # Try SDK locations
    candidates: list[Path] = []
    for env_var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        root = os.environ.get(env_var)
        if root:
            candidates.append(Path(root) / "platform-tools" / "adb")
    candidates.extend([
        Path.home() / "Library" / "Android" / "sdk" / "platform-tools" / "adb",  # macOS
        Path.home() / "Android" / "Sdk" / "platform-tools" / "adb",              # Linux
        Path("/opt/android-sdk/platform-tools/adb"),
        Path("/usr/local/lib/android/sdk/platform-tools/adb"),
    ])
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    # Give up — return the literal so error messages remain clear
    return explicit

from .base import (
    Adapter,
    AdapterError,
    CaptureResult,
    PageHint,
    PageIdentity,
)


_FRAGMENT_BASE_HINTS = (
    "Fragment",
    "BottomSheetDialogFragment",
    "DialogFragment",
    "PreferenceFragmentCompat",
)


class AndroidAdapter(Adapter):
    platform = "android"

    def __init__(self, adb: str = "adb"):
        self.adb = _resolve_adb(adb)

    # ------------------------------------------------------------------ infra

    def _run_adb(self, args: list[str], *, capture_output: bool = True, timeout: int = 30) -> subprocess.CompletedProcess:
        cmd = [self.adb, *args]
        return subprocess.run(cmd, capture_output=capture_output, text=True, timeout=timeout)

    def check_infrastructure(self) -> tuple[bool, str]:
        # _resolve_adb may have returned a literal "adb" if nothing was found —
        # verify it's actually executable now.
        if "/" not in self.adb and "\\" not in self.adb:
            if shutil.which(self.adb) is None:
                return False, (
                    f"adb not found in PATH, ANDROID_HOME, ANDROID_SDK_ROOT, or common SDK locations. "
                    f"Set ANDROID_HOME to your Android Studio SDK root, or add platform-tools to PATH."
                )
        if not Path(self.adb).is_file():
            return False, f"adb path resolved to {self.adb!r} but file does not exist"
        try:
            result = self._run_adb(["devices"], timeout=10)
        except subprocess.TimeoutExpired:
            return False, f"`{self.adb} devices` timed out (adb server stuck? try `adb kill-server`)"
        if result.returncode != 0:
            return False, f"`{self.adb} devices` failed: {result.stderr.strip()}"
        lines = [l for l in result.stdout.splitlines() if l.strip() and not l.startswith("List of devices")]
        devices = [l for l in lines if "\tdevice" in l]
        if not devices:
            return False, (
                f"no Android device or emulator attached. Start an Android Studio emulator (Tools → Device Manager), "
                f"then re-run. `{self.adb} devices` must list one with state 'device'."
            )
        return True, f"ok ({len(devices)} device(s) via {self.adb})"

    # ------------------------------------------------------- static discovery

    def discover_hints(self, source_root: Path) -> list[PageHint]:
        hints: dict[str, PageHint] = {}  # dedupe by class_name

        for hint in self._discover_navgraph(source_root):
            hints.setdefault(hint.class_name, hint)
        for hint in self._discover_activities(source_root):
            hints.setdefault(hint.class_name, hint)
        for hint in self._discover_fragment_subclasses(source_root):
            hints.setdefault(hint.class_name, hint)

        # Stable order: by source_file then class_name
        return sorted(hints.values(), key=lambda h: (h.source_file, h.class_name))

    def _discover_navgraph(self, source_root: Path) -> Iterable[PageHint]:
        for nav_xml in source_root.rglob("res/navigation/*.xml"):
            if _in_skipped_dir(nav_xml):
                continue
            try:
                tree = ET.parse(nav_xml)
            except ET.ParseError:
                continue
            for el in tree.iter():
                tag = el.tag.split("}")[-1]
                if tag not in ("fragment", "activity", "dialog"):
                    continue
                cls = el.attrib.get("{http://schemas.android.com/apk/res/android}name") \
                    or el.attrib.get("android:name") \
                    or el.attrib.get("name")
                if not cls:
                    continue
                short_name = cls.rsplit(".", 1)[-1]
                yield PageHint(
                    slug=_slugify(short_name),
                    source_file=_relpath(nav_xml, source_root),
                    class_name=short_name,
                    origin="navgraph",
                    note=f"destination in {nav_xml.name}",
                )

    def _discover_activities(self, source_root: Path) -> Iterable[PageHint]:
        manifests = [m for m in source_root.rglob("AndroidManifest.xml") if not _in_skipped_dir(m)]
        for manifest in manifests:
            try:
                tree = ET.parse(manifest)
            except ET.ParseError:
                continue
            ns = "{http://schemas.android.com/apk/res/android}"
            for activity in tree.iter("activity"):
                cls = activity.attrib.get(ns + "name")
                if not cls:
                    continue
                short_name = cls.rsplit(".", 1)[-1]
                yield PageHint(
                    slug=_slugify(short_name),
                    source_file=_relpath(manifest, source_root),
                    class_name=short_name,
                    origin="manifest",
                    note="activity declared in AndroidManifest",
                )

    def _discover_fragment_subclasses(self, source_root: Path) -> Iterable[PageHint]:
        # Best-effort regex over .java / .kt looking for "class X extends *Fragment"
        # or "class X : *Fragment(...)" / "object X : *Fragment".
        java_pat = re.compile(r"class\s+(\w+)\s+extends\s+(\w*Fragment\w*)")
        kt_pat = re.compile(r"(?:class|object)\s+(\w+)\s*[^{]*:\s*([\w.]*Fragment\w*)\b")
        for path in _iter_source_files(source_root, (".java", ".kt")):
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for pat in (java_pat, kt_pat):
                for m in pat.finditer(text):
                    short_name, base = m.group(1), m.group(2)
                    base_short = base.rsplit(".", 1)[-1]
                    if not any(h in base_short for h in _FRAGMENT_BASE_HINTS):
                        continue
                    yield PageHint(
                        slug=_slugify(short_name),
                        source_file=_relpath(path, source_root),
                        class_name=short_name,
                        origin="fragment_subclass",
                        note=f"extends {base}",
                    )

    # --------------------------------------------------- nav script scaffold

    def render_nav_script(self, hints: list[PageHint], out_path: Path) -> None:
        if out_path.exists():
            return  # never overwrite user-edited nav scripts
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # Embed the resolved adb path so the script runs even when the invoking
        # shell (e.g. Claude Code Bash sessions) doesn't have platform-tools in
        # PATH. ${ADB:-...} still lets operators override.
        adb_path = self.adb if ("/" in self.adb or "\\" in self.adb) else "adb"
        lines = [
            "#!/usr/bin/env bash",
            "# Android UI capture nav script — generated scaffold.",
            "#",
            "# Each capture_page <id> call should be preceded by adb commands that bring",
            "# the corresponding screen to the foreground. The capture function is provided",
            "# by the harness (do not redefine).",
            "#",
            "# Edit this file to match the actual navigation of your app. The scaffold is a",
            "# starting point derived from static discovery; it is almost certainly complete",
            "# only for the launcher screen — every other hint below is a TODO with no taps.",
            "",
            "set -euo pipefail",
            "",
            f'ADB="${{ADB:-{adb_path}}}"',
            ': "${PACKAGE:?set PACKAGE env to the app package name}"',
            ': "${MAIN_ACTIVITY:?set MAIN_ACTIVITY to the launcher activity (e.g. .ui.MainActivity)}"',
            "",
            "# Launch app",
            '"$ADB" shell am start -n "${PACKAGE}/${MAIN_ACTIVITY}"',
            "sleep 2",
            "",
        ]

        # Treat the first hint as the launcher landing page — capture immediately
        # after `am start`. For every remaining hint emit a guarded TODO block so
        # the operator can see exactly what's missing (which tap, which keyevent),
        # uncomment after filling in coordinates, and re-run. Today the scaffold
        # silently dropped 5 of 6 hints from WeatherApp because only the first
        # got an explicit capture_page line.
        if hints:
            launcher = hints[0]
            lines.append(f"# Launcher screen ({launcher.class_name}, {launcher.origin}: {launcher.note})")
            lines.append(f"capture_page {launcher.slug}")
            lines.append("")
            remaining = hints[1:]
        else:
            lines.append("# No hints discovered — fill in capture_page calls manually.")
            lines.append("# capture_page TODO_replace_with_page_id")
            lines.append("")
            remaining = []

        if remaining:
            lines.append("# ---- TODO: navigate to and capture the remaining discovered screens ----")
            lines.append("# Each block below is COMMENTED OUT until you supply real adb input commands.")
            lines.append("# Replace the `# adb shell input tap X Y` placeholder with the actual coords")
            lines.append("# (or `input keyevent`, swipe, etc.) that brings the page to the foreground,")
            lines.append("# then uncomment both the navigation line and the capture_page line.")
            lines.append("# If a hint isn't reachable as a standalone screen (e.g. it's an inline")
            lines.append("# sub-view rendered by another page), delete its block instead of filling it.")
            lines.append("")
            for h in remaining:
                lines.append(f"# --- {h.class_name} ({h.origin}: {h.note}) ---")
                lines.append(f'# "$ADB" shell input tap   X   Y   # TODO: tap target that opens {h.class_name}')
                lines.append("# sleep 1")
                lines.append(f"# capture_page {h.slug}")
                lines.append('# "$ADB" shell input keyevent KEYCODE_BACK   # TODO: return to launcher (or chain into next page)')
                lines.append("# sleep 1")
                lines.append("")

        lines.append("# Add additional capture_page entries below for screens not in static discovery")
        lines.append("# (modals, deep-linked pages, etc.).")
        lines.append("")
        out_path.write_text("\n".join(lines), encoding="utf-8")
        out_path.chmod(0o755)

    # ----------------------------------------------------- runtime probing

    # Match a package/activity pair within an ActivityRecord{...} block on a line that
    # mentions ResumedActivity or topResumedActivity. Tolerant to format drift across
    # Android versions (some have "u0", PID, hash, etc. in varying positions).
    _RESUMED_ACTIVITY_LINE_RE = re.compile(
        r"(?:mResumedActivity|topResumedActivity)[^\n]*?"
        r"([A-Za-z][\w.]*)/(\.?[\w.$]+)",
        re.MULTILINE,
    )

    def probe_identity(self) -> PageIdentity:
        # 1) Resumed activity (works on most Android versions)
        result = self._run_adb(["shell", "dumpsys", "activity", "activities"], timeout=15)
        if result.returncode != 0:
            raise AdapterError(f"dumpsys activities failed: {result.stderr.strip()}")
        text = result.stdout
        m = self._RESUMED_ACTIVITY_LINE_RE.search(text)
        if not m:
            raise AdapterError("could not locate resumed activity in dumpsys output")
        package, activity = m.group(1), m.group(2)
        # Activity class may be ".ui.MainActivity" — resolve to absolute then take short name
        if activity.startswith("."):
            full = package + activity
        else:
            full = activity
        activity_short = full.rsplit(".", 1)[-1]

        # 2) Current Fragment via dumpsys activity <component>
        fragment_short = self._probe_current_fragment(package, full)

        return PageIdentity(
            top_component=activity_short,
            current_fragment=fragment_short,
            extra={"package": package, "activity_full": full},
        )

    # Framework-internal fragments that show up in dumpsys but are not user-visible UI.
    # ReportFragment is the AndroidX lifecycle helper; SupportRequestManagerFragment is
    # injected by Glide; NavHostFragment is just a container. We keep NavHostFragment
    # filtering conservative — drop only the obvious helpers so user pages survive even
    # in unusual nav setups.
    _INTERNAL_FRAGMENT_NAMES = frozenset({
        "ReportFragment",
        "SupportRequestManagerFragment",
        "EmptyFragment",
        "FrameworkFragment",
    })
    _INTERNAL_PACKAGE_PREFIXES = (
        "androidx.",
        "android.",
        "com.google.android.material.",
        "com.google.android.gms.",
        "com.bumptech.glide.",
    )

    # Each entry looks like:  #0: SomeFragment{abc} (...)  OR  #0: com.foo.Bar{abc} (...)
    _ADDED_ENTRY_RE = re.compile(r"#\d+:\s*([\w.$]+)\{")

    def _probe_current_fragment(self, package: str, full_activity: str) -> Optional[str]:
        """Pick the currently-visible user Fragment from dumpsys.

        Algorithm (parse the FragmentManager tree explicitly):

          1. Parse every "Added Fragments:" block: indent, entries, and parent
             fragment (from preceding "Child FragmentManager{... in <Parent>{...}}}"
             marker; None for the activity-level support FM).
          2. Filter framework-internal classes.
          3. Identify the activity-level block: parent=None at MIN indent among
             user-visible blocks. Its LAST entry is the current top-of-back-stack.
          4. For that top-level fragment, find child blocks whose parent matches
             (by class name). If any has a user-visible entry, recurse into it —
             that's the deeper visible content (e.g. TodayWeatherFragment under
             MainFragment, SettingsFragment under MainSettingsFragment).
          5. Otherwise return the top-level fragment (e.g. SearchMenuFragment, a
             BottomSheetDialogFragment with no user-visible inner content).
        """
        component = f"{package}/{full_activity}"
        result = self._run_adb(["shell", "dumpsys", "activity", component], timeout=20)
        if result.returncode != 0:
            return None
        text = result.stdout

        class Block:
            __slots__ = ("start", "indent", "parent", "entries", "user")
            def __init__(self, start, indent, parent, entries, user):
                self.start = start
                self.indent = indent
                self.parent = parent
                self.entries = entries
                self.user = user

        # To attribute each "Added Fragments" block to its enclosing parent fragment
        # we walk markers and blocks together in document order, maintaining a stack
        # of (marker_indent, parent_class). A block at indent X is inside the deepest
        # still-active marker whose indent < X. Each event first pops scopes whose
        # indent >= current_indent (we've exited them).
        marker_re = re.compile(
            r"^(?P<indent>[ \t]*)Child FragmentManager\{[^}]*\s+in\s+(?P<parent>[\w.$]+)\{",
            re.MULTILINE,
        )
        block_re = re.compile(
            r"^(?P<indent>[ \t]*)Added Fragments:\s*\n(?P<body>(?:\1[ \t]+#\d+:.*\n)+)",
            re.MULTILINE,
        )

        # Collect every event, tagged with its kind, then sort by position.
        events: list[tuple[int, str, object]] = []  # (pos, "marker"|"block", payload)
        for m in marker_re.finditer(text):
            indent = len(m.group("indent").expandtabs(4))
            events.append((m.start(), "marker", (indent, _short_class(m.group("parent")))))
        for bm in block_re.finditer(text):
            indent = len(bm.group("indent").expandtabs(4))
            raw: list[str] = []
            for line in bm.group("body").splitlines():
                m = self._ADDED_ENTRY_RE.search(line)
                if m:
                    raw.append(m.group(1))
            if not raw:
                continue
            user = [e for e in raw if not self._is_internal_fragment(e)]
            events.append((bm.start(), "block", (indent, raw, user)))
        events.sort(key=lambda e: e[0])

        blocks: list[Block] = []
        scope_stack: list[tuple[int, str]] = []  # (marker_indent, parent_class)
        for pos, kind, payload in events:
            if kind == "marker":
                indent, parent_cls = payload  # type: ignore[misc]
                # Exit any scopes at >= this indent
                while scope_stack and scope_stack[-1][0] >= indent:
                    scope_stack.pop()
                scope_stack.append((indent, parent_cls))
            else:  # block
                indent, raw, user = payload  # type: ignore[misc]
                while scope_stack and scope_stack[-1][0] >= indent:
                    scope_stack.pop()
                # Stack now has only markers strictly less indented than this block.
                # The TOP of the stack is the immediate enclosing parent (or empty → None).
                parent_cls = scope_stack[-1][1] if scope_stack else None
                blocks.append(Block(start=pos, indent=indent, parent=parent_cls,
                                    entries=raw, user=user))

        user_blocks = [b for b in blocks if b.user]
        if not user_blocks:
            all_raw = [e for b in blocks for e in b.entries]
            if all_raw:
                return _short_class(all_raw[-1])
            return None

        # Activity-level block: parent=None, smallest indent among parent=None blocks.
        root_blocks = [b for b in user_blocks if b.parent is None]
        if not root_blocks:
            # Defensive: take the shallowest user block
            root_blocks = sorted(user_blocks, key=lambda b: b.indent)[:1]
        root = sorted(root_blocks, key=lambda b: (b.indent, b.start))[0]
        current_fqcn = root.user[-1]
        current = _short_class(current_fqcn)

        # Descend: look for a user-visible block whose parent == current.
        # Repeat until no deeper block.
        seen = {current}
        while True:
            children = [b for b in user_blocks if b.parent == current]
            if not children:
                break
            # If multiple child blocks at this level (rare), pick the deepest.
            child = sorted(children, key=lambda b: (-b.indent, b.start))[0]
            next_fqcn = child.user[-1]
            nxt = _short_class(next_fqcn)
            if nxt in seen:
                break  # cycle guard
            current = nxt
            seen.add(current)
        return current

    def _is_internal_fragment(self, fully_qualified: str) -> bool:
        short = fully_qualified.rsplit(".", 1)[-1].split("$", 1)[0]
        if short in self._INTERNAL_FRAGMENT_NAMES:
            return True
        return any(fully_qualified.startswith(p) for p in self._INTERNAL_PACKAGE_PREFIXES)

    # -------------------------------------------------------------- capture

    # UI stability gating constants. Two consecutive identical dumps (default
    # `STABLE_SETTLE_REPEATS = 2`) are required before we consider the page
    # settled. `STABLE_MAX_WAIT_S` caps the total wait so pages with persistent
    # animation (lottie, marquees, infinite loaders) don't hang the run.
    STABLE_MAX_WAIT_S = 10.0
    STABLE_SETTLE_REPEATS = 2
    STABLE_POLL_INTERVAL_S = 0.5

    def _wait_until_stable(self) -> dict:
        """Poll uiautomator dumps until the view tree stops changing.

        Strategy: dump the hierarchy XML, hash it, compare to the previous
        hash. When the same hash repeats `STABLE_SETTLE_REPEATS` times in a
        row, the UI is considered settled and we return.

        Returns a diagnostic dict (always — never raises) with:
          status: "stable" | "timeout" | "no_dump"
          elapsed_ms, polls, settled_at_poll, last_hash
        Callers may log this; failure to settle is NOT fatal — we proceed
        with the capture anyway, on the theory that a possibly-imperfect
        snapshot beats no snapshot.
        """
        device_xml = "/sdcard/ui_capture_stability_probe.xml"
        prev_hash: Optional[str] = None
        same_count = 0
        polls = 0
        t0 = time.time()
        deadline = t0 + self.STABLE_MAX_WAIT_S
        while time.time() < deadline:
            polls += 1
            try:
                self._run_adb(["shell", "rm", "-f", device_xml], timeout=5)
                dump = self._run_adb(
                    ["shell", "uiautomator", "dump", device_xml], timeout=15,
                )
                if dump.returncode != 0:
                    time.sleep(self.STABLE_POLL_INTERVAL_S)
                    continue
                cat = self._run_adb(["shell", "cat", device_xml], timeout=5)
                xml_text = cat.stdout or ""
                if not xml_text.strip():
                    time.sleep(self.STABLE_POLL_INTERVAL_S)
                    continue
                h = hashlib.md5(xml_text.encode("utf-8", errors="replace")).hexdigest()
                if h == prev_hash:
                    same_count += 1
                    if same_count >= self.STABLE_SETTLE_REPEATS:
                        # Clean up the probe file (best-effort).
                        try:
                            self._run_adb(["shell", "rm", "-f", device_xml], timeout=5)
                        except subprocess.TimeoutExpired:
                            pass
                        return {
                            "status": "stable",
                            "elapsed_ms": int((time.time() - t0) * 1000),
                            "polls": polls,
                            "settled_at_poll": polls,
                            "last_hash": h,
                        }
                else:
                    same_count = 0
                    prev_hash = h
            except subprocess.TimeoutExpired:
                # adb hiccup — count it as a poll and keep trying
                pass
            time.sleep(self.STABLE_POLL_INTERVAL_S)

        return {
            "status": "timeout" if prev_hash else "no_dump",
            "elapsed_ms": int((time.time() - t0) * 1000),
            "polls": polls,
            "settled_at_poll": None,
            "last_hash": prev_hash,
        }

    def capture(self, page_id: str, out_dir: Path) -> CaptureResult:
        start = time.time()
        out_dir.mkdir(parents=True, exist_ok=True)
        identity: Optional[PageIdentity] = None
        screenshot_path = out_dir / "screenshot.png"
        raw_path = out_dir / "raw.xml"

        try:
            identity = self.probe_identity()
        except AdapterError as exc:
            return CaptureResult(
                status="failed",
                identity=None,
                raw_path=None,
                screenshot_path=None,
                hierarchy=None,
                error=f"probe_identity: {exc}",
                duration_ms=int((time.time() - start) * 1000),
            )

        # Wait for the UI to settle before screenshot + dump. Critical when
        # async data load (LiveData / ViewModel callbacks) populates text long
        # after View inflation — without this, captures hit the "skeleton" state
        # (e.g. `Feels like %1$s` placeholders), and stitching mixes skeleton
        # bounds with later-loaded bounds, producing overlapping nodes.
        stability = self._wait_until_stable()
        print(
            f"[android] {page_id} stability={stability['status']} "
            f"elapsed={stability['elapsed_ms']}ms polls={stability['polls']}",
            flush=True,
        )

        # Screenshot via exec-out (binary stdout)
        try:
            shot = subprocess.run(
                [self.adb, "exec-out", "screencap", "-p"],
                capture_output=True, timeout=20,
            )
            if shot.returncode != 0 or not shot.stdout:
                return CaptureResult(
                    status="failed", identity=identity, raw_path=None,
                    screenshot_path=None, hierarchy=None,
                    error=f"screencap failed: {shot.stderr.decode(errors='ignore').strip()}",
                    duration_ms=int((time.time() - start) * 1000),
                )
            screenshot_path.write_bytes(shot.stdout)
        except subprocess.TimeoutExpired:
            return CaptureResult(
                status="failed", identity=identity, raw_path=None,
                screenshot_path=None, hierarchy=None,
                error="screencap timed out",
                duration_ms=int((time.time() - start) * 1000),
            )

        # uiautomator dump → /sdcard, then pull
        device_xml = "/sdcard/ui_capture_dump.xml"
        try:
            self._run_adb(["shell", "rm", "-f", device_xml], timeout=10)
            dump = self._run_adb(["shell", "uiautomator", "dump", device_xml], timeout=30)
            if dump.returncode != 0:
                return CaptureResult(
                    status="failed", identity=identity, raw_path=None,
                    screenshot_path=screenshot_path, hierarchy=None,
                    error=f"uiautomator dump failed: {dump.stderr.strip() or dump.stdout.strip()}",
                    duration_ms=int((time.time() - start) * 1000),
                )
            pull = self._run_adb(["pull", device_xml, str(raw_path)], timeout=15)
            if pull.returncode != 0 or not raw_path.exists():
                return CaptureResult(
                    status="failed", identity=identity, raw_path=None,
                    screenshot_path=screenshot_path, hierarchy=None,
                    error=f"adb pull failed: {pull.stderr.strip()}",
                    duration_ms=int((time.time() - start) * 1000),
                )
        except subprocess.TimeoutExpired:
            return CaptureResult(
                status="failed", identity=identity, raw_path=None,
                screenshot_path=screenshot_path, hierarchy=None,
                error="uiautomator dump timed out",
                duration_ms=int((time.time() - start) * 1000),
            )

        try:
            hierarchy = self.normalize(raw_path)
        except Exception as exc:  # parsing should not crash the run
            return CaptureResult(
                status="failed", identity=identity, raw_path=raw_path,
                screenshot_path=screenshot_path, hierarchy=None,
                error=f"normalize: {exc}",
                duration_ms=int((time.time() - start) * 1000),
            )

        # If the page has a scrollable container with content extending past the
        # viewport, walk down it: swipe + capture per step, then stitch the
        # screenshots into one tall PNG and union the hierarchies into a single tree
        # with full-page bounds. The viewport screenshot.png is preserved as the
        # canonical "what the user first sees"; the stitched copy goes to a
        # distinctly-named screenshot_fullpage.png with visual fold-line annotations.
        fullpage_path: Optional[Path] = None
        try:
            extended = self._scroll_and_extend(out_dir, screenshot_path, hierarchy)
            if extended is not None:
                fullpage_path, hierarchy = extended
        except Exception as exc:  # scrolling is best-effort, never fatal
            (out_dir / "scroll_error.log").write_text(repr(exc), encoding="utf-8")

        return CaptureResult(
            status="captured",
            identity=identity,
            raw_path=raw_path,
            screenshot_path=screenshot_path,
            hierarchy=hierarchy,
            duration_ms=int((time.time() - start) * 1000),
            fullpage_screenshot_path=fullpage_path,
        )

    # --------------------------------------------------------- scroll capture

    # Conservative defaults so even bouncy / slow scrollers behave well.
    SCROLL_MAX_STEPS = 8
    SCROLL_SWIPE_DURATION_MS = 250
    SCROLL_SETTLE_S = 0.7
    # Threshold below which a swipe is considered "didn't move" → stop.
    SCROLL_MIN_DELTA_PX = 60

    def _scroll_and_extend(
        self, out_dir: Path, viewport_shot: Path, base_hierarchy: dict,
    ) -> Optional[tuple[Path, dict]]:
        """Walk a scrollable container, capture each step, stitch + unify.

        Returns (full_screenshot_path, unified_hierarchy) or None if the page
        wasn't scrollable / scroll yielded no extra content.
        """
        if not self._has_scrollable_content(base_hierarchy):
            return None

        try:
            from PIL import Image  # type: ignore
        except ImportError:
            # Without Pillow we can still try, but stitching is impossible.
            return None

        # Read viewport dimensions from the screenshot itself (authoritative)
        with Image.open(viewport_shot) as img:
            viewport_w, viewport_h = img.size

        # Anchor map for dump 0: {stable_key: bounds[top]}
        anchors_prev = self._anchor_map(base_hierarchy)
        if not anchors_prev:
            return None  # nothing stable to measure against

        scroll_dir = out_dir / "scroll"
        scroll_dir.mkdir(exist_ok=True)
        # screenshot.png at out_dir IS the viewport (caller saved it there before
        # calling us). Reference it as scroll/00.png via a relative symlink so
        # the file is observable as "step 0" without duplicating ~500KB on disk.
        # Fall back to a byte copy on filesystems that don't support symlinks.
        step0_link = scroll_dir / "00.png"
        if step0_link.exists() or step0_link.is_symlink():
            step0_link.unlink()
        try:
            step0_link.symlink_to(Path("..") / viewport_shot.name)
        except (OSError, NotImplementedError):
            step0_link.write_bytes(viewport_shot.read_bytes())

        steps: list[dict] = [{"offset": 0, "hierarchy": base_hierarchy, "image_path": scroll_dir / "00.png"}]
        cumulative = 0

        # Swipe path: from near bottom to near top of viewport, leaving a margin
        # so we don't hit system gesture areas.
        swipe_from_y = int(viewport_h * 0.85)
        swipe_to_y = int(viewport_h * 0.15)
        swipe_dx = viewport_w // 2

        termination_reason = "max_steps_reached"
        for step in range(1, self.SCROLL_MAX_STEPS + 1):
            swipe = self._run_adb([
                "shell", "input", "swipe",
                str(swipe_dx), str(swipe_from_y),
                str(swipe_dx), str(swipe_to_y),
                str(self.SCROLL_SWIPE_DURATION_MS),
            ], timeout=15)
            if swipe.returncode != 0:
                termination_reason = "swipe_failed"
                break
            time.sleep(self.SCROLL_SETTLE_S)

            # Fresh dump
            device_xml = "/sdcard/ui_capture_dump.xml"
            self._run_adb(["shell", "rm", "-f", device_xml], timeout=10)
            dr = self._run_adb(["shell", "uiautomator", "dump", device_xml], timeout=30)
            if dr.returncode != 0:
                termination_reason = "dump_failed"
                break
            step_raw = scroll_dir / f"{step:02d}.xml"
            pr = self._run_adb(["pull", device_xml, str(step_raw)], timeout=15)
            if pr.returncode != 0 or not step_raw.exists():
                termination_reason = "pull_failed"
                break
            step_h = self.normalize(step_raw)

            # Fresh screenshot
            shot = subprocess.run(
                [self.adb, "exec-out", "screencap", "-p"],
                capture_output=True, timeout=20,
            )
            if shot.returncode != 0 or not shot.stdout:
                termination_reason = "screencap_failed"
                break
            step_img = scroll_dir / f"{step:02d}.png"
            step_img.write_bytes(shot.stdout)

            # Compute delta from anchor median
            anchors_now = self._anchor_map(step_h)
            delta = self._median_anchor_delta(anchors_prev, anchors_now)
            # B.5: overlap check — if most anchors from the previous frame still
            # appear in the new frame, very little new content was revealed; that's
            # the strongest "page bottom reached" signal, more reliable than the
            # delta threshold alone (which can be fooled by sticky headers).
            common = set(anchors_prev) & set(anchors_now)
            overlap_ratio = (len(common) / len(anchors_prev)) if anchors_prev else 0.0

            if delta is None:
                termination_reason = "all_anchors_lost"
                step_raw.unlink(missing_ok=True); step_img.unlink(missing_ok=True)
                break
            if delta < self.SCROLL_MIN_DELTA_PX:
                termination_reason = "delta_below_threshold"
                step_raw.unlink(missing_ok=True); step_img.unlink(missing_ok=True)
                break
            if overlap_ratio > 0.9 and delta < viewport_h * 0.1:
                # Almost everything from previous frame still on screen AND
                # the scroll moved less than 10% of viewport — page is at bottom.
                termination_reason = "high_overlap_bottom_reached"
                step_raw.unlink(missing_ok=True); step_img.unlink(missing_ok=True)
                break

            cumulative += delta
            steps.append({"offset": cumulative, "hierarchy": step_h, "image_path": step_img})
            anchors_prev = anchors_now

        if len(steps) < 2:
            # Page is scrollable in markup but had no extra content to reveal.
            return None

        # === Part C: dual-artifact naming ===
        # screenshot.png stays as the viewport (what the user first sees). The full
        # stitched page lives under a distinct, self-describing name so downstream
        # consumers can never confuse them.
        # The initial single-viewport screenshot was already saved at viewport_shot
        # (=out_dir/screenshot.png) by the caller. Keep it.
        fullpage_path = out_dir / "screenshot_fullpage.png"

        stitched = self._stitch_images([s["image_path"] for s in steps],
                                       [s["offset"] for s in steps],
                                       viewport_h)
        # === Part B: visual fold lines on stitched image ===
        # Draw a red dashed horizontal line + label at every viewport boundary so
        # any LLM reading this image visually cannot miss that scrolling was
        # required. Critical mitigation: a tall image without these annotations
        # would silently invite Workers to omit the Scroll() wrapper in ArkUI.
        deltas = [steps[i]["offset"] - steps[i - 1]["offset"] for i in range(1, len(steps))]
        self._draw_fold_lines(stitched, viewport_h, deltas)
        stitched.save(fullpage_path, format="PNG")

        page_total_height = viewport_h + steps[-1]["offset"]

        unified = self._union_hierarchies(steps, viewport_h)

        # B.4: stitch quality self-check. After merging all scroll-step hierarchies
        # into one tree, every node's `bounds[3]` (bottom y) must lie within the
        # stitched canvas. Out-of-bounds nodes mean stitching dropped scroll steps
        # or anchor drift accumulated — surfacing as warnings prevents silent
        # truncation of the fullpage view.
        stitch_warnings = self._check_stitch_quality(unified, page_total_height)

        (out_dir / "scroll_meta.json").write_text(
            __import__("json").dumps({
                "viewport": [viewport_w, viewport_h],
                "steps": [{"offset": s["offset"]} for s in steps],
                "page_total_height": page_total_height,
                "viewport_screenshot": "screenshot.png",
                "fullpage_screenshot": "screenshot_fullpage.png",
                "termination_reason": termination_reason,
                "stitch_warnings": stitch_warnings,
                "step0_is_symlink": step0_link.is_symlink(),
            }, indent=2), encoding="utf-8",
        )
        if stitch_warnings:
            print(f"[android] stitch quality warnings for {out_dir.name}: "
                  f"{len(stitch_warnings)} node(s) out of fullpage bounds")
        # === Part A: IR-level scroll metadata ===
        # Annotate the root so structured consumers (arch-gen, Reviewer scripts) get
        # an explicit signal without having to look at scroll_meta.json. The number
        # of scroll steps and the total height let the consumer reason about
        # "is this a scrollable section?" without re-deriving from bounds.
        unified["scrolled"] = True
        unified["viewport_height"] = viewport_h
        unified["page_total_height"] = page_total_height
        unified["scroll_steps"] = len(steps) - 1  # number of swipes, not snapshots
        return fullpage_path, unified

    def _draw_fold_lines(self, image, viewport_h: int, deltas: list[int]) -> None:
        """Overlay dashed horizontal lines at every scroll-fold boundary plus a
        loud text banner explaining what they mean. Modifies `image` in place.

        Fold positions in the stitched image:
          fold_0 = viewport_h                 (boundary after the first viewport)
          fold_i = fold_{i-1} + delta_i       (after the new content from step i)
        """
        from PIL import ImageDraw, ImageFont
        draw = ImageDraw.Draw(image)
        font_big, font_small = self._load_fonts()

        line_color = (220, 50, 50, 255)
        label_bg = (255, 240, 200, 255)
        label_fg = (170, 30, 30, 255)

        fold_y = viewport_h
        for i, _ in enumerate(deltas, start=1):
            # Dashed line across full width
            for x in range(0, image.width, 32):
                draw.line([(x, fold_y), (x + 16, fold_y)], fill=line_color, width=4)
            # Two-line label
            line1 = f" ⬇  SCROLL FOLD #{i}  ⬇ "
            line2 = (
                f" The user had to scroll here. Total page height = "
                f"{viewport_h + sum(deltas)}px (viewport {viewport_h}px). "
                f"In ArkUI: wrap with Scroll() so this content is reachable."
            )
            self._draw_label(draw, (16, fold_y + 6), line1, font_big, label_bg, label_fg)
            tb = draw.textbbox((0, 0), line1, font=font_big)
            self._draw_label(draw, (16, fold_y + (tb[3] - tb[1]) + 18), line2,
                             font_small, label_bg, label_fg)
            # Advance for the next fold (if any)
            if i < len(deltas):
                fold_y += deltas[i]  # next fold is delta_{i+1} below this one

        # Top-of-image banner so the message is also visible at first glance,
        # even before the reader scrolls a long image.
        banner = (
            f"  ⚠  SCROLLABLE PAGE — total height {viewport_h + sum(deltas)}px, "
            f"viewport {viewport_h}px, {len(deltas)} swipe(s). "
            f"Implement with Scroll() {{ … }} in ArkUI; this image is a STITCH, not a single viewport.  "
        )
        self._draw_label(draw, (16, 16), banner, font_big, label_bg, label_fg)

    @staticmethod
    def _load_fonts():
        from PIL import ImageFont
        candidates = (
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        )
        for path in candidates:
            try:
                return ImageFont.truetype(path, 30), ImageFont.truetype(path, 22)
            except OSError:
                continue
        f = ImageFont.load_default()
        return f, f

    @staticmethod
    def _draw_label(draw, xy, text, font, bg, fg, pad: int = 8) -> None:
        bbox = draw.textbbox((0, 0), text, font=font)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        x, y = xy
        draw.rectangle([(x, y), (x + w + pad * 2, y + h + pad * 2)], fill=bg,
                       outline=(170, 30, 30, 255), width=2)
        draw.text((x + pad, y + pad), text, font=font, fill=fg)

    def _has_scrollable_content(self, hierarchy: dict) -> bool:
        """True if any node is marked scrollable. Cheap structural test."""
        def walk(n: dict) -> bool:
            if "scrollable" in (n.get("flags") or []):
                return True
            cls = n.get("class", "")
            if any(s in cls for s in ("ScrollView", "RecyclerView", "ViewPager", "NestedScrollView")):
                return True
            for c in n.get("children", []):
                if walk(c):
                    return True
            return False
        for c in hierarchy.get("children", []):
            if walk(c):
                return True
        return False

    def _anchor_map(self, hierarchy: dict) -> dict:
        """Build {stable_key: y_top} for elements that have a resource-id OR a text.

        Keys are stable across dumps; values are bounds.top in current viewport coords.
        Excludes system-UI ids (status bar, navigation bar).
        """
        anchors: dict[str, int] = {}

        def stable_key(n: dict) -> Optional[str]:
            rid = n.get("id") or ""
            txt = (n.get("text") or "").strip()
            cls = n.get("class", "")
            if rid and not rid.startswith(("com.android.systemui:", "android:id/statusBar",
                                            "android:id/navigationBar")):
                return f"id:{rid}|{cls}"
            if txt and len(txt) >= 2 and not txt.startswith("%"):  # skip format-string placeholders
                return f"tx:{txt}|{cls}"
            return None

        def walk(n: dict) -> None:
            k = stable_key(n)
            b = n.get("bounds")
            if k and b and isinstance(b, list) and len(b) == 4:
                anchors.setdefault(k, b[1])  # first occurrence wins (stable)
            for c in n.get("children", []):
                walk(c)
        for c in hierarchy.get("children", []):
            walk(c)
        return anchors

    def _median_anchor_delta(self, prev: dict, now: dict) -> Optional[int]:
        """Median of (prev_y - now_y) over keys present in both. Positive = scrolled down."""
        shared = set(prev) & set(now)
        if not shared:
            return None
        deltas = sorted(prev[k] - now[k] for k in shared)
        # Drop outliers from the tails (animations, status bar etc.)
        m = len(deltas)
        if m >= 5:
            deltas = deltas[m // 5: -(m // 5)]
        if not deltas:
            return None
        return deltas[len(deltas) // 2]

    def _stitch_images(self, image_paths: list[Path], offsets: list[int], viewport_h: int):
        """Stitch screenshots vertically using cumulative offsets.

        For each step i>0, only the bottom (offsets[i] - offsets[i-1]) pixels of
        the new screenshot are NEW content (the top portion overlaps the previous
        screenshot's bottom). We crop and concatenate accordingly.
        """
        from PIL import Image  # already verified
        images = [Image.open(str(p)).convert("RGB") for p in image_paths]
        w = images[0].width
        total_h = viewport_h
        for i in range(1, len(images)):
            total_h += offsets[i] - offsets[i - 1]
        canvas = Image.new("RGB", (w, total_h), (0, 0, 0))
        canvas.paste(images[0], (0, 0))
        cursor = viewport_h
        for i in range(1, len(images)):
            delta = offsets[i] - offsets[i - 1]
            # The bottom `delta` rows of images[i] are the new content
            new_part = images[i].crop((0, viewport_h - delta, w, viewport_h))
            canvas.paste(new_part, (0, cursor))
            cursor += delta
        return canvas

    def _check_stitch_quality(self, unified: dict, page_total_height: int) -> list[dict]:
        """Walk the unified hierarchy and flag nodes whose bottom y exceeds
        the stitched canvas. Returns a list of warning records (empty when
        everything fits). Caller writes them to scroll_meta.json.
        """
        warnings: list[dict] = []
        # Small slack absorbs single-pixel rounding from anchor median math.
        max_allowed = page_total_height + 2

        def walk(n: dict, path: str) -> None:
            b = n.get("bounds")
            if isinstance(b, list) and len(b) == 4 and b[3] > max_allowed:
                warnings.append({
                    "path": path,
                    "kind": n.get("kind", "view"),
                    "id": n.get("id"),
                    "text": (n.get("text") or "")[:40],
                    "bottom": b[3],
                    "max_allowed": max_allowed,
                    "overflow_px": b[3] - max_allowed,
                })
            for i, c in enumerate(n.get("children") or []):
                walk(c, f"{path}/{i}")

        for i, c in enumerate(unified.get("children") or []):
            walk(c, str(i))
        return warnings

    def _union_hierarchies(self, steps: list[dict], viewport_h: int) -> dict:
        """Union per-step hierarchies into one tree with global (full-page) bounds.

        Strategy: start with step 0's tree (verbatim — viewport y == global y).
        For each later step, walk its tree and for each node whose stable key is
        NOT present in the merged set, append a copy with bounds translated by
        the step's scroll offset.

        The translated nodes get attached to the *deepest scrollable ancestor* in
        the merged tree, which keeps the tree well-formed and groups the new
        items under the same container they actually live in.
        """
        merged = self._deep_copy(steps[0]["hierarchy"])
        seen_keys: set[str] = set()
        self._collect_keys(merged, seen_keys)

        # Find the scrollable target container in merged tree (deepest match)
        scroll_target = self._find_scrollable(merged) or merged

        for step in steps[1:]:
            offset = step["offset"]
            new_nodes: list[dict] = []
            self._collect_translated_new(step["hierarchy"], offset, seen_keys, new_nodes)
            if new_nodes:
                scroll_target.setdefault("children", []).extend(new_nodes)

        return merged

    @staticmethod
    def _deep_copy(d):
        import copy
        return copy.deepcopy(d)

    def _collect_keys(self, node: dict, out: set[str]) -> None:
        k = self._node_stable_key(node)
        if k:
            out.add(k)
        for c in node.get("children", []):
            self._collect_keys(c, out)

    def _collect_translated_new(self, node: dict, offset: int,
                                seen: set[str], out: list[dict]) -> None:
        k = self._node_stable_key(node)
        if k and k not in seen:
            # Take this whole subtree, translate, mark all keys as seen
            translated = self._translate_subtree(node, offset)
            self._collect_keys(translated, seen)
            out.append(translated)
            return  # don't descend further; subtree already taken
        for c in node.get("children", []):
            self._collect_translated_new(c, offset, seen, out)

    def _translate_subtree(self, node: dict, offset: int) -> dict:
        new = {k: v for k, v in node.items() if k != "children"}
        b = node.get("bounds")
        if b and isinstance(b, list) and len(b) == 4:
            new["bounds"] = [b[0], b[1] + offset, b[2], b[3] + offset]
        new["children"] = [self._translate_subtree(c, offset) for c in node.get("children", [])]
        return new

    def _node_stable_key(self, n: dict) -> Optional[str]:
        rid = n.get("id") or ""
        txt = (n.get("text") or "").strip()
        cls = n.get("class", "")
        if rid:
            return f"id:{rid}|{cls}"
        if txt:
            return f"tx:{txt}|{cls}"
        return None

    # ============================================================ resources

    # Regex for resource references in source code: `R.layout.foo`, `R.drawable.bar`, etc.
    # `xml` covers preferences and other arbitrary XML resources (res/xml/*.xml);
    # `array` covers <string-array>/<integer-array> definitions used by ListPreference
    # entries etc.
    _R_REF_RE = re.compile(r"\bR\.(?P<kind>layout|xml|drawable|mipmap|string|color|dimen|style|id|array)\.(?P<name>\w+)")
    # Regex for ViewBinding class usages: `FragmentTodayWeatherBinding.inflate(...)`
    _VIEWBINDING_RE = re.compile(r"\b([A-Z][A-Za-z0-9]+)Binding(?:Impl)?\b")
    # Layout/Preference XML references: @drawable/foo, @string/bar, @array/baz, etc.
    _RES_REF_IN_XML_RE = re.compile(r"@(?P<kind>drawable|mipmap|string|color|style|dimen|array)/(?P<name>[a-zA-Z0-9_.]+)")

    # Density buckets to search for raster drawables, highest density first. We copy
    # only the highest available — Worker doesn't need the full density ladder.
    _RASTER_DENSITY_DIRS = (
        "drawable-xxxhdpi", "drawable-xxhdpi", "drawable-xhdpi",
        "drawable-hdpi", "drawable-mdpi", "drawable",
    )
    _MIPMAP_DENSITY_DIRS = (
        "mipmap-xxxhdpi", "mipmap-xxhdpi", "mipmap-xhdpi",
        "mipmap-hdpi", "mipmap-mdpi", "mipmap-anydpi-v26", "mipmap",
    )
    _RASTER_EXTS = (".png", ".webp", ".jpg", ".jpeg", ".9.png")

    def extract_resources(
        self,
        source_file: Optional[str],
        source_root: Path,
        out_dir: Path,
    ) -> Optional[dict]:
        """Find and copy resources referenced by an Android page.

        Strategy:
          1. Read the Fragment / Activity source file.
          2. Find R.layout.X refs (code) AND ViewBinding class usages → set of layout names.
          3. Copy each layout XML to resources/layouts/, then parse its XML for further
             @drawable / @mipmap / @string / @color refs.
          4. Find R.drawable.X / R.mipmap.X / R.string.X / R.color.X refs in code → add.
          5. For each drawable / mipmap: resolve to a file under res/ (preferring vector
             XMLs and highest-density raster) and copy.
          6. For each string / color: look up its value in res/values/*.xml and record.
          7. Write resources_manifest.json with full provenance, including a `policy`
             note documenting that this is a NON-RECURSIVE direct-reference dump:
             selectors/shapes that reference further drawables are not auto-followed —
             Worker must use `copied_from` to find transitive deps in the source tree.
        """
        if not source_file:
            return None
        src_path = (source_root / source_file).resolve() if not Path(source_file).is_absolute() else Path(source_file)
        if not src_path.exists():
            return None

        res_dir = out_dir / "resources"
        # Idempotent: wipe prior extraction so stale resources don't accumulate.
        if res_dir.exists():
            shutil.rmtree(res_dir)
        (res_dir / "layouts").mkdir(parents=True, exist_ok=True)
        (res_dir / "drawables").mkdir(parents=True, exist_ok=True)
        (res_dir / "mipmaps").mkdir(parents=True, exist_ok=True)
        (res_dir / "xml").mkdir(parents=True, exist_ok=True)

        try:
            code_text = src_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None

        # Collect references
        layouts: set[str] = set()
        xmls: set[str] = set()             # res/xml/*.xml (preferences, scenes, etc.)
        drawables: set[str] = set()
        mipmaps: set[str] = set()
        strings_ref: set[str] = set()
        colors_ref: set[str] = set()
        arrays_ref: set[str] = set()

        # Pass 1: explicit R.X.Y in source code
        for m in self._R_REF_RE.finditer(code_text):
            kind, name = m.group("kind"), m.group("name")
            if kind == "layout":
                layouts.add(name)
            elif kind == "xml":
                xmls.add(name)
            elif kind == "drawable":
                drawables.add(name)
            elif kind == "mipmap":
                mipmaps.add(name)
            elif kind == "string":
                strings_ref.add(name)
            elif kind == "color":
                colors_ref.add(name)
            elif kind == "array":
                arrays_ref.add(name)

        # Pass 2: ViewBinding class names → derived layout name
        for m in self._VIEWBINDING_RE.finditer(code_text):
            layouts.add(self._camel_to_snake(m.group(1)))

        # Locate the Android module's res/ root. Most Android projects place it at
        # <module>/src/main/res; walk up from source_file until we find one.
        res_root = self._find_res_root(src_path, source_root)

        ref_map: dict[str, list[str]] = {}  # for manifest "referenced_by" tracking

        def note_ref(res_name: str, where: str) -> None:
            ref_map.setdefault(res_name, []).append(where)

        def parse_xml_for_refs(xml_path: Path, where: str) -> None:
            """Parse @drawable/@string/@color/@array refs from a layout / xml resource."""
            try:
                xml_text = xml_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                return
            for m in self._RES_REF_IN_XML_RE.finditer(xml_text):
                kind, name = m.group("kind"), m.group("name")
                if kind == "drawable":
                    drawables.add(name); note_ref(f"drawable/{name}", where)
                elif kind == "mipmap":
                    mipmaps.add(name); note_ref(f"mipmap/{name}", where)
                elif kind == "string":
                    strings_ref.add(name); note_ref(f"string/{name}", where)
                elif kind == "color":
                    colors_ref.add(name); note_ref(f"color/{name}", where)
                elif kind == "array":
                    arrays_ref.add(name); note_ref(f"array/{name}", where)

        # Pass 3a: copy each referenced layout and parse it for transitive refs
        copied_layouts: list[dict] = []
        if res_root:
            for layout_name in sorted(layouts):
                layout_path = self._find_layout_file(res_root, layout_name)
                if not layout_path:
                    continue
                target = res_dir / "layouts" / layout_path.name
                shutil.copyfile(layout_path, target)
                copied_layouts.append({
                    "name": layout_name,
                    "copied_from": self._rel_to_source(layout_path, source_root),
                    "local_path": target.relative_to(res_dir).as_posix(),
                })
                note_ref(f"layout/{layout_name}", str(src_path.name))
                parse_xml_for_refs(layout_path, f"layout/{layout_name}")

        # Pass 3b: copy each referenced res/xml/*.xml (preferences, scenes, etc.)
        # and parse it for transitive refs the same way as layouts.
        copied_xmls: list[dict] = []
        if res_root:
            for xml_name in sorted(xmls):
                xml_path = self._find_xml_resource(res_root, xml_name)
                if not xml_path:
                    continue
                target = res_dir / "xml" / xml_path.name
                shutil.copyfile(xml_path, target)
                copied_xmls.append({
                    "name": xml_name,
                    "copied_from": self._rel_to_source(xml_path, source_root),
                    "local_path": target.relative_to(res_dir).as_posix(),
                })
                note_ref(f"xml/{xml_name}", str(src_path.name))
                parse_xml_for_refs(xml_path, f"xml/{xml_name}")

        # Pass 4: copy drawables (vector XML preferred, else highest-density raster).
        # Selectors/shapes/layer-lists/ripples may reference further drawables; we
        # follow those one level (depth ≤ 2 total) in Pass 4b to satisfy R2.3
        # without risking runaway recursion.
        copied_drawables: list[dict] = []
        unresolved_drawables: list[str] = []
        inner_ref_drawables: set[str] = set()  # names to resolve in Pass 4b
        if res_root:
            for name in sorted(drawables):
                resolved = self._resolve_drawable(res_root, name)
                if not resolved:
                    unresolved_drawables.append(name)
                    continue
                target = res_dir / "drawables" / resolved.name
                shutil.copyfile(resolved, target)
                entry = {
                    "name": name,
                    "format": ("vector_xml" if resolved.suffix == ".xml" else "raster"),
                    "copied_from": self._rel_to_source(resolved, source_root),
                    "local_path": target.relative_to(res_dir).as_posix(),
                    "referenced_by": sorted(set(ref_map.get(f"drawable/{name}", []))),
                }
                # Scan composite drawable XMLs (selector/layer-list/shape/ripple)
                # for inner refs. Drawable refs queue for Pass 4b; color refs
                # piggyback on Pass 6's @color resolver.
                if resolved.suffix == ".xml":
                    try:
                        xml_text = resolved.read_text(encoding="utf-8", errors="ignore")
                        if any(tag in xml_text for tag in ("<selector", "<layer-list", "<shape", "<ripple")):
                            entry["has_inner_refs"] = True
                            inner: list[str] = []
                            for m in self._RES_REF_IN_XML_RE.finditer(xml_text):
                                kind, ref_name = m.group("kind"), m.group("name")
                                if kind == "drawable" and ref_name not in drawables:
                                    inner_ref_drawables.add(ref_name)
                                    inner.append(f"drawable/{ref_name}")
                                    note_ref(f"drawable/{ref_name}", f"inner_ref:drawable/{name}")
                                elif kind == "color" and ref_name not in colors_ref:
                                    colors_ref.add(ref_name)
                                    inner.append(f"color/{ref_name}")
                                    note_ref(f"color/{ref_name}", f"inner_ref:drawable/{name}")
                                elif kind == "mipmap" and ref_name not in mipmaps:
                                    mipmaps.add(ref_name)
                                    inner.append(f"mipmap/{ref_name}")
                                    note_ref(f"mipmap/{ref_name}", f"inner_ref:drawable/{name}")
                            if inner:
                                entry["inner_refs"] = sorted(set(inner))
                    except OSError:
                        pass
                copied_drawables.append(entry)

        # Pass 4b: resolve drawables discovered via inner refs (depth 2). No
        # further recursion — if these are themselves composite, they keep
        # `has_inner_refs: true` but we stop following to avoid loops.
        if res_root and inner_ref_drawables:
            already_copied = {e["name"] for e in copied_drawables}
            for name in sorted(inner_ref_drawables - already_copied):
                resolved = self._resolve_drawable(res_root, name)
                if not resolved:
                    unresolved_drawables.append(name)
                    continue
                target = res_dir / "drawables" / resolved.name
                if not target.exists():
                    shutil.copyfile(resolved, target)
                entry = {
                    "name": name,
                    "format": ("vector_xml" if resolved.suffix == ".xml" else "raster"),
                    "copied_from": self._rel_to_source(resolved, source_root),
                    "local_path": target.relative_to(res_dir).as_posix(),
                    "referenced_by": sorted(set(ref_map.get(f"drawable/{name}", []))),
                    "via_inner_ref": True,
                }
                if resolved.suffix == ".xml":
                    try:
                        xml_text = resolved.read_text(encoding="utf-8", errors="ignore")
                        if any(tag in xml_text for tag in ("<selector", "<layer-list", "<shape", "<ripple")):
                            entry["has_inner_refs"] = True
                            entry["worker_note"] = (
                                "Depth-2 drawable: this was pulled in by a parent selector/shape, "
                                "but its own inner refs (if any) were NOT followed. Inspect "
                                "`copied_from`'s parent directory for additional drawables."
                            )
                    except OSError:
                        pass
                copied_drawables.append(entry)

        # Pass 5: copy mipmaps
        copied_mipmaps: list[dict] = []
        unresolved_mipmaps: list[str] = []
        if res_root:
            for name in sorted(mipmaps):
                resolved = self._resolve_mipmap(res_root, name)
                if not resolved:
                    unresolved_mipmaps.append(name)
                    continue
                target = res_dir / "mipmaps" / resolved.name
                shutil.copyfile(resolved, target)
                copied_mipmaps.append({
                    "name": name,
                    "format": ("vector_xml" if resolved.suffix == ".xml" else "raster"),
                    "copied_from": self._rel_to_source(resolved, source_root),
                    "local_path": target.relative_to(res_dir).as_posix(),
                    "referenced_by": sorted(set(ref_map.get(f"mipmap/{name}", []))),
                })

        # Pass 6: look up string + color + array values. We follow @string/@color
        # refs that appear INSIDE resolved values (e.g. array items that are string
        # refs, color aliases) — this is an in-memory lookup, not a file copy, so it
        # doesn't violate the "no recursive file copy" policy.
        resolved_strings: dict[str, str] = {}
        resolved_colors: dict[str, str] = {}
        resolved_arrays: dict[str, list[str]] = {}
        if res_root:
            # Fixed-point: keep resolving until no new refs are pulled in. Bounded by
            # the total number of resources in res/values, so cheap and safe.
            for _ in range(8):  # paranoid upper bound; real apps converge in 1-2 passes
                new_s = self._resolve_values_resources(res_root, "string", strings_ref - set(resolved_strings))
                new_c = self._resolve_values_resources(res_root, "color", colors_ref - set(resolved_colors))
                new_a = self._resolve_array_resources(res_root, arrays_ref - set(resolved_arrays))
                if not (new_s or new_c or new_a):
                    break
                resolved_strings.update(new_s)
                resolved_colors.update(new_c)
                resolved_arrays.update(new_a)
                # Scan freshly-resolved values for additional @string/@color/@array refs.
                for v in list(new_s.values()) + list(new_c.values()):
                    for m in self._RES_REF_IN_XML_RE.finditer(v or ""):
                        kind, name = m.group("kind"), m.group("name")
                        if kind == "string":
                            strings_ref.add(name); note_ref(f"string/{name}", "string-value")
                        elif kind == "color":
                            colors_ref.add(name); note_ref(f"color/{name}", "color-value")
                        elif kind == "array":
                            arrays_ref.add(name); note_ref(f"array/{name}", "value-ref")
                for items in new_a.values():
                    for item in items:
                        for m in self._RES_REF_IN_XML_RE.finditer(item or ""):
                            kind, name = m.group("kind"), m.group("name")
                            if kind == "string":
                                strings_ref.add(name); note_ref(f"string/{name}", "array-item")
                            elif kind == "color":
                                colors_ref.add(name); note_ref(f"color/{name}", "array-item")

        if resolved_strings:
            strings_txt = res_dir / "strings.txt"
            strings_txt.write_text(
                "\n".join(f"{k}\t{v}" for k, v in sorted(resolved_strings.items())) + "\n",
                encoding="utf-8",
            )
        if resolved_colors:
            colors_txt = res_dir / "colors.txt"
            colors_txt.write_text(
                "\n".join(f"{k}\t{v}" for k, v in sorted(resolved_colors.items())) + "\n",
                encoding="utf-8",
            )
        if resolved_arrays:
            arrays_txt = res_dir / "arrays.txt"
            lines = []
            for k, vals in sorted(resolved_arrays.items()):
                lines.append(f"[{k}]")
                for v in vals:
                    lines.append(f"  {v}")
            arrays_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")

        manifest = {
            "page_source_file": self._rel_to_source(src_path, source_root),
            "res_root": (self._rel_to_source(res_root, source_root) if res_root else None),
            "policy": "depth_2_inner_ref_follow",
            "policy_note": (
                "This manifest lists resources directly referenced by the page's source "
                "code and its layout XMLs, PLUS one level of follow into composite "
                "drawables (selectors / layer-lists / shapes / ripples). Drawables added "
                "by the depth-2 follow are tagged `via_inner_ref: true`. Anything beyond "
                "depth 2 is still NOT followed: if a `via_inner_ref` drawable has "
                "`has_inner_refs: true`, inspect its `copied_from` parent directory to "
                "pick up further refs manually."
            ),
            "worker_notes": [
                "When implementing the target Cangjie/ArkUI page, prefer using the "
                "exact assets in resources/drawables/ and resources/mipmaps/ — they "
                "are the SOURCE app's visual identity. Don't substitute placeholders.",
                "resources/layouts/<name>.xml is the source layout — use it as the "
                "primary structural reference; the captured screenshot is the visual.",
                "Each resource entry has a `referenced_by` field — if you need to know "
                "which view/widget uses this asset, that's where to look.",
                "Drawables with `via_inner_ref: true` were pulled in automatically "
                "from a parent selector/shape/layer-list (depth-2 follow). If one of "
                "those still has `has_inner_refs: true`, its own inner refs were NOT "
                "followed — inspect `copied_from`'s parent directory manually.",
            ],
            "layouts": copied_layouts,
            "xml_resources": copied_xmls,
            "drawables": copied_drawables,
            "mipmaps": copied_mipmaps,
            "strings": resolved_strings,
            "colors": resolved_colors,
            "arrays": resolved_arrays,
            "unresolved": {
                "drawables": unresolved_drawables,
                "mipmaps": unresolved_mipmaps,
                "strings": sorted(strings_ref - set(resolved_strings)),
                "colors": sorted(colors_ref - set(resolved_colors)),
                "arrays": sorted(arrays_ref - set(resolved_arrays)),
            },
            "stats": {
                "layouts": len(copied_layouts),
                "xml_resources": len(copied_xmls),
                "drawables_copied": len(copied_drawables),
                "mipmaps_copied": len(copied_mipmaps),
                "strings_resolved": len(resolved_strings),
                "colors_resolved": len(resolved_colors),
                "arrays_resolved": len(resolved_arrays),
            },
        }
        (res_dir / "resources_manifest.json").write_text(
            __import__("json").dumps(manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return manifest

    # ---------------------------------------------------- resource helpers

    @staticmethod
    def _camel_to_snake(name: str) -> str:
        # e.g. FragmentTodayWeather -> fragment_today_weather
        s = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
        return s

    def _find_res_root(self, src_path: Path, source_root: Path) -> Optional[Path]:
        """Walk up from src_path until we find a sibling `res/` directory.

        Typical layout: app/src/main/java/.../Foo.java with res at app/src/main/res/.
        We walk up until we find a directory containing both `java` (or `kotlin`)
        and `res` as children.
        """
        for parent in src_path.parents:
            if parent == source_root.parent:
                break
            if (parent / "res").is_dir() and any((parent / d).is_dir() for d in ("java", "kotlin")):
                return parent / "res"
            if parent == source_root:
                # Last-ditch: check well-known location
                candidate = source_root / "app" / "src" / "main" / "res"
                if candidate.is_dir():
                    return candidate
                break
        return None

    def _find_layout_file(self, res_root: Path, name: str) -> Optional[Path]:
        # Layouts live in res/layout*/<name>.xml; usually res/layout, sometimes
        # variant folders (layout-land, layout-v21). Prefer the plain `layout/`.
        for sub in res_root.glob("layout*"):
            f = sub / f"{name}.xml"
            if f.exists():
                return f
        return None

    def _find_xml_resource(self, res_root: Path, name: str) -> Optional[Path]:
        # res/xml/<name>.xml — used for PreferenceScreen definitions, motion scenes,
        # network security configs, etc.
        for sub in sorted(res_root.glob("xml*"), key=lambda p: 0 if p.name == "xml" else 1):
            f = sub / f"{name}.xml"
            if f.exists():
                return f
        return None

    def _resolve_array_resources(
        self, res_root: Path, names: set[str],
    ) -> dict[str, list[str]]:
        """Look up <string-array>/<integer-array>/<array> entries from res/values*/*.xml.

        Returns {name: [item1, item2, ...]}. Item text comes from each child element.
        """
        if not names:
            return {}
        out: dict[str, list[str]] = {}
        candidate_files: list[Path] = []
        for sub in sorted(res_root.glob("values*"), key=lambda p: 0 if p.name == "values" else 1):
            candidate_files.extend(sub.glob("*.xml"))
        for xml_path in candidate_files:
            if not names - set(out):
                break
            try:
                tree = ET.parse(xml_path)
            except ET.ParseError:
                continue
            for el in tree.iter():
                tag = el.tag.split("}")[-1]
                if tag not in ("string-array", "integer-array", "array"):
                    continue
                n = el.attrib.get("name")
                if n and n in names and n not in out:
                    items = []
                    for child in el:
                        items.append("".join(child.itertext()).strip())
                    out[n] = items
        return out

    def _resolve_drawable(self, res_root: Path, name: str) -> Optional[Path]:
        # XML drawables (vectors / selectors / shapes) live in res/drawable*/
        # Raster lives in res/drawable-<density>/. Prefer XML, else highest density.
        # XML first: any res/drawable*/`name`.xml
        for sub in sorted(res_root.glob("drawable*"), key=lambda p: 0 if p.name == "drawable" else 1):
            f = sub / f"{name}.xml"
            if f.exists():
                return f
        # Raster by density
        for density in self._RASTER_DENSITY_DIRS:
            sub = res_root / density
            if not sub.is_dir():
                continue
            for ext in self._RASTER_EXTS:
                f = sub / f"{name}{ext}"
                if f.exists():
                    return f
        return None

    def _resolve_mipmap(self, res_root: Path, name: str) -> Optional[Path]:
        # Same idea but in mipmap-*/. Adaptive icons may use anydpi-v26 with an XML.
        for sub in sorted(res_root.glob("mipmap-anydpi*")) + sorted(res_root.glob("mipmap")):
            f = sub / f"{name}.xml"
            if f.exists():
                return f
        for density in self._MIPMAP_DENSITY_DIRS:
            sub = res_root / density
            if not sub.is_dir():
                continue
            for ext in self._RASTER_EXTS:
                f = sub / f"{name}{ext}"
                if f.exists():
                    return f
        return None

    def _resolve_values_resources(
        self, res_root: Path, tag: str, names: set[str],
    ) -> dict[str, str]:
        """Look up <tag name="X">value</tag> entries from res/values*/*.xml.

        Uses the first occurrence found. Doesn't try to merge variants (values-night,
        values-v21, etc.) — Worker can find variants via the policy_note.
        """
        if not names:
            return {}
        out: dict[str, str] = {}
        # Prefer plain `values/` first
        candidate_files: list[Path] = []
        for sub in sorted(res_root.glob("values*"), key=lambda p: 0 if p.name == "values" else 1):
            candidate_files.extend(sub.glob("*.xml"))
        for xml_path in candidate_files:
            if not names - set(out):
                break
            try:
                tree = ET.parse(xml_path)
            except ET.ParseError:
                continue
            for el in tree.iter(tag):
                n = el.attrib.get("name")
                if n and n in names and n not in out:
                    text = "".join(el.itertext()).strip()
                    out[n] = text
        return out

    @staticmethod
    def _rel_to_source(path: Path, source_root: Path) -> str:
        try:
            return path.resolve().relative_to(source_root.resolve()).as_posix()
        except ValueError:
            return path.as_posix()

    # ============================================================ end resources

    def _find_scrollable(self, hierarchy: dict) -> Optional[dict]:
        """Locate the deepest scrollable container in the tree."""
        best: Optional[dict] = None
        best_depth = -1

        def walk(n: dict, depth: int) -> None:
            nonlocal best, best_depth
            flags = n.get("flags") or []
            cls = n.get("class", "")
            if "scrollable" in flags or any(s in cls for s in
                ("ScrollView", "RecyclerView", "NestedScrollView")):
                if depth > best_depth:
                    best = n
                    best_depth = depth
            for c in n.get("children", []):
                walk(c, depth + 1)
        for c in hierarchy.get("children", []):
            walk(c, 1)
        return best

    # ------------------------------------------------------------- normalize

    def normalize(self, raw_path: Path) -> dict:
        """uiautomator XML → UI-IR.

        UI-IR is a platform-agnostic tree:
          {kind, class, id, text, content_desc, bounds: [l,t,r,b], children: [...]}
        Only fields present in the source are emitted; missing fields are omitted, not nulled.
        """
        tree = ET.parse(raw_path)
        root = tree.getroot()
        # uiautomator wraps in <hierarchy> with a single <node> root child
        nodes = list(root)
        if not nodes:
            return {"kind": "root", "children": []}
        return {
            "kind": "root",
            "platform": "android",
            "children": [self._uia_to_ir(n) for n in nodes],
        }

    def _uia_to_ir(self, node) -> dict:
        attr = node.attrib
        cls = attr.get("class", "")
        out: dict = {
            "kind": _cls_to_kind(cls),
            "class": cls,
        }
        for src, dst in (
            ("resource-id", "id"),
            ("text", "text"),
            ("content-desc", "content_desc"),
            ("package", "package"),
        ):
            v = attr.get(src)
            if v:
                out[dst] = v
        bounds = attr.get("bounds")
        if bounds:
            parsed = _parse_bounds(bounds)
            if parsed:
                out["bounds"] = parsed

        # Structured state: capture BOTH true and false so diff can detect
        # transitions like "enabled was true, now false". The legacy `flags`
        # array below only records true-valued attrs, which is lossy.
        state: dict = {}
        for field in ("enabled", "checked", "checkable", "clickable", "long-clickable",
                      "focused", "focusable", "selected", "scrollable", "password"):
            v = attr.get(field)
            if v in ("true", "false"):
                state[field.replace("-", "_")] = (v == "true")
        if state:
            out["state"] = state

        # Legacy flags array — kept for back-compat with existing consumers /
        # tests. Listed entries are inherently lossy (false = absent), prefer
        # `state` for new code.
        for flag in ("clickable", "scrollable", "checkable", "checked", "enabled",
                     "focused", "selected", "long-clickable", "password"):
            v = attr.get(flag)
            if v == "true":
                out.setdefault("flags", []).append(flag)

        children = [self._uia_to_ir(c) for c in node]
        if children:
            out["children"] = children
        return out


# ----------------------------------------------------------------- helpers

def _short_class(fqcn: str) -> str:
    """Return short class name from a possibly-fully-qualified `pkg.path.Class$Inner`."""
    return fqcn.rsplit(".", 1)[-1].split("$", 1)[0]


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()
    # Strip common suffixes for cleaner ids
    for suf in ("_fragment", "_activity", "_view_controller"):
        if s.endswith(suf):
            s = s[: -len(suf)]
            break
    return s or "page"


def _relpath(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


_SKIP_DIRS = frozenset({".git", "build", "node_modules", ".gradle", ".idea", ".cxx", "intermediates", "generated"})


def _in_skipped_dir(p: Path) -> bool:
    return any(part in _SKIP_DIRS for part in p.parts)


def _iter_source_files(root: Path, suffixes: tuple[str, ...]):
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if _in_skipped_dir(p):
            continue
        if p.suffix in suffixes:
            yield p


_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def _parse_bounds(value: str) -> Optional[list[int]]:
    m = _BOUNDS_RE.search(value)
    if not m:
        return None
    return [int(g) for g in m.groups()]


def _cls_to_kind(cls: str) -> str:
    short = cls.rsplit(".", 1)[-1]
    mapping = {
        # Containers
        "FrameLayout": "frame",
        "LinearLayout": "linear",
        "RelativeLayout": "relative",
        "ConstraintLayout": "constraint",
        "CardView": "card",
        # Lists & pagers
        "RecyclerView": "list",
        "ListView": "list",
        "GridView": "list",
        "ViewPager": "pager",
        "ViewPager2": "pager",
        # Scroll
        "ScrollView": "scroll",
        "NestedScrollView": "scroll",
        "HorizontalScrollView": "scroll",
        # Text
        "TextView": "text",
        "AppCompatTextView": "text",
        "MaterialTextView": "text",
        # Images
        "ImageView": "image",
        "AppCompatImageView": "image",
        "ShapeableImageView": "image",
        # Buttons
        "Button": "button",
        "AppCompatButton": "button",
        "MaterialButton": "button",
        "ImageButton": "button",
        "AppCompatImageButton": "button",
        "FloatingActionButton": "button",
        # Inputs
        "EditText": "input",
        "AppCompatEditText": "input",
        "TextInputEditText": "input",
        "TextInputLayout": "input",
        # Toggles & selectors
        "Switch": "switch",
        "SwitchCompat": "switch",
        "SwitchMaterial": "switch",
        "CheckBox": "checkbox",
        "AppCompatCheckBox": "checkbox",
        "MaterialCheckBox": "checkbox",
        "RadioButton": "radio",
        "AppCompatRadioButton": "radio",
        "MaterialRadioButton": "radio",
        "RadioGroup": "radio_group",
        # Sliders & progress
        "SeekBar": "slider",
        "AppCompatSeekBar": "slider",
        "Slider": "slider",
        "RangeSlider": "slider",
        "ProgressBar": "progress",
        "ContentLoadingProgressBar": "progress",
        # Dropdowns
        "Spinner": "dropdown",
        "AppCompatSpinner": "dropdown",
        "AutoCompleteTextView": "dropdown",
        "MaterialAutoCompleteTextView": "dropdown",
        # Web / media
        "WebView": "web",
        "VideoView": "media",
        "SurfaceView": "media",
        "TextureView": "media",
        # Material chrome
        "TabLayout": "tabs",
        "BottomNavigationView": "bottom_nav",
        "NavigationView": "nav_drawer",
        "Toolbar": "toolbar",
        "MaterialToolbar": "toolbar",
        "ActionBar": "toolbar",
        "AppBarLayout": "appbar",
        "CollapsingToolbarLayout": "appbar",
        "BottomSheetDialog": "sheet",
        "BottomSheetBehavior": "sheet",
        "Chip": "chip",
        "ChipGroup": "chip_group",
    }
    return mapping.get(short, "view")
