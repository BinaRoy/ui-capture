#!/usr/bin/env python3
"""Render hierarchy.json as a Markdown nested list — git-diff friendly.

One line per node; depth via indentation. Useful for code review where the
HTML view doesn't survive `git diff` cleanly. Each line is stable across
runs (no timestamps, no auto-generated IDs) so structural changes show up
as clean text diffs.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def render(ir: dict) -> str:
    lines: list[str] = []
    platform = ir.get("platform") or "?"
    lines.append(f"# hierarchy ({platform})")
    if ir.get("scrolled"):
        lines.append(f"")
        lines.append(f"- scrolled: True, viewport={ir.get('viewport_height')}px, "
                     f"fullpage={ir.get('page_total_height')}px, "
                     f"swipes={ir.get('scroll_steps')}")
    lines.append("")
    for c in ir.get("children", []):
        _render_node(c, depth=0, out=lines)
    return "\n".join(lines) + "\n"


def _render_node(node: dict, *, depth: int, out: list[str]) -> None:
    indent = "  " * depth
    kind = node.get("kind", "view")
    parts = [f"`{kind}`"]
    nid = node.get("id")
    if nid:
        parts.append(f"#{nid.rsplit('/', 1)[-1]}")
    text = (node.get("text") or "").strip()
    if text:
        parts.append(f'"{text[:60]}"')
    cd = (node.get("content_desc") or "").strip()
    if cd and cd != text:
        parts.append(f"(desc: {cd[:40]})")
    state = node.get("state") or {}
    state_bits = []
    if state.get("enabled") is False:
        state_bits.append("disabled")
    for k in ("checked", "selected", "focused", "password"):
        if state.get(k):
            state_bits.append(k)
    if state_bits:
        parts.append("[" + ",".join(state_bits) + "]")
    b = node.get("bounds")
    if isinstance(b, list) and len(b) == 4:
        parts.append(f"<{b[0]},{b[1]},{b[2]},{b[3]}>")
    out.append(f"{indent}- " + " ".join(parts))
    for c in node.get("children") or []:
        _render_node(c, depth=depth + 1, out=out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hierarchy", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    ir = json.loads(Path(args.hierarchy).read_text(encoding="utf-8"))
    Path(args.out).write_text(render(ir), encoding="utf-8")
    print(f"[hierarchy_md] → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
