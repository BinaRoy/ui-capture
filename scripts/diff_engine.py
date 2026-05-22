#!/usr/bin/env python3
"""Structural diff between two hierarchy.json trees.

Output: structured diff result with 6 difference categories. Consumed by:
  - compare.py (HTML rendering)
  - diff dumper CLI (diff.json / diff.md)
  - downstream CI (machine-readable diff.json)

Node alignment uses three fallback strategies:
  1. `id` match — exact native id (Android resource-id, ArkUI id, etc.)
  2. `(kind, text)` match — same kind + non-trivial text (len >= MIN_TEXT_LEN)
  3. `(kind, parent_path, relative_position)` — same kind at similar position
     in the tree, with parent kind chain matching

Difference types:
  missing         — node in source, not in target
  extra           — node in target, not in source
  kind_mismatch   — matched (by id), but kind differs
  text_mismatch   — matched, but text differs
  state_mismatch  — matched, but state booleans differ (per-field)
  bounds_drift    — matched, but bounds differ beyond tolerance
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Optional, Iterator

SCHEMA_VERSION = 2
MIN_TEXT_LEN = 3                  # below this, text is too generic to align on
# Tolerances are now in RELATIVE-bounds space (% of viewport), not absolute
# pixels. 2% catches DPI rounding noise; 5% is the cross-platform threshold
# below which we treat drift as platform-noise rather than layout difference.
DEFAULT_BOUNDS_TOL_PCT = 2
DEFAULT_CROSS_PLATFORM_TOL_PCT = 2


# ============================================================================
# B.2.2 P0-B  — chrome / passthrough rules
# ============================================================================
# Goal: strip platform-injected framework chrome (ArkUI Navigation stack,
# Android single-child layout wrappers) BEFORE diffing, so cross-platform
# comparison surfaces business-level differences instead of platform noise.
#
# Empirically observed (cj_telegram + WeatherApp captures, 2026-05-19):
# every HarmonyOS NavPathStack-pushed page wraps its content in a fixed chain
# `Navigation → NavigationContent → NavDestination → NavDestinationContent`,
# regardless of what the developer wrote. These wrappers carry no user-facing
# semantics and have no Android equivalent — diff treats them as 162 "extra"
# nodes, swamping the real signal.
#
# Android side has the inverse problem: layout managers (FrameLayout /
# LinearLayout / ViewGroup) often wrap a single child purely for the layout
# system's benefit. Cross-platform-equivalent ArkUI uses fewer such wrappers,
# so these Android wrappers appear as "missing" against HarmonyOS targets.

# Framework chrome: always strip when found (regardless of child count).
# Names are the `class` field as adapter normalizes them — for HarmonyOS this
# is the ArkUI component name reported by `uitest dumpLayout`.
_CHROME_CLASSES_HARMONY: frozenset[str] = frozenset({
    "root",                    # top-level uitest container (also appears as kind=view class=root)
    "Navigation",              # NavPathStack root
    "NavigationContent",       # content area placeholder
    "NavDestination",          # each NavPathStack entry's wrapper
    "NavDestinationContent",   # per-destination content wrapper
    "NavBar",                  # navigation title bar wrapper
    "NavBarContent",           # nav bar content area
    "__Common__",              # ArkUI internal common parent
    "EffectComponent",         # visual effect overlay
    "WindowScene",             # system-injected scene root
})

# Android-side passthrough: layout-only wrappers WITH a single child are
# considered chrome. Multi-child wrappers carry real layout semantics; do
# not strip them.
_ANDROID_PASSTHROUGH_KINDS: frozenset[str] = frozenset({
    "frame", "linear", "view", "relative", "constraint",
})


def _is_chrome_node(node: dict, platform: str) -> bool:
    """True if this node should be transparently passed through during diff."""
    cls = node.get("class") or ""
    if platform == "harmony" and cls in _CHROME_CLASSES_HARMONY:
        return True
    if platform == "android":
        kind = node.get("kind", "")
        if kind in _ANDROID_PASSTHROUGH_KINDS:
            # Only strip if it's an empty single-child wrapper. A wrapper with
            # text/id/click semantics or with multiple children is meaningful.
            has_semantics = (
                bool((node.get("text") or "").strip()) or
                bool(node.get("id")) or
                bool((node.get("state") or {}).get("clickable"))
            )
            kids = node.get("children") or []
            if not has_semantics and len(kids) == 1:
                return True
    return False


# ============================================================================
# B.2.2 P0-A  — semantic slot templates
# ============================================================================
# When strict (kind, text) match fails, try matching by (kind, slot_template).
# Slot templates abstract over data variations: "16°", "28°C", "30°F" all map
# to <TEMP>. This lets diff identify "same data slot, different content" vs
# "actually missing slot".
#
# Order matters — more specific patterns first so e.g. "1018 hPa" matches
# <PRESSURE> and not <NUM>.

_SLOT_PATTERNS: list[tuple[re.Pattern, str]] = [
    # "May 19, 10:16" / "2026-05-19"
    (re.compile(r"^[A-Z][a-z]{2,8}\s+\d{1,2}(,\s*\d{2,4})?(\s*\d{1,2}:\d{2})?$"), "<DATE>"),
    (re.compile(r"^\d{4}-\d{2}-\d{2}(\s+\d{1,2}:\d{2})?$"), "<DATE>"),

    # "Feels like 17°C" / "Last update May 19, 10:16" — composite templates with prefix
    (re.compile(r"^Feels like\s+-?\d+(\.\d+)?\s*°[CF]?$", re.IGNORECASE), "Feels like <TEMP>"),
    (re.compile(r"^Day\s+-?\d+°[CF]?\s+Night\s+-?\d+°[CF]?$"), "Day <TEMP> Night <TEMP>"),

    # "14 km/h", "8 mph", "3 m/s"
    (re.compile(r"^-?\d+(\.\d+)?\s*(km/h|mph|m/s)$", re.IGNORECASE), "<SPEED>"),

    # "1018 hPa", "29.92 inHg", "760 mmHg"
    (re.compile(r"^-?\d+(\.\d+)?\s*(hpa|inhg|mmhg|pa|bar)$", re.IGNORECASE), "<PRESSURE>"),

    # Time of day "12:00" "08:30" "23:59:59"
    (re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$"), "<TIME>"),

    # "7h ago" "in 2h" "3 min ago" "5 days later"
    (re.compile(r"^(in\s+)?\d+\s*(h|hr|hour|hours|m|min|mins|minutes|d|day|days)(\s+(ago|later))?$",
                re.IGNORECASE), "<DURATION>"),

    # Temperature: "16°", "28°C", "-3.5°F"
    (re.compile(r"^-?\d+(\.\d+)?\s*°[CF]?$"), "<TEMP>"),

    # Percentage: "0%", "100%", "12.5%"
    (re.compile(r"^-?\d+(\.\d+)?\s*%$"), "<PCT>"),

    # UV index / generic-unit "1 km" / "5 mi"
    (re.compile(r"^-?\d+(\.\d+)?\s*(km|mi|m|ft|mm|cm)$", re.IGNORECASE), "<LENGTH>"),

    # Bare number (catch-all numeric)
    (re.compile(r"^-?\d+(\.\d+)?$"), "<NUM>"),
]


def _slot_template(text: str) -> Optional[str]:
    """Return a slot template token for `text`, or None if no template matches.

    Template tokens are stable strings like '<TEMP>'. Two pieces of text that
    yield the same token are treated as occupying the same semantic slot for
    cross-platform alignment.
    """
    t = (text or "").strip()
    if not t:
        return None
    for pat, token in _SLOT_PATTERNS:
        if pat.match(t):
            return token
    return None


_DIGIT_RE = re.compile(r"\d+(?:\.\d+)?")


def _digit_skeleton(s: str) -> str:
    """Replace every numeric run with 'N'. Lets us tell apart 'value differs'
    from 'format differs' inside a slot-template-matched text_mismatch:
        '15°'   vs '17°'    → 'N°'  == 'N°'    → same skeleton (data_drift)
        '15°'   vs '24°C'   → 'N°'  != 'N°C'   → different     (format_diff)
        '0%'    vs '2%'     → 'N%'  == 'N%'    → data_drift
        '1018 hpa' vs '1024 hPa' → 'N hpa' != 'N hPa' (case)   → format_diff
    """
    return _DIGIT_RE.sub("N", s.strip())


def _text_mismatch_subkind(src_text: str, tgt_text: str,
                           strat: Optional[str]) -> str:
    """Refine a text_mismatch into format_diff / data_drift / content_change.

    Only meaningful when the two texts were aligned via slot_template — that
    is the evidence that both sides occupy the same semantic slot. Without
    that evidence the texts may simply be different content.
    """
    if strat != "slot_template":
        return "content_change"
    sk_s = _digit_skeleton(src_text)
    sk_t = _digit_skeleton(tgt_text)
    if sk_s == sk_t:
        return "data_drift"
    if sk_s.lower() == sk_t.lower():
        # Same after lowercasing → casing-only format difference (hpa/hPa)
        return "format_diff"
    return "format_diff"


# ---------------------------------------------------------------- data shapes

@dataclass
class FlatNode:
    """One node from a hierarchy tree, flattened with path context."""
    path: str                     # breadcrumb like "0/2/1"
    parent_kinds: tuple[str, ...] # chain of ancestor kinds, root → parent
    sibling_index: int            # position among siblings
    sibling_count: int            # how many siblings (for positional matching)
    node: dict                    # original node dict


@dataclass
class Diff:
    type: str                       # missing/extra/kind_mismatch/text_mismatch/state_mismatch/bounds_drift
    severity: str                   # UI-action category (see SEVERITY_ORDER)
    category: str                   # consumer-facing bucket (see CATEGORY_ORDER)
    match_strategy: Optional[str]   # "id" | "kind_text" | "slot_template" | "positional" | None
    path: str                       # source path (or target path for `extra`)
    source_node: Optional[dict] = None
    target_node: Optional[dict] = None
    details: dict = field(default_factory=dict)


@dataclass
class DiffResult:
    schema_version: int
    source: dict
    target: dict
    summary: dict
    diffs: list[Diff]
    config: dict

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "source": self.source,
            "target": self.target,
            "summary": self.summary,
            "diffs": [asdict(d) for d in self.diffs],
            "config": self.config,
        }


# ------------------------------------------------------------------ flatten

def _flatten(root: dict, *, strip_chrome: bool = False) -> list[FlatNode]:
    """Walk a hierarchy tree depth-first, yield FlatNode for every descendant
    of the root (the root itself is skipped — it's a wrapper).

    When `strip_chrome=True`, framework chrome (ArkUI Navigation stack,
    Android single-child layout wrappers — see `_is_chrome_node`) is
    transparently passed through: the chrome node itself is not emitted, and
    its children are walked as if they were direct children of the chrome's
    parent. This is the standard mode for cross-platform diff.

    Side effect: each emitted node gets `_label_anchor` written onto it (in
    place) — the nearest plain-text sibling (text without a slot template),
    used by Pass 2.5 to anchor templated values to their nearby labels.
    """
    out: list[FlatNode] = []
    platform = root.get("platform", "unknown")

    def annotate_label_anchors(siblings: list[dict]) -> None:
        # Collect every plain-text (non-templated) string among these siblings.
        # The "primary" label is the longest such string — beats picking
        # whichever shows up first.
        plain_texts = []
        for s in siblings:
            t = (s.get("text") or "").strip()
            if t and _slot_template(t) is None and len(t) >= 3:
                plain_texts.append(t)
        if not plain_texts:
            return
        anchor = max(plain_texts, key=len)
        for s in siblings:
            t = (s.get("text") or "").strip()
            if t and _slot_template(t) is not None:
                s["_label_anchor"] = anchor

    def walk(nodes: list[dict], parent_kinds: tuple[str, ...], path_prefix: str) -> None:
        # Annotate label anchors at this sibling level before recursing.
        annotate_label_anchors(nodes)
        n = len(nodes)
        for i, node in enumerate(nodes):
            path = f"{path_prefix}/{i}" if path_prefix else str(i)
            kids = node.get("children") or []
            if strip_chrome and _is_chrome_node(node, platform):
                # Skip emitting this node; walk its children at the same
                # logical level (path keeps drilling so positional matching
                # still sees them at their structural depth).
                if kids:
                    walk(kids, parent_kinds, path)
                continue
            out.append(FlatNode(
                path=path,
                parent_kinds=parent_kinds,
                sibling_index=i,
                sibling_count=n,
                node=node,
            ))
            if kids:
                walk(kids, parent_kinds + (node.get("kind", "view"),), path)

    walk(root.get("children", []), (), "")
    return out


# ---------------------------------------------------------------- alignment

def _bucket_by_id(nodes: list[FlatNode]) -> dict[str, list[FlatNode]]:
    out: dict[str, list[FlatNode]] = {}
    for fn in nodes:
        nid = fn.node.get("id")
        if nid:
            out.setdefault(nid, []).append(fn)
    return out


def _bucket_by_kind_text(nodes: list[FlatNode]) -> dict[tuple[str, str], list[FlatNode]]:
    out: dict[tuple[str, str], list[FlatNode]] = {}
    for fn in nodes:
        text = (fn.node.get("text") or "").strip()
        if len(text) < MIN_TEXT_LEN:
            continue
        key = (fn.node.get("kind", "view"), text)
        out.setdefault(key, []).append(fn)
    return out


def _slot_key(fn: FlatNode) -> Optional[tuple[str, str, str]]:
    """Slot key for Pass 2.5 matching.

    Returns `(kind, slot_template, label_anchor)` if the node's text fits a
    known semantic slot template. `label_anchor` is the nearest sibling /
    nearby plain-text label (e.g., the value "14 km/h" is anchored to its
    sibling "Wind speed"). Empty string when no plain-text anchor is nearby
    — those slot nodes still match each other, just less precisely.

    `parent_kinds` is intentionally NOT in the key: Android maps layout
    containers to `kind=view`, ArkUI to `kind=linear`, so cross-platform
    parent chains never match. The label anchor gives us a content-driven
    locality anchor that survives that platform asymmetry.
    """
    text = (fn.node.get("text") or "").strip()
    if not text:
        return None
    tpl = _slot_template(text)
    if not tpl:
        return None
    return (fn.node.get("kind", "view"), tpl, fn.node.get("_label_anchor") or "")


def _positional_key(fn: FlatNode) -> tuple[str, tuple[str, ...], int, int]:
    """Used as last-resort match key. Combines kind + ancestor kind chain +
    sibling slot. Cross-platform trees won't match exactly, but same-platform
    re-runs of identical pages will."""
    return (fn.node.get("kind", "view"), fn.parent_kinds, fn.sibling_index, fn.sibling_count)


def _bucket_by_positional(nodes: list[FlatNode]) -> dict:
    out: dict = {}
    for fn in nodes:
        out.setdefault(_positional_key(fn), []).append(fn)
    return out


# ------------------------------------------------------------ field comparison

def _viewport_dims(ir: dict) -> tuple[int, int]:
    """Compute viewport (w, h) for a hierarchy.

    Height: prefer `page_total_height` (post-scroll-stitching full page) so
    relative top% is meaningful for elements far down a long page. Falls back
    to `viewport_height`, then to max bottom edge across all bounds.

    Width: not stored explicitly, so we scan all node bounds and take the
    max right edge. The root container of a captured page covers the
    physical screen width, so this gives us 1080 / 1272 / etc.
    """
    h = ir.get("page_total_height") or ir.get("viewport_height") or 0
    w = 0
    def walk(n):
        nonlocal w, h
        b = n.get("bounds")
        if isinstance(b, list) and len(b) == 4:
            if b[2] > w: w = b[2]
            if not h and b[3] > h: h = b[3]
        for c in n.get("children") or []:
            walk(c)
    walk(ir)
    return (max(w, 1), max(h, 1))


def _relative_bounds(bounds: list, vw: int, vh: int) -> Optional[list]:
    """Return [left%, top%, width%, height%] in percent of viewport,
    rounded to 0.1%. None if bounds missing."""
    if not (isinstance(bounds, list) and len(bounds) == 4):
        return None
    x1, y1, x2, y2 = bounds
    return [
        round(x1 / vw * 100, 1),
        round(y1 / vh * 100, 1),
        round((x2 - x1) / vw * 100, 1),
        round((y2 - y1) / vh * 100, 1),
    ]


# Cutoffs for classifying relative-bounds drift. Cross-platform pixel
# differences due to DPI rounding are well under 2%; anything beyond ~5%
# is a real layout difference an agent should investigate.
_REL_NOISE_PCT = 2.0          # under this → pure DPI rounding, classify NOISE
_REL_POSITION_PCT = 5.0       # 2–5% → minor drift; >5% on x/y → LAYOUT_POSITION
_REL_SIZE_PCT = 5.0           # >5% on w/h → LAYOUT_SIZE


def _bounds_drift(b1: list, b2: list, tol_pct: float,
                  src_vw: int, src_vh: int,
                  tgt_vw: int, tgt_vh: int) -> Optional[dict]:
    """Compare two bounds in RELATIVE (per-viewport %) space.

    Returns a dict describing the drift with:
      - source_abs / target_abs: original pixel bounds (provenance)
      - source_rel / target_rel: [left%, top%, w%, h%]
      - drift_rel_pct: {left: 4.2, top: 0.8, width: 1.1, height: 0.3} (only
        fields where drift exceeds `tol_pct`)
      - max_position_drift / max_size_drift: handy for severity classification

    `tol_pct` is in RELATIVE units (percent of viewport), not absolute pixels.
    Default 5% works for cross-platform; 2% for same-platform.

    None when bounds match within tolerance, or when both bounds are missing.
    """
    if not (isinstance(b1, list) and len(b1) == 4 and
            isinstance(b2, list) and len(b2) == 4):
        if (b1 is None) != (b2 is None):
            return {"source_abs": b1, "target_abs": b2, "reason": "one_side_missing"}
        return None
    rb1 = _relative_bounds(b1, src_vw, src_vh)
    rb2 = _relative_bounds(b2, tgt_vw, tgt_vh)
    if rb1 is None or rb2 is None:
        return None
    drifts: dict[str, float] = {}
    labels = ("left", "top", "width", "height")
    for label, v1, v2 in zip(labels, rb1, rb2):
        pct = abs(v1 - v2)
        if pct > tol_pct:
            drifts[label] = round(pct, 1)
    if not drifts:
        return None
    max_pos = max(drifts.get("left", 0), drifts.get("top", 0))
    max_size = max(drifts.get("width", 0), drifts.get("height", 0))
    return {
        "source_abs": b1, "target_abs": b2,
        "source_rel": rb1, "target_rel": rb2,
        "drift_rel_pct": drifts,
        "max_position_drift": round(max_pos, 1),
        "max_size_drift": round(max_size, 1),
        "tol_pct": tol_pct,
    }


def _state_diff(s1: Optional[dict], s2: Optional[dict]) -> Optional[dict]:
    s1 = s1 or {}; s2 = s2 or {}
    # Strip adapter-schema-asymmetric fields before comparing. Android's
    # uiautomator emits password / focusable / long_clickable / checkable as
    # bools on every node; Harmony's uitest dumpLayout omits them. Comparing
    # them produces ~33 spurious state_mismatch entries per page that carry
    # no user-visible behavior signal. The fields are kept in the IR for
    # debugging — only excluded from the cross-platform diff.
    keys = (set(s1) | set(s2)) - _NOISE_STATE_FIELDS
    changed = {k: {"source": s1.get(k), "target": s2.get(k)}
               for k in keys if s1.get(k) != s2.get(k)}
    return changed or None


# ============================================================================
# B.2.2 P1-B  — UI-action severity classification
# ============================================================================
# Each Diff entry gets one severity label. The label answers: "what kind of
# UI-design fix does this imply?" Categories are designed so a downstream
# agent can read the diff and produce a concrete fix list, not so the
# numbers look clean.
#
# The user reframed the goal: "I want the diff to tell me UI-design
# differences and let the agent improve the translated UI's layout / tree /
# visual gap." Categories below reflect that lens.
#
# Categories (in order an agent should action them):
#
#   MISSING_FEATURE       Source has a labeled element with no counterpart in
#                         target. The translated UI is incomplete.
#                         Action: add the missing element.
#
#   EXTRA_FEATURE         Target has a labeled element with no counterpart in
#                         source. Often a dev-chosen wrapper (Refresh,
#                         LoadingProgress) — review whether intentional.
#                         Action: consider removing or document why kept.
#
#   TEXT_FORMAT           Matched element, text differs in format (unit
#                         suffix, casing, punctuation). Translation choice
#                         that should be aligned project-wide.
#                         Action: pick canonical format, apply both sides.
#
#   LAYOUT_PARADIGM       Matched element with different kind (Button vs
#                         Text, list vs pager, scroll vs frame). Two ways
#                         of expressing the same intent.
#                         Action: design decision; align if cross-platform
#                         consistency desired.
#
#   LAYOUT_POSITION       Matched element shifted on screen beyond DPI noise
#                         (>5% on x or y relative to viewport).
#                         Action: investigate alignment / spacing.
#
#   LAYOUT_SIZE           Matched element sized differently beyond DPI noise
#                         (>5% on width or height).
#                         Action: investigate sizing constraints.
#
#   BEHAVIOR              State difference on interactive fields (clickable
#                         on one side only, enabled flipping, etc.) — affects
#                         what a user can do, not just look.
#                         Action: align interactive affordances.
#
#   PLATFORM_NOISE        Cross-platform artifacts that aren't UI-design
#                         differences: DPI rounding (drift <2%), adapter
#                         schema variance (Android tracks password /
#                         focusable bool, Harmony omits), framework chrome
#                         normalization. Displayed for completeness, but
#                         expected to be ignored.
#                         Action: none.

SEVERITY_ORDER = (
    "MISSING_FEATURE",
    "EXTRA_FEATURE",
    "TEXT_FORMAT",
    "LAYOUT_PARADIGM",
    "LAYOUT_POSITION",
    "LAYOUT_SIZE",
    "BEHAVIOR",
    "DATA_DIVERGENCE",
    "PLATFORM_NOISE",
)

# Severities that are informational only — excluded from "actionable signal"
# counts in summary / triage. DATA_DIVERGENCE is a real signal but not one a
# translation fix can resolve (it reflects different runtime data, not code).
_INFORMATIONAL_SEVERITIES: frozenset[str] = frozenset({
    "DATA_DIVERGENCE", "PLATFORM_NOISE",
})

# A node is "data-bound" if its text content is highly likely set at runtime
# from external state (API, system clock, user prefs) rather than declared in
# source layout. Differences across two such nodes usually reflect captures
# made under different runtime state (different city, different timestamp),
# not translation gaps.

# Android Hungarian convention: TextView ids prefixed with `tv` + CamelCase.
_ANDROID_DATA_ID_RE = re.compile(r"^tv[A-Z]")

# Substrings (case-insensitive) inside id that strongly suggest dynamic text.
_DATA_ID_HINTS: tuple[str, ...] = (
    "city", "country", "time", "date", "degree", "temp", "press", "wind",
    "speed", "humid", "feel", "lastupdate", "diff", "uv", "rain", "sun",
)

# Text-shape patterns that almost always indicate runtime-filled data.
_DATA_TEXT_RE = re.compile(
    r"""^(
        -?\d+(\.\d+)?\s*°[CF]?               # 18°  18°C
      | \d{1,2}:\d{2}(\s*[AP]M)?             # 04:00
      | \d{1,2}/\d{1,2}(/\d{2,4})?           # 5/22
      | \d+(\.\d+)?\s*%                      # 87%
      | \d+(\.\d+)?\s*(hpa|hpa|mmhg|inhg|mph|km/h|ms|kmh|m/s)  # 1008 hPa
      | -?\d+(\.\d+)?                        # pure number
    )$""",
    re.VERBOSE | re.IGNORECASE,
)


def _is_data_bound(node: Optional[dict]) -> bool:
    """Heuristically detect runtime-filled text nodes. Platform-agnostic.

    Signals (any one is enough):
      - Android-style `tv<CamelCase>` id
      - id contains a common dynamic-data substring (city/time/temp/...)
      - text matches a numeric / time / unit / percentage pattern

    Heuristic — produces false positives on hand-typed constants matching
    these shapes (e.g. a fixed "100%" label). The downstream impact is the
    diff is bucketed to DATA_DIVERGENCE rather than dropped, so it remains
    visible to an agent that wants to re-classify.
    """
    if not node:
        return False
    raw_id = (node.get("id") or "").strip()
    nid = raw_id.rsplit("/", 1)[-1]   # strip "pkg:id/" namespace if present
    if nid:
        if _ANDROID_DATA_ID_RE.match(nid):
            return True
        lower = nid.lower()
        if any(h in lower for h in _DATA_ID_HINTS):
            return True
    text = (node.get("text") or "").strip()
    if text and _DATA_TEXT_RE.match(text):
        return True
    return False

# Consumer-facing categorization. Independent of `severity` (which is the
# UI-design lens). `category` is for downstream agents/scripts that want to
# quickly filter "real" diffs vs. cross-platform noise without re-deriving
# what every entry means.
#
#   chrome_wrapper       Pure structural wrapper (no text / id / content_desc).
#                        Filter out by default.
#   schema_asymmetry     Adapter-schema difference (e.g. Android-only state
#                        bool fields). Currently stripped upstream in
#                        _state_diff, so this category is reserved for future
#                        cross-platform schema gaps.
#   data_drift           Same slot template on both sides, only the filled
#                        value differs (city name, time, temperature number).
#                        Downgraded; the format is consistent.
#   format_diff          Matched element, text differs in format/casing/unit
#                        (e.g. "16°" vs "24°C", "hpa" vs "hPa").
#   implementation_diff  Two sides express the same semantic with different
#                        primitives (e.g. ImageView icon vs emoji Text).
#                        Detection requires pair_hint logic — not implemented
#                        in this pass; reserved.
#   real_diff            Genuine layout / content / behavior difference.
CATEGORY_ORDER = (
    "real_diff",
    "format_diff",
    "implementation_diff",
    "data_drift",
    "chrome_wrapper",
    "schema_asymmetry",
)


_STRUCTURAL_KINDS = frozenset({
    "linear", "frame", "scroll", "view", "list", "stack", "row", "column", "flex",
})


def _classify_category(diff_type: str, source_node: Optional[dict],
                       target_node: Optional[dict], details: dict,
                       strat: Optional[str]) -> str:
    """Bucket each diff for agent-side filtering. Independent of severity."""
    if diff_type in ("missing", "extra"):
        n = source_node if diff_type == "missing" else target_node
        n = n or {}
        text = (n.get("text") or "").strip()
        desc = (n.get("content_desc") or "").strip()
        if text or desc:
            return "real_diff"
        # No user-visible text. An id alone counts as semantic only if the
        # node is NOT a generic structural container — framework-assigned
        # ids on layout wrappers (e.g. Android's `action_bar_root`) are not
        # user-visible signal.
        if n.get("id") and n.get("kind") not in _STRUCTURAL_KINDS:
            return "real_diff"
        return "chrome_wrapper"
    if diff_type == "text_mismatch":
        # P0-④ refines this: slot_template match → split format_diff vs
        # data_drift based on whether the template itself differs. Until
        # then, slot_template-matched text_mismatch is treated as format_diff.
        subkind = (details or {}).get("subkind")
        if subkind in ("data_drift", "format_diff", "content_change"):
            return subkind if subkind != "content_change" else "real_diff"
        if strat == "slot_template":
            return "format_diff"
        return "real_diff"
    if diff_type == "state_mismatch":
        return "real_diff"   # noise fields already stripped in _state_diff
    # kind_mismatch, bounds_drift → real_diff
    return "real_diff"

# State fields whose differences are purely adapter-normalization (Android
# emits them as bool, HarmonyOS omits them) → classify as PLATFORM_NOISE.
_NOISE_STATE_FIELDS: frozenset[str] = frozenset({
    "password", "focusable", "long_clickable", "checkable",
})

# State fields whose differences DO affect user-perceivable behavior.
_BEHAVIOR_STATE_FIELDS: frozenset[str] = frozenset({
    "clickable", "enabled", "checked", "selected", "scrollable",
})


def _classify_severity(diff_type: str, source_node: Optional[dict],
                       target_node: Optional[dict],
                       details: dict) -> str:
    """Map a Diff into one of SEVERITY_ORDER labels — the UI-action category.
    All input is what a Diff record already carries; no extra hierarchy
    walks needed."""
    if diff_type == "missing":
        # Has visible text → real feature missing; pure structural wrapper → noise
        text = (source_node or {}).get("text") or ""
        if text.strip():
            if _is_data_bound(source_node):
                return "DATA_DIVERGENCE"
            return "MISSING_FEATURE"
        return "PLATFORM_NOISE"
    if diff_type == "extra":
        text = (target_node or {}).get("text") or ""
        if text.strip():
            if _is_data_bound(target_node):
                return "DATA_DIVERGENCE"
            return "EXTRA_FEATURE"
        return "PLATFORM_NOISE"
    if diff_type == "text_mismatch":
        # Slot-template "data_drift" subkind (same template, different value)
        # is by definition data-bound — surface as DATA_DIVERGENCE.
        if details.get("subkind") == "data_drift":
            return "DATA_DIVERGENCE"
        # Either-side data-bound text mismatch → also DATA_DIVERGENCE. Catches
        # cases the slot-template detector missed (free-form city names, etc.)
        if _is_data_bound(source_node) or _is_data_bound(target_node):
            return "DATA_DIVERGENCE"
        return "TEXT_FORMAT"
    if diff_type == "kind_mismatch":
        return "LAYOUT_PARADIGM"
    if diff_type == "state_mismatch":
        changes = details.get("changes") or {}
        # If ALL diffs are in noise fields → PLATFORM_NOISE. If any is in
        # a behavior field → BEHAVIOR.
        keys = set(changes.keys())
        if keys and keys.issubset(_NOISE_STATE_FIELDS):
            return "PLATFORM_NOISE"
        if keys & _BEHAVIOR_STATE_FIELDS:
            return "BEHAVIOR"
        return "PLATFORM_NOISE"
    if diff_type == "bounds_drift":
        # Use relative-drift fields. <2% = DPI noise, >5% on pos or size =
        # real layout shift.
        max_pos = details.get("max_position_drift", 0)
        max_size = details.get("max_size_drift", 0)
        # Determine which category dominates
        if max_pos >= _REL_POSITION_PCT and max_pos >= max_size:
            return "LAYOUT_POSITION"
        if max_size >= _REL_SIZE_PCT:
            return "LAYOUT_SIZE"
        return "PLATFORM_NOISE"
    return "PLATFORM_NOISE"


def _short_node(node: dict) -> dict:
    """Compact form for inclusion in diff records — keep the parts a reviewer
    needs to identify the node without hauling the full subtree."""
    keep = {}
    for k in ("kind", "id", "class", "text", "content_desc", "bounds"):
        v = node.get(k)
        if v is not None:
            keep[k] = v
    return keep


# -------------------------------------------------------------------- compute

def compute_diff(
    source_ir: dict,
    target_ir: dict,
    *,
    bounds_tol_pct: Optional[float] = None,
    source_page_id: str = "",
    target_page_id: str = "",
) -> DiffResult:
    """Compute a structural diff between two hierarchy trees."""
    src_platform = source_ir.get("platform", "unknown")
    tgt_platform = target_ir.get("platform", "unknown")
    cross_platform = src_platform != tgt_platform
    if bounds_tol_pct is None:
        bounds_tol_pct = (DEFAULT_CROSS_PLATFORM_TOL_PCT
                          if cross_platform
                          else DEFAULT_BOUNDS_TOL_PCT)

    # B.2.2 P0-B: strip framework chrome on cross-platform diff so structural
    # noise (Navigation/NavDestination chains on Harmony side, single-child
    # FrameLayout wrappers on Android side) doesn't drown out real diffs.
    src_flat = _flatten(source_ir, strip_chrome=cross_platform)
    tgt_flat = _flatten(target_ir, strip_chrome=cross_platform)
    src_total_raw = sum(1 for _ in _flatten(source_ir))   # for stats
    tgt_total_raw = sum(1 for _ in _flatten(target_ir))

    # P1-A: per-page viewport for relative-bounds comparison
    src_vw, src_vh = _viewport_dims(source_ir)
    tgt_vw, tgt_vh = _viewport_dims(target_ir)

    src_unmatched = set(range(len(src_flat)))
    tgt_unmatched = set(range(len(tgt_flat)))
    matched: list[tuple[int, int, str]] = []  # (src_idx, tgt_idx, strategy)

    # --- Pass 1: id match
    tgt_by_id = _bucket_by_id(tgt_flat)
    src_by_id_idx: dict[str, list[int]] = {}
    for i, fn in enumerate(src_flat):
        nid = fn.node.get("id")
        if nid:
            src_by_id_idx.setdefault(nid, []).append(i)
    for nid, src_idxs in src_by_id_idx.items():
        tgt_nodes = tgt_by_id.get(nid, [])
        # 1-to-1 within an id bucket; consume in order
        tgt_idxs = [tgt_flat.index(tn) for tn in tgt_nodes]
        for s_i, t_i in zip(src_idxs, tgt_idxs):
            if s_i in src_unmatched and t_i in tgt_unmatched:
                matched.append((s_i, t_i, "id"))
                src_unmatched.discard(s_i); tgt_unmatched.discard(t_i)

    # --- Pass 2: (kind, text) match — only among still-unmatched
    src_remaining = [src_flat[i] for i in sorted(src_unmatched)]
    tgt_remaining = [tgt_flat[i] for i in sorted(tgt_unmatched)]
    src_kt_idx: dict[tuple[str, str], list[int]] = {}
    for i, fn in enumerate(src_remaining):
        text = (fn.node.get("text") or "").strip()
        if len(text) >= MIN_TEXT_LEN:
            key = (fn.node.get("kind", "view"), text)
            src_kt_idx.setdefault(key, []).append(i)
    tgt_kt_idx: dict[tuple[str, str], list[int]] = {}
    for i, fn in enumerate(tgt_remaining):
        text = (fn.node.get("text") or "").strip()
        if len(text) >= MIN_TEXT_LEN:
            key = (fn.node.get("kind", "view"), text)
            tgt_kt_idx.setdefault(key, []).append(i)
    for key, src_idxs in src_kt_idx.items():
        tgt_idxs = tgt_kt_idx.get(key, [])
        for si, ti in zip(src_idxs, tgt_idxs):
            real_si = src_flat.index(src_remaining[si])
            real_ti = tgt_flat.index(tgt_remaining[ti])
            if real_si in src_unmatched and real_ti in tgt_unmatched:
                matched.append((real_si, real_ti, "kind_text"))
                src_unmatched.discard(real_si); tgt_unmatched.discard(real_ti)

    # --- Pass 2.5: semantic slot match (B.2.2 P0-A)
    # For nodes whose text fits a known template ("28°C" → <TEMP>, "14 km/h"
    # → <SPEED>, etc.), bucket by (kind, template, parent_kind_chain). This
    # aligns "same-slot, different-data" pairs across the two sides — e.g.
    # Android "16°" against HarmonyOS "28°C" — so the next phase reports them
    # as a text_mismatch (a translation decision point) rather than missing
    # on one side and extra on the other.
    src_remaining = [src_flat[i] for i in sorted(src_unmatched)]
    tgt_remaining = [tgt_flat[i] for i in sorted(tgt_unmatched)]
    src_slot_idx: dict = {}
    for i, fn in enumerate(src_remaining):
        k = _slot_key(fn)
        if k is not None:
            src_slot_idx.setdefault(k, []).append(i)
    tgt_slot_idx: dict = {}
    for i, fn in enumerate(tgt_remaining):
        k = _slot_key(fn)
        if k is not None:
            tgt_slot_idx.setdefault(k, []).append(i)
    for key, src_idxs in src_slot_idx.items():
        tgt_idxs = tgt_slot_idx.get(key, [])
        # When multiple values share the same slot key (e.g. 6 hourly temps),
        # zip pairs them in document order — good enough since both sides
        # render them in time order.
        for si, ti in zip(src_idxs, tgt_idxs):
            real_si = src_flat.index(src_remaining[si])
            real_ti = tgt_flat.index(tgt_remaining[ti])
            if real_si in src_unmatched and real_ti in tgt_unmatched:
                matched.append((real_si, real_ti, "slot_template"))
                src_unmatched.discard(real_si); tgt_unmatched.discard(real_ti)

    # --- Pass 3: positional match (kind + parent chain + sibling slot)
    src_remaining = [src_flat[i] for i in sorted(src_unmatched)]
    tgt_remaining = [tgt_flat[i] for i in sorted(tgt_unmatched)]
    tgt_pos_idx: dict = {}
    for i, fn in enumerate(tgt_remaining):
        tgt_pos_idx.setdefault(_positional_key(fn), []).append(i)
    for si, sfn in enumerate(src_remaining):
        key = _positional_key(sfn)
        tgt_list = tgt_pos_idx.get(key, [])
        if not tgt_list:
            continue
        ti = tgt_list.pop(0)
        real_si = src_flat.index(sfn)
        real_ti = tgt_flat.index(tgt_remaining[ti])
        if real_si in src_unmatched and real_ti in tgt_unmatched:
            matched.append((real_si, real_ti, "positional"))
            src_unmatched.discard(real_si); tgt_unmatched.discard(real_ti)

    # --- Emit diffs
    diffs: list[Diff] = []
    counts = {"missing": 0, "extra": 0, "kind_mismatch": 0,
              "text_mismatch": 0, "state_mismatch": 0, "bounds_drift": 0}

    def _emit(diff_type, source_node, target_node, details, path, strat):
        details = details or {}
        sev = _classify_severity(diff_type, source_node, target_node, details)
        cat = _classify_category(diff_type, source_node, target_node, details, strat)
        diffs.append(Diff(
            type=diff_type, severity=sev, category=cat, match_strategy=strat,
            path=path, source_node=source_node, target_node=target_node,
            details=details,
        ))
        counts[diff_type] += 1

    # missing: still-unmatched source nodes
    for i in sorted(src_unmatched):
        fn = src_flat[i]
        _emit("missing", _short_node(fn.node), None, {}, fn.path, None)
    # extra: still-unmatched target nodes
    for i in sorted(tgt_unmatched):
        fn = tgt_flat[i]
        _emit("extra", None, _short_node(fn.node), {}, fn.path, None)
    # mismatches on matched pairs
    for si, ti, strat in matched:
        s_node = src_flat[si].node; t_node = tgt_flat[ti].node
        s_path = src_flat[si].path
        s_kind = s_node.get("kind"); t_kind = t_node.get("kind")
        if s_kind != t_kind:
            _emit("kind_mismatch", _short_node(s_node), _short_node(t_node),
                  {"source_kind": s_kind, "target_kind": t_kind}, s_path, strat)
        s_text = (s_node.get("text") or "").strip()
        t_text = (t_node.get("text") or "").strip()
        if s_text != t_text and (s_text or t_text):
            subkind = _text_mismatch_subkind(s_text, t_text, strat)
            _emit("text_mismatch", _short_node(s_node), _short_node(t_node),
                  {"source_text": s_text, "target_text": t_text,
                   "subkind": subkind},
                  s_path, strat)
        st = _state_diff(s_node.get("state"), t_node.get("state"))
        if st:
            _emit("state_mismatch", _short_node(s_node), _short_node(t_node),
                  {"changes": st}, s_path, strat)
        bd = _bounds_drift(s_node.get("bounds"), t_node.get("bounds"),
                           bounds_tol_pct, src_vw, src_vh, tgt_vw, tgt_vh)
        if bd:
            _emit("bounds_drift", _short_node(s_node), _short_node(t_node),
                  bd, s_path, strat)

    by_severity = {s: 0 for s in SEVERITY_ORDER}
    for d in diffs:
        by_severity[d.severity] = by_severity.get(d.severity, 0) + 1

    by_category = {c: 0 for c in CATEGORY_ORDER}
    for d in diffs:
        by_category[d.category] = by_category.get(d.category, 0) + 1

    total_diffs = len(diffs)
    informational = sum(by_severity.get(s, 0) for s in _INFORMATIONAL_SEVERITIES)
    actionable_signal = total_diffs - informational

    summary = {
        **counts,
        "matched": len(matched),
        "source_total": len(src_flat),
        "target_total": len(tgt_flat),
        "source_total_raw": src_total_raw,
        "target_total_raw": tgt_total_raw,
        "chrome_stripped": cross_platform,
        "matched_by_strategy": {
            "id":            sum(1 for _, _, s in matched if s == "id"),
            "kind_text":     sum(1 for _, _, s in matched if s == "kind_text"),
            "slot_template": sum(1 for _, _, s in matched if s == "slot_template"),
            "positional":    sum(1 for _, _, s in matched if s == "positional"),
        },
        "by_severity": by_severity,
        "by_category": by_category,
        "total": total_diffs,
        "actionable_signal": actionable_signal,
        "signal_pct": (round(actionable_signal * 100 / total_diffs, 1)
                       if total_diffs else 0.0),
    }

    return DiffResult(
        schema_version=SCHEMA_VERSION,
        source={"page_id": source_page_id, "platform": src_platform, "node_count": len(src_flat)},
        target={"page_id": target_page_id, "platform": tgt_platform, "node_count": len(tgt_flat)},
        summary=summary,
        diffs=diffs,
        config={
            "bounds_tol_pct": bounds_tol_pct,
            "min_text_length": MIN_TEXT_LEN,
            "schema_version": SCHEMA_VERSION,
        },
    )


# ------------------------------------------------------------- markdown render

_SEVERITY_HEADERS: dict[str, tuple[str, str]] = {
    # severity → (display heading, fix-guidance line)
    "MISSING_FEATURE":  ("Missing features (source has, target lacks)",
                         "Action: add the missing element to the translated UI."),
    "EXTRA_FEATURE":    ("Extra features (target has, source lacks)",
                         "Action: review whether the extra element is intentional; remove or document."),
    "TEXT_FORMAT":      ("Text format / translation choices",
                         "Action: pick a canonical text format (units, casing, punctuation) and align both sides."),
    "LAYOUT_PARADIGM":  ("Layout paradigm differences (different element kind)",
                         "Action: decide which kind to standardize on; refactor the translated component."),
    "LAYOUT_POSITION":  ("Layout — position drift (>5% viewport)",
                         "Action: investigate alignment / spacing / parent flex rules."),
    "LAYOUT_SIZE":      ("Layout — size drift (>5% viewport)",
                         "Action: investigate width/height constraints, padding, or wrap settings."),
    "BEHAVIOR":         ("Interactive behavior differences",
                         "Action: align clickable / enabled / scrollable affordances."),
    "DATA_DIVERGENCE":  ("Runtime data divergence (API / clock / locale state differs)",
                         "Action: usually none — captures taken under different runtime data. "
                         "Re-classify only if a node was incorrectly flagged as data-bound."),
    "PLATFORM_NOISE":   ("Platform noise (DPI rounding, adapter schema variance)",
                         "Action: none expected; review only if a category seems mis-classified."),
}


def _fmt_diff_entry(d: Diff) -> list[str]:
    """Format a single Diff record as markdown list lines (one diff = multi-line block)."""
    out: list[str] = []
    head = f"- `{d.path}` · {d.type}"
    if d.match_strategy:
        head += f" · matched by {d.match_strategy}"
    out.append(head)
    if d.source_node:
        out.append(f"    - source: `{_fmt_node(d.source_node)}`")
    if d.target_node:
        out.append(f"    - target: `{_fmt_node(d.target_node)}`")
    det = d.details or {}
    if d.type == "kind_mismatch":
        out.append(f"    - kind: {det.get('source_kind')} → {det.get('target_kind')}")
    elif d.type == "text_mismatch":
        out.append(f"    - text: {det.get('source_text')!r} → {det.get('target_text')!r}")
    elif d.type == "state_mismatch":
        for k, vv in (det.get("changes") or {}).items():
            out.append(f"    - state.{k}: {vv.get('source')} → {vv.get('target')}")
    elif d.type == "bounds_drift":
        if "drift_rel_pct" in det:
            drifts = ", ".join(f"{k}={v}%" for k, v in det["drift_rel_pct"].items())
            out.append(f"    - drift: {drifts}  "
                       f"(pos≤{det.get('max_position_drift', 0)}%, "
                       f"size≤{det.get('max_size_drift', 0)}%)")
        else:
            out.append(f"    - {det.get('reason', 'unknown')}")
    return out


def render_markdown(result: DiffResult) -> str:
    """Human-readable diff report, organized as an actionable UI fix list.

    Top-level structure (per B.2.2 P1-B reframing):
      1. Header — source/target/matched stats
      2. Quick action summary — counts per severity category
      3. One section per severity in SEVERITY_ORDER, with fix guidance.
         PLATFORM_NOISE is rendered last and folded into a <details> block —
         visible (user explicitly wants noise DISPLAYED, not hidden) but
         de-prioritized.
    """
    s = result.summary
    by_sev: dict[str, list[Diff]] = {sev: [] for sev in SEVERITY_ORDER}
    for d in result.diffs:
        by_sev.setdefault(d.severity, []).append(d)
    sev_counts: dict[str, int] = s.get("by_severity") or {
        sev: len(by_sev.get(sev, [])) for sev in SEVERITY_ORDER
    }

    lines: list[str] = []
    lines.append(f"# UI diff: {result.source['page_id']} ↔ {result.target['page_id']}")
    lines.append("")
    src_total_raw = s.get("source_total_raw", result.source["node_count"])
    tgt_total_raw = s.get("target_total_raw", result.target["node_count"])
    chrome_note = ""
    if s.get("chrome_stripped"):
        src_chrome = src_total_raw - result.source["node_count"]
        tgt_chrome = tgt_total_raw - result.target["node_count"]
        chrome_note = (f" *(chrome stripped: -{src_chrome} source / "
                       f"-{tgt_chrome} target framework nodes)*")
    lines.append(f"- **Source**: `{result.source['platform']}` · {result.source['node_count']} nodes")
    lines.append(f"- **Target**: `{result.target['platform']}` · {result.target['node_count']} nodes{chrome_note}")
    ms = s['matched_by_strategy']
    lines.append(f"- **Matched**: {s['matched']}  "
                 f"(by id: {ms['id']}, "
                 f"kind+text: {ms['kind_text']}, "
                 f"slot_template: {ms.get('slot_template', 0)}, "
                 f"positional: {ms['positional']})")
    lines.append(f"- **Bounds tolerance**: ±{result.config['bounds_tol_pct']}% (viewport-relative)")
    lines.append("")

    # ---- Quick action summary
    lines.append("## Quick action summary")
    lines.append("")
    total = s.get("total", sum(sev_counts.values()))
    actionable = s.get("actionable_signal",
                       total - sum(sev_counts.get(k, 0) for k in _INFORMATIONAL_SEVERITIES))
    pct = s.get("signal_pct", 0.0)
    lines.append(f"**Actionable signal**: {actionable} of {total} diffs "
                 f"({pct}%) — excludes DATA_DIVERGENCE + PLATFORM_NOISE. "
                 f"Focus fixes on rows above the fold.")
    lines.append("")
    lines.append("Diff entries grouped by UI-design impact, in fix-priority order.")
    lines.append("")
    lines.append("| severity | count | fix priority |")
    lines.append("|---|---|---|")
    priority_label = {
        "MISSING_FEATURE":  "P0 — translated UI is incomplete",
        "EXTRA_FEATURE":    "P1 — extra element to review",
        "TEXT_FORMAT":      "P1 — align translation/unit conventions",
        "LAYOUT_PARADIGM":  "P1 — component kind decision",
        "LAYOUT_POSITION":  "P2 — visual alignment",
        "LAYOUT_SIZE":      "P2 — visual sizing",
        "BEHAVIOR":         "P1 — interaction affordance",
        "DATA_DIVERGENCE":  "informational — runtime data differs",
        "PLATFORM_NOISE":   "informational — expected noise",
    }
    for sev in SEVERITY_ORDER:
        lines.append(f"| **{sev}** | {sev_counts.get(sev, 0)} | {priority_label[sev]} |")
    lines.append("")

    # ---- One section per severity (non-noise first, noise last + folded)
    for sev in SEVERITY_ORDER:
        items = by_sev.get(sev, [])
        if not items:
            continue
        heading, guidance = _SEVERITY_HEADERS[sev]
        fold = sev in _INFORMATIONAL_SEVERITIES
        lines.append(f"## {sev} — {heading} ({len(items)})")
        lines.append("")
        lines.append(f"> {guidance}")
        lines.append("")
        if fold:
            lines.append(f"<details><summary>Show {len(items)} entries</summary>")
            lines.append("")
        for d in items[:50]:
            lines.extend(_fmt_diff_entry(d))
        if len(items) > 50:
            lines.append(f"- _… and {len(items) - 50} more (see diff.json)_")
        if fold:
            lines.append("")
            lines.append("</details>")
        lines.append("")

    return "\n".join(lines)


def _fmt_node(n: Optional[dict]) -> str:
    if not n:
        return ""
    parts = [n.get("kind", "?")]
    if n.get("id"): parts.append(f"#{n['id'].rsplit('/', 1)[-1]}")
    if n.get("text"): parts.append(f"“{n['text'][:30]}”")
    return " ".join(parts)
