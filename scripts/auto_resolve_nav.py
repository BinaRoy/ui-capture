#!/usr/bin/env python3
"""Auto-resolve nav-script tap coordinates from Android source.

For each PageHint that has no obvious nav script entry yet, this module
inspects the input source tree to find:
  - which class instantiates / shows the hint's Fragment / Activity
  - which `View` id (`R.id.foo`) triggers that instantiation
  - the resolved trigger metadata (file, method, view_id)

It does NOT itself drive an emulator or call adb. Instead it emits a
`nav_resolution.json` (next to the nav script) so the scaffold step can
embed the resolved view ids into the TODO blocks. A second-pass live
resolver (separate module) consumes the live `uiautomator dump` and the
resolution JSON to fill `input tap X Y` lines.

Splitting static analysis from live coord matching lets the static part
run without an emulator, keeps the surface small, and makes the live
matcher (which needs adb) a thin wrapper.

Generic across Android apps: relies only on `R.id.*`, listener install
patterns, FragmentTransaction.replace/add, and standard nav-graph action
IDs. No app-specific knowledge.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, Optional


# ----------------------------------------------------------------------- regex


# Listener install with R.id reference. Captures the id name and (optionally)
# the lambda / method ref body so we can scan for FragmentTransaction usage.
_LISTENER_INSTALL = re.compile(
    r"findViewById\s*\(\s*R\.id\.(\w+)\s*\)\s*\.\s*(setOnClickListener|setOnLongClickListener)\s*\(",
)

# Block-form listener with view id resolved earlier into a variable.
# e.g.  binding.searchIcon.setOnClickListener { ... }
_BINDING_LISTENER = re.compile(
    r"(?:binding|mBinding|viewBinding)\.(\w+)\s*\.\s*(setOnClickListener|setOnLongClickListener)",
)

# Direct `X.newInstance(...)`, `new X(...)`, FragmentTransaction.replace/add(R.id.container, new X(),...)
_FRAGMENT_INSTANTIATE = re.compile(
    r"(?:new\s+(\w+)\s*\(|(\w+)\s*\.\s*newInstance\s*\()",
)

# NavController action navigation (Compose Navigation / Jetpack)
_NAV_ACTION = re.compile(
    r"\.navigate\s*\(\s*R\.id\.(\w+)\s*[,)]",
)

# Nav graph destination → Fragment class
_NAVGRAPH_DEST = re.compile(
    r'<fragment\s+[^>]*android:id="@\+id/(\w+)"[^>]*android:name="([\w.$]+)"',
    re.MULTILINE | re.DOTALL,
)

# Nav graph action → destination
_NAVGRAPH_ACTION = re.compile(
    r'<action\s+[^>]*android:id="@\+id/(\w+)"[^>]*app:destination="@id/(\w+)"',
    re.MULTILINE | re.DOTALL,
)

# Compose `Modifier.clickable { ... navigate / show <Fragment-or-Composable> }`.
# We capture the `.clickable {` opening so we can walk forward and find a
# navigation call to the target composable. The trigger view-id concept does
# not apply to Compose (no R.id.*); for Compose hints we record the source
# location instead, and the live resolver falls back to text/content-desc.
_COMPOSE_CLICKABLE = re.compile(
    r"\.clickable\s*(?:\([^)]*\))?\s*\{",
)
_COMPOSE_NAVIGATE = re.compile(
    r"\.navigate\s*\(\s*\"([\w/]+)\"",
)
_COMPOSE_TEST_TAG = re.compile(
    r"\.testTag\s*\(\s*\"([\w.-]+)\"\s*\)",
)

# Cangjie / ArkUI NavPathStack navigation pattern.
# pushPathByName("PageName", param) — the page name IS the navigation key.
_CJ_PUSH_PATH = re.compile(
    r"pageStack\.pushPathByName\s*\(\s*\"(\w+)\"",
)
# Nearest .onClick({ evt => ... }) block before a pushPathByName call
_CJ_ON_CLICK = re.compile(r"\.onClick\s*\(\s*\{")
# Trigger label: nearest Text("...") / Button("...") / Image(...) literal
# wrapping the onClick. Used by the live matcher to find tap bounds.
_CJ_TRIGGER_LABEL = re.compile(r'(?:Text|Button)\s*\(\s*"([^"]{1,80})"')


@dataclass
class Resolution:
    hint_class: str
    trigger_method: Optional[str]
    trigger_file: Optional[str]
    view_id: Optional[str]  # R.id.foo (matched against resource-id="<pkg>:id/foo" in uiautomator dump)
    via: str  # "fragment_instantiate" | "navgraph_action" | "manifest_intent" | "compose_clickable" | "cangjie_router" | "unresolved"
    note: Optional[str] = None
    # Compose lookups produce no R.id but can still pin a testTag / content-desc
    # the live matcher uses as anchor. Empty for classic Android.
    test_tag: Optional[str] = None
    # Visible label of the trigger element (text wrapping the onClick that
    # invokes pushPathByName). Used by the live matcher to locate bounds in
    # the captured parent-page hierarchy. None when no nearby literal found.
    trigger_label: Optional[str] = None

    @property
    def resolved(self) -> bool:
        # Either a classic R.id, or a Compose test_tag is enough for the live
        # matcher to find tap coordinates.
        return bool(self.view_id) or bool(self.test_tag)


# --------------------------------------------------------------- nav-graph map


def _build_navgraph_action_map(source_root: Path) -> dict[str, dict[str, str]]:
    """Return {nav_xml_relpath: {action_id: destination_id}} and reverse."""
    out = {"actions": {}, "destinations": {}}
    for xml in source_root.rglob("res/navigation/*.xml"):
        try:
            text = xml.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for dest_id, dest_name in _NAVGRAPH_DEST.findall(text):
            out["destinations"][dest_id] = dest_name.rsplit(".", 1)[-1]
        for action_id, dest_id in _NAVGRAPH_ACTION.findall(text):
            out["actions"][action_id] = dest_id
    return out


def _walk_back_to_view_id(text: str, hit_index: int, window: int = 800) -> Optional[str]:
    """Given a position in source text where a fragment is instantiated /
    navigated to, walk back up to find the nearest setOnClickListener
    install — if any — and return its R.id name.

    Heuristic: the closest preceding listener install within `window` chars
    is almost always the click handler that wraps this navigation.
    """
    start = max(0, hit_index - window)
    chunk = text[start:hit_index]
    last_match: Optional[re.Match] = None
    for m in _LISTENER_INSTALL.finditer(chunk):
        last_match = m
    if last_match:
        return last_match.group(1)
    # Try binding-style listener
    last_bind: Optional[re.Match] = None
    for m in _BINDING_LISTENER.finditer(chunk):
        last_bind = m
    if last_bind:
        # ViewBinding generates field names verbatim from the XML @+id/<name>:
        # `binding.searchBtn` ↔ `@+id/searchBtn`, `binding.search_btn` ↔ `@+id/search_btn`.
        # The previous heuristic forced snake_case and broke camelCase layouts.
        return last_bind.group(1)
    return None


def _camel_to_snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


# --------------------------------------------------------------- resolver core


def resolve(hints: list, source_root: Path) -> dict[str, Resolution]:
    """Resolve each hint to a (view_id, trigger_method, trigger_file) tuple
    when possible. `hints` items must have `.class_name` and `.source_file`
    attributes (duck-typed from ui-capture.PageHint).

    Strategy plugins are tried in order; each is allowed to fail independently
    (a regex hiccup or unreadable file in one strategy does not block the
    others). Adding new patterns (e.g. ButterKnife @OnClick, DataBinding)
    means appending a new function to `_STRATEGIES` below.
    """
    nav_map = _build_navgraph_action_map(source_root)
    # Reverse: dest_id → class_name
    dest_to_class = nav_map.get("destinations", {})
    # Reverse: action_id → dest_id
    action_to_dest = nav_map.get("actions", {})

    # Index all .java / .kt for quick scan
    sources: list[tuple[Path, str]] = []
    for path in source_root.rglob("*"):
        if path.suffix not in (".java", ".kt"):
            continue
        if any(part in {"build", "generated", ".gradle"} for part in path.parts):
            continue
        try:
            sources.append((path, path.read_text(encoding="utf-8", errors="ignore")))
        except OSError:
            continue

    # Index all .cj files for Cangjie/ArkUI strategy
    cj_sources: list[tuple[Path, str]] = []
    _CJ_SKIP = {"build", "node_modules", ".hvigor", "oh_modules"}
    for path in source_root.rglob("*.cj"):
        if any(part in _CJ_SKIP for part in path.parts):
            continue
        try:
            cj_sources.append((path, path.read_text(encoding="utf-8", errors="ignore")))
        except OSError:
            continue

    ctx = {
        "source_root": source_root,
        "sources": sources,
        "cj_sources": cj_sources,
        "dest_to_class": dest_to_class,
        "action_to_dest": action_to_dest,
    }

    out: dict[str, Resolution] = {}
    for hint in hints:
        cls = hint.class_name
        result = Resolution(
            hint_class=cls,
            trigger_method=None,
            trigger_file=None,
            view_id=None,
            via="unresolved",
        )

        for strategy in _STRATEGIES:
            try:
                if strategy(hint, result, ctx):
                    break  # resolved
            except Exception as exc:
                # A misbehaving regex in one strategy must not block the others.
                # Record on the result so users see why a hint stayed unresolved.
                result.note = (result.note + "; " if result.note else "") + \
                              f"{strategy.__name__} skipped ({exc})"

        out[hint.slug] = result
    return out


# --------------------------------------------------------------- strategies
# Each strategy: (hint, result, ctx) -> bool (True if resolution complete).
# Mutate `result` in place. Return True only when result.resolved becomes True
# (so the caller knows to stop trying further strategies).


def _strategy_fragment_instantiate(hint, result: Resolution, ctx: dict) -> bool:
    """Scan for `new X(` or `X.newInstance(` and walk back to a click handler."""
    cls = hint.class_name
    for path, text in ctx["sources"]:
        for m in _FRAGMENT_INSTANTIATE.finditer(text):
            name = m.group(1) or m.group(2)
            if name != cls:
                continue
            vid = _walk_back_to_view_id(text, m.start())
            if vid:
                result.trigger_file = str(path.relative_to(ctx["source_root"]))
                result.trigger_method = _enclosing_method(text, m.start())
                result.view_id = vid
                result.via = "fragment_instantiate"
                return True
    return False


def _strategy_navgraph_action(hint, result: Resolution, ctx: dict) -> bool:
    """NavController.navigate(R.id.action_X) where action_X destination resolves to hint class."""
    cls = hint.class_name
    target_actions = {a for a, d in ctx["action_to_dest"].items()
                      if ctx["dest_to_class"].get(d) == cls}
    if not target_actions:
        return False
    for path, text in ctx["sources"]:
        for m in _NAV_ACTION.finditer(text):
            if m.group(1) not in target_actions:
                continue
            vid = _walk_back_to_view_id(text, m.start())
            if vid:
                result.trigger_file = str(path.relative_to(ctx["source_root"]))
                result.trigger_method = _enclosing_method(text, m.start())
                result.view_id = vid
                result.via = "navgraph_action"
                return True
    return False


def _strategy_compose_clickable(hint, result: Resolution, ctx: dict) -> bool:
    """Compose: find `.clickable { ... navigate("<route>") }` where the route
    matches the hint class (case-insensitive, with snake/camel tolerance), and
    pin the nearest `.testTag("...")` as the live-resolver anchor.

    Compose generates no R.id, so the live matcher needs a textual anchor
    (testTag or content-desc). When a testTag is present this resolution is
    "complete enough" to fill into nav script; otherwise we leave it as a hint
    with a note.
    """
    cls = hint.class_name
    cls_lower = cls.lower()
    cls_snake = _camel_to_snake(cls).lower()
    for path, text in ctx["sources"]:
        if "@Composable" not in text and "Composable" not in text:
            continue
        for m in _COMPOSE_NAVIGATE.finditer(text):
            route = m.group(1).lower().rstrip("/")
            route_last = route.rsplit("/", 1)[-1]
            if route_last not in (cls_lower, cls_snake) and \
               not route_last.startswith(cls_lower[:6]):
                continue
            # Walk back to nearest .clickable {
            chunk = text[:m.start()]
            clickable_match = None
            for cm in _COMPOSE_CLICKABLE.finditer(chunk):
                clickable_match = cm
            if not clickable_match:
                continue
            # Walk further back to nearest .testTag("...") within ~600 chars
            back_window = text[max(0, clickable_match.start() - 600):clickable_match.start()]
            tag_match = None
            for tm in _COMPOSE_TEST_TAG.finditer(back_window):
                tag_match = tm
            if tag_match:
                result.test_tag = tag_match.group(1)
                result.via = "compose_clickable"
            else:
                # Resolution incomplete but useful: record location for manual fill.
                result.via = "compose_clickable"
                result.note = (
                    "Compose .clickable found, but no .testTag anchor nearby — "
                    "live matcher must fall back to text/content-desc, or you "
                    "need to add a testTag to the source."
                )
            result.trigger_file = str(path.relative_to(ctx["source_root"]))
            result.trigger_method = _enclosing_method(text, clickable_match.start())
            return bool(result.test_tag)
    return False


def _strategy_cangjie_router(hint, result: Resolution, ctx: dict) -> bool:
    """Cangjie/ArkUI NavPathStack pattern:
       button.onClick({ evt => this.pageStack.pushPathByName("PageName", ...) })

    Finds the source file and enclosing method for each pushPathByName call.
    The page name string IS the navigation key — no R.id equivalent exists.
    Sets test_tag to the page name so the live matcher can confirm it.
    """
    cls = hint.class_name
    cj_sources = ctx.get("cj_sources", [])
    if not cj_sources:
        return False

    for path, text in cj_sources:
        for m in _CJ_PUSH_PATH.finditer(text):
            if m.group(1) != cls:
                continue
            # Walk back through the enclosing .onClick({ ... }) block to find
            # the Text("...") / Button("...") literal that hosts it. Prefer a
            # discriminating label over decorative arrows ('›' / '>' / '→'),
            # which are non-unique on settings-row parents.
            chunk = text[:m.start()]
            window = chunk[-800:]
            candidates = [cm.group(1) for cm in _CJ_TRIGGER_LABEL.finditer(window)]
            label: Optional[str] = None
            if candidates:
                arrows = {"›", ">", "→", "❯", "»", "›"}
                substantive = [c for c in candidates if c.strip() and c.strip() not in arrows and len(c.strip()) > 1]
                # Prefer the closest substantive label; fall back to the closest
                # arrow if that's all we have.
                label = (substantive[-1] if substantive else candidates[-1])
            result.trigger_file = str(path.relative_to(ctx["source_root"]))
            result.trigger_method = _enclosing_method(text, m.start())
            result.trigger_label = label
            result.via = "cangjie_router"
            result.test_tag = cls   # page name used in pushPathByName
            label_note = f' label="{label}"' if label else ""
            result.note = f"pushPathByName(\"{cls}\") in {path.name}{label_note}"
            return True  # test_tag set → resolved
    return False


def _strategy_conditional_render(hint, result: Resolution, ctx: dict) -> bool:
    """ArkUI/Cangjie conditional-render pages: rendered inline inside another
    @Component via `if/else { ChildPage() }`. No pushPathByName, no R.id.

    Mark them with the parent file as trigger_file so the future nav-graph
    builder can plan: "capture parent page → flip state → child appears".
    Does not set view_id/test_tag, so .resolved stays False (live matcher
    still needs the state-flip detail filled in by the nav-graph step).
    """
    if getattr(hint, "origin", None) != "conditional_render":
        return False
    # hint.note from discover_hints looks like:
    #   "page-suffix class instantiated inline at MainTabPage.cj (not in pageMap)"
    #   "pageMap entry rendered inline at index.cj"
    parent_file = None
    note = getattr(hint, "note", "") or ""
    m = re.search(r"inline at (\S+\.cj)", note)
    if m:
        parent_file = m.group(1)
    # Best-effort: find that file in cj_sources to make trigger_file absolute-ish
    if parent_file:
        for path, _ in ctx.get("cj_sources", []):
            if path.name == parent_file:
                result.trigger_file = str(path.relative_to(ctx["source_root"]))
                break
    result.via = "conditional_render"
    result.note = (
        f"conditional render inside {parent_file or 'unknown parent'}; "
        "nav-graph step must capture parent first and flip render state"
    )
    return False  # not auto-resolvable to a tap point


def _strategy_manifest_intent(hint, result: Resolution, ctx: dict) -> bool:
    """Launcher activities — declared in manifest, not opened from code."""
    if getattr(hint, "origin", None) != "manifest":
        return False
    result.via = "manifest_intent"
    result.note = "launcher activity — start via `adb shell am start -n <pkg>/<cls>`"
    # Not "resolved" in the view_id sense, so return False to allow other strategies
    # to add detail; but since manifest is the last strategy, this is moot.
    return False


_STRATEGIES = (
    _strategy_fragment_instantiate,
    _strategy_navgraph_action,
    _strategy_compose_clickable,
    _strategy_cangjie_router,
    _strategy_conditional_render,
    _strategy_manifest_intent,
)


def _enclosing_method(text: str, pos: int) -> Optional[str]:
    """Walk back to find the nearest method declaration enclosing `pos`.

    Supports:
      - Java:     `public void onClick(View v) {`
      - Kotlin:   `fun onClick(v: View) {` / `override fun ... (`
      - Cangjie:  `func onClick(): Unit {` / `public func ...(` / `private func ...(`
    """
    chunk = text[:pos]
    pat = re.compile(
        r"(?:public\s+|private\s+|protected\s+|override\s+)*"
        r"(?:fun|func|void)\s+(\w+)\s*\(",
        re.MULTILINE,
    )
    last: Optional[re.Match] = None
    for m in pat.finditer(chunk):
        last = m
    return last.group(1) if last else None


# ----------------------------------------------------------------- CLI / main


def _load_hints(manifest_path: Path, source_root: Path):
    """Resolve the full hint list.

    Manifest only carries hints that were eventually CAPTURED (1 of N in
    the degraded round-1 case). The whole point of this resolver is to
    fill nav-script TODOs for the un-captured hints, so we re-run static
    discovery from `source_root` to recover the full set.

    Falls back to manifest.pages[] only if discovery imports fail (e.g.
    the script is being run without the rest of the ui-capture package
    on sys.path).
    """
    # Pick adapter by manifest.adapter (falls back to AndroidAdapter for back-compat).
    adapter_name = ""
    try:
        adapter_name = (json.loads(manifest_path.read_text(encoding="utf-8"))
                        .get("adapter", ""))
    except Exception:
        pass
    try:
        skill_root = Path(__file__).resolve().parent.parent  # .../ui-capture
        sys.path.insert(0, str(skill_root))
        if adapter_name == "generic_harmony":
            from adapters.harmony import HarmonyAdapter  # type: ignore
            return HarmonyAdapter().discover_hints(source_root)
        # default / generic_android
        from adapters.android import AndroidAdapter  # type: ignore
        return AndroidAdapter().discover_hints(source_root)
    except Exception as exc:
        print(f"[auto-resolve-nav] could not import adapter ({exc}); "
              "falling back to manifest.pages[]", file=sys.stderr)

    data = json.loads(manifest_path.read_text(encoding="utf-8"))

    class _StubHint:
        __slots__ = ("class_name", "source_file", "slug", "origin")

        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    out = []
    for p in data.get("pages", []):
        out.append(_StubHint(
            class_name=p.get("class_name") or p.get("identity", {}).get("page_class") or p["id"],
            source_file=p.get("source_file") or "",
            slug=p["id"],
            origin=p.get("identity", {}).get("origin") or "fragment_subclass",
        ))
    return out


def main(argv: list[str]) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Auto-resolve nav-script tap targets from Android source.")
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--manifest", required=True,
                        help="Path to ui_manifest.json (provides hint list).")
    parser.add_argument("--out", required=True,
                        help="Output JSON path (e.g. nav_resolution.json)")
    args = parser.parse_args(argv)

    source_root = Path(args.source_root).resolve()
    manifest = Path(args.manifest).resolve()
    out_path = Path(args.out).resolve()

    if not manifest.exists():
        print(f"manifest not found: {manifest}", file=sys.stderr)
        return 2

    hints = _load_hints(manifest, source_root)
    if not hints:
        print(f"no hints in {manifest}", file=sys.stderr)
        return 2

    resolutions = resolve(hints, source_root)
    out = {
        "schema_version": 1,
        "source_root": str(source_root),
        "manifest": str(manifest),
        "resolutions": {k: asdict(v) for k, v in resolutions.items()},
        "stats": {
            "total_hints": len(resolutions),
            "resolved": sum(1 for r in resolutions.values() if r.resolved),
        },
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"[auto-resolve-nav] {out['stats']['resolved']}/{out['stats']['total_hints']} hints resolved -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
