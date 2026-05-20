"""Nav-graph builder for HarmonyOS / Cangjie projects.

Takes the static output of `auto_resolve_nav.py` (`nav_resolution.json`) plus
any already-captured page hierarchies, and emits a `nav_plan.json` that maps
each page to a concrete navigation step: either a tap coordinate against a
parent page, or a TODO status describing what's missing.

This is the second half of the Cangjie page-capture pipeline:

  discover_hints  →  auto_resolve_nav  →  [this module]  →  nav_script

The runtime capture loop (future B.1.7 sub-step) will iterate over the plan,
capture the parent pages it points to, and refine plan entries that started
in `needs_parent_capture` state.

Matching strategy for trigger_label → hierarchy bounds:
    1. exact text equality
    2. NFKC-normalized + trimmed equality
    3. emoji-stripped equality
    4. substring containment (label ⊂ node text, or node text ⊂ label)

Anything still unmatched lands in plan as `status=label_not_found`.
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Optional

# Unicode emoji + variation selector cleanup. Conservative: matches common
# pictographic blocks; tolerable false positives are stripping rare symbols.
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF"   # symbols & pictographs, emoticons, transport
    "\U00002600-\U000027BF"    # misc symbols, dingbats
    "️]+"                 # variation selectors
)


def _normalize(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    return s.strip()


def _strip_emoji(s: str) -> str:
    return _EMOJI_RE.sub("", s).strip()


def _iter_text_nodes(node: dict):
    if node.get("text"):
        yield node
    for c in node.get("children") or []:
        yield from _iter_text_nodes(c)


def find_tap_target(hierarchy: dict, label: str) -> Optional[dict]:
    """Locate the smallest text node whose text matches `label`.

    Returns dict {x, y, bounds, matched_text, match_mode} or None.
    `x, y` is the center of the node's bounds — what `uitest uiInput click` wants.
    """
    if not label:
        return None
    targets = list(_iter_text_nodes(hierarchy))
    if not targets:
        return None

    label_n = _normalize(label)
    label_ne = _strip_emoji(label_n)

    # Score each candidate; lower score is a better match.
    best: Optional[tuple[int, dict, str]] = None
    for node in targets:
        text = node.get("text") or ""
        text_n = _normalize(text)
        if not text_n:
            continue
        text_ne = _strip_emoji(text_n)

        if text_n == label_n:
            mode = "exact"
            score = 0
        elif text_ne and text_ne == label_ne:
            mode = "emoji_stripped"
            score = 10
        elif label_n in text_n and len(label_n) >= 4:
            # Avoid matching short labels (›, OK, Go) inside long node text.
            mode = "label_substring_of_node"
            score = 20 + len(text_n) - len(label_n)
        elif (text_n in label_n
              and len(text_n) >= 4
              and len(text_n) >= 0.6 * len(label_n)):
            # Avoid matching the launcher's "Telegram" header to "Telegram Passport".
            # Require the node text to cover ≥60% of the label.
            mode = "node_substring_of_label"
            score = 30 + len(label_n) - len(text_n)
        else:
            continue

        if best is None or score < best[0]:
            best = (score, node, mode)

    if best is None:
        return None

    _, node, mode = best
    bounds = node.get("bounds")
    if not (isinstance(bounds, list) and len(bounds) == 4):
        return None
    x1, y1, x2, y2 = bounds
    return {
        "x": (x1 + x2) // 2,
        "y": (y1 + y2) // 2,
        "bounds": bounds,
        "matched_text": node.get("text"),
        "match_mode": mode,
    }


# --------------------------------------------------------------------- plan


def _classify_status(res: dict, tap: Optional[dict],
                     captured: set[str],
                     aliases: dict[str, str]) -> str:
    via = res.get("via", "unresolved")
    if via == "cangjie_router":
        if tap:
            return "ready"
        if not res.get("trigger_label"):
            return "needs_runtime_anchor"  # label missing entirely
        parent_slug = _slug_from_trigger_file(res.get("trigger_file"))
        effective = aliases.get(parent_slug or "", parent_slug)
        if effective and effective in captured:
            return "label_not_found"       # parent captured but text didn't match
        return "needs_parent_capture"      # haven't captured the parent yet
    if via == "conditional_render":
        parent_slug = _slug_from_trigger_file(res.get("trigger_file"))
        if parent_slug and parent_slug in captured:
            return "needs_state_flip"      # parent captured, but no state-flip plan yet
        return "needs_parent_capture"
    if via == "manifest_intent":
        return "launcher"
    return "unresolved"


def _slug_from_trigger_file(trigger_file: Optional[str]) -> Optional[str]:
    if not trigger_file:
        return None
    stem = Path(trigger_file).stem
    # Convert PascalCase / camelCase / index → slug roughly matching adapter._slugify
    s = re.sub(r"(?<!^)(?=[A-Z])", "_", stem).lower()
    return s


def build_nav_plan(resolution_path: Path,
                   pages_root: Path) -> dict:
    """Build a nav plan.

    `resolution_path`: nav_resolution.json
    `pages_root`: directory containing `<page_id>/hierarchy.json` for already-
                  captured pages. Missing files are OK — those pages just stay
                  in `needs_parent_capture` state.
    """
    data = json.loads(resolution_path.read_text(encoding="utf-8"))
    resolutions: dict = data["resolutions"]

    # Index captured hierarchies by their on-disk page_id.
    captured: dict[str, dict] = {}
    if pages_root.exists():
        for hier in pages_root.glob("*/hierarchy.json"):
            try:
                captured[hier.parent.name] = json.loads(hier.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue

    # Build alias map: pages whose trigger_file is conceptually rendered at
    # a different captured page. e.g. SplashPage.cj is rendered inside the
    # @Entry's `entryview` capture when !isLoggedIn, so triggers living in
    # SplashPage.cj should resolve against entryview's hierarchy.
    # Source of truth: resolutions where via=conditional_render whose parent
    # is index.cj (the @Entry file) — those children inherit the entryview
    # capture as their visual root.
    aliases: dict[str, str] = {}
    for res in resolutions.values():
        if res.get("via") != "conditional_render":
            continue
        # Conditional renders nested inside @Entry (index.cj) ride on entryview.
        parent_tf = res.get("trigger_file") or ""
        if parent_tf.endswith("/index.cj") or Path(parent_tf).name == "index.cj":
            child_slug = _slug_from_trigger_file(
                f"placeholder/{res['hint_class']}.cj"
            )
            if child_slug:
                aliases[child_slug] = "entryview"

    plan: dict[str, dict] = {}
    for page_id, res in resolutions.items():
        via = res.get("via", "unresolved")
        entry: dict = {
            "hint_class": res.get("hint_class"),
            "via": via,
            "trigger_label": res.get("trigger_label"),
            "trigger_method": res.get("trigger_method"),
            "trigger_file": res.get("trigger_file"),
            "parent": None,
            "tap": None,
            "command": None,
        }

        if via == "cangjie_router":
            parent_slug = _slug_from_trigger_file(res.get("trigger_file"))
            # Resolve parent through the alias map (e.g. splashpage → entryview).
            effective_parent = aliases.get(parent_slug or "", parent_slug)
            entry["parent"] = effective_parent
            parent_hier = captured.get(effective_parent or "")
            if parent_hier and res.get("trigger_label"):
                tap = find_tap_target(parent_hier, res["trigger_label"])
                if tap:
                    entry["tap"] = tap
                    entry["command"] = (
                        f"hdc shell uitest uiInput click {tap['x']} {tap['y']}"
                    )

        elif via == "conditional_render":
            parent_slug = _slug_from_trigger_file(res.get("trigger_file"))
            entry["parent"] = parent_slug

        entry["status"] = _classify_status(res, entry["tap"],
                                           set(captured.keys()), aliases)
        plan[page_id] = entry

    return {
        "schema_version": 1,
        "source_resolution": str(resolution_path),
        "pages_root": str(pages_root),
        "captured_pages": sorted(captured.keys()),
        "plan": plan,
        "stats": _summarize(plan),
    }


def _summarize(plan: dict) -> dict:
    from collections import Counter
    return dict(Counter(e["status"] for e in plan.values()))


# ---------------------------------------------------------------------- CLI


def main(argv: list[str]) -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--resolution", required=True,
                   help="Path to nav_resolution.json")
    p.add_argument("--pages-root", required=True,
                   help="Directory containing <page_id>/hierarchy.json")
    p.add_argument("--out", required=True, help="Output nav_plan.json")
    args = p.parse_args(argv)

    plan = build_nav_plan(Path(args.resolution).resolve(),
                          Path(args.pages_root).resolve())
    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[nav-graph] {len(plan['plan'])} pages, status: {plan['stats']} -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
