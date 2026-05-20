#!/usr/bin/env python3
"""Generate an SVG overlay of hierarchy bounds — to be layered on top of
screenshot_fullpage.png in image viewers or external tools.

Each leaf node becomes a <rect> with stroke color keyed to its kind. The SVG
viewBox matches the source screenshot in device pixels, so opening it side-by-
side with the PNG (or layering with `magick composite`) gives a structural
trace without the HTML projection's CSS-rendered approximation.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional


_COLORS = {
    "text":      "#c84500",
    "image":     "#0a8030",
    "button":    "#8c008c",
    "input":     "#8c008c",
    "list":      "#0044aa",
    "scroll":    "#0044aa",
    "pager":     "#0044aa",
    "switch":    "#aa6600",
    "checkbox":  "#aa6600",
    "radio":     "#aa6600",
    "progress":  "#888888",
}
_DEFAULT_COLOR = "#3a3a3a"


def render(ir: dict, *, page_width: Optional[int] = None,
           page_height: Optional[int] = None) -> str:
    children = ir.get("children", [])
    # Auto-bounds from tree if not given
    if page_width is None or page_height is None:
        w, h = _auto_bounds(children)
        page_width = page_width or w
        page_height = page_height or ir.get("page_total_height") or h

    rects: list[str] = []
    _emit(children, rects)

    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {page_width} {page_height}" '
        f'width="{page_width}" height="{page_height}">\n'
        f'  <style>rect {{ fill: none; stroke-width: 2; }} '
        f'text {{ font: 18px sans-serif; fill: #222; }}</style>\n'
        + "\n".join(rects)
        + "\n</svg>\n"
    )


def _auto_bounds(nodes: list[dict]) -> tuple[int, int]:
    right = bottom = 0
    def walk(nl):
        nonlocal right, bottom
        for n in nl:
            b = n.get("bounds")
            if isinstance(b, list) and len(b) == 4:
                right = max(right, b[2])
                bottom = max(bottom, b[3])
            walk(n.get("children") or [])
    walk(nodes)
    return right or 1080, bottom or 2400


def _emit(nodes: list[dict], out: list[str]) -> None:
    for n in nodes:
        b = n.get("bounds")
        if isinstance(b, list) and len(b) == 4 and not (n.get("children")):
            # Leaf only — drawing every container would obscure the page
            kind = n.get("kind", "view")
            color = _COLORS.get(kind, _DEFAULT_COLOR)
            l, t, r, bot = b
            w, h = max(1, r - l), max(1, bot - t)
            label = (n.get("text") or n.get("id") or kind).replace("&", "&amp;").replace("<", "&lt;")[:30]
            out.append(
                f'  <g><rect x="{l}" y="{t}" width="{w}" height="{h}" stroke="{color}"/>'
                f'<title>{kind}: {label}</title></g>'
            )
        _emit(n.get("children") or [], out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hierarchy", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    ir = json.loads(Path(args.hierarchy).read_text(encoding="utf-8"))
    Path(args.out).write_text(render(ir), encoding="utf-8")
    print(f"[svg_overlay] → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
