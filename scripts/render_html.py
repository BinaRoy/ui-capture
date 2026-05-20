#!/usr/bin/env python3
"""
Render a UI-IR (hierarchy.json) tree to a human-readable HTML projection.

Goals:
- One self-contained HTML file per page (CSS inline, no external assets except
  a sibling screenshot.png shown alongside the tree).
- Structure-faithful: nested <div> hierarchy mirrors the IR tree.
- Bounds become absolute-positioned overlays so reviewers can visually align with
  the screenshot. Coordinates are scaled to fit a fixed canvas width.

Non-goals: pixel-perfect rendering. This is a *structural* projection, not a screenshot.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path
from typing import Optional


CANVAS_WIDTH = 360  # logical px in the HTML; bounds get scaled to fit


def render(
    ir: dict, *, page_id: str, identity: Optional[dict], screenshot_rel: Optional[str],
    manual_meta: Optional[dict] = None,
) -> str:
    _NODE_ID_COUNTER[0] = 0  # stable, deterministic ids per render call
    children = ir.get("children", [])
    has_bounds = _any_bounds(children)
    layout_extra_style = ""
    if has_bounds:
        device_w = _estimate_device_width(children) or 1080
        scale = CANVAS_WIDTH / device_w
        nodes_html = "\n".join(_render_node(c, scale, depth=0) for c in children)
        layout_class = "layout"
        # Bound the canvas to the deepest node so absolute-positioned children
        # past ~800px (the CSS min-height) aren't clipped — most visible on
        # stitched full-page captures.
        max_bottom = _max_bottom(children)
        if max_bottom:
            canvas_h = int(max_bottom * scale) + 8
            layout_extra_style = f"height:{canvas_h}px;"
    else:
        # No bounds (target side): fall back to a nested-block flow layout that
        # mirrors the hierarchy. Useful for visual structural comparison even
        # without absolute coordinates.
        nodes_html = "\n".join(_render_flow(c, depth=0) for c in children)
        layout_class = "layout layout-flow"

    identity_block = ""
    if identity:
        items = []
        for k in ("top_component", "current_fragment", "source_file"):
            v = identity.get(k)
            if v:
                items.append(f"<li><b>{html.escape(k)}</b>: {html.escape(str(v))}</li>")
        identity_block = "<ul class='ident'>" + "".join(items) + "</ul>"

    # Loud banner when the page was captured manually (no real hierarchy / dump).
    # This trumps the scroll banner — manual pages don't have meaningful structure
    # data, so consumers must rely on the screenshot + source_file + resources/.
    manual_banner = ""
    if ir.get("manual") or manual_meta:
        meta = manual_meta or {}
        warnings_html = ""
        if meta.get("warnings"):
            warnings_html = "<ul>" + "".join(
                f"<li>{html.escape(w)}</li>" for w in meta["warnings"]
            ) + "</ul>"
        notes_html = ""
        if meta.get("notes"):
            notes_html = f"<div><b>Operator note:</b> {html.escape(meta['notes'])}</div>"
        status = meta.get("status", "captured_manual")
        manual_banner = (
            "<div class='manual-banner'>"
            f"⚠ <b>MANUAL CAPTURE</b> — status: <code>{html.escape(status)}</code>. "
            "This page was provided by the operator; <b>no view hierarchy is available</b> "
            "(no uiautomator dump). "
            "Worker: rely on the screenshot + <code>source_file</code> + <code>resources/</code> "
            "folder as the primary references. Structural diff against the target Cangjie "
            "page is not possible — only visual comparison."
            f"{notes_html}{warnings_html}"
            "</div>"
        )

    # Loud banner when the page is scrollable. Mitigation D: even readers who don't
    # parse the IR or look at the stitched image's fold lines will see this banner
    # and know to wrap the content in Scroll() in ArkUI.
    scroll_banner = ""
    if ir.get("scrolled") and not ir.get("manual"):
        vh = ir.get("viewport_height", 0)
        ph = ir.get("page_total_height", 0)
        steps = ir.get("scroll_steps", 0)
        scroll_banner = (
            "<div class='scroll-banner'>"
            "⚠ <b>Scrollable page.</b> "
            f"Full page is <b>{ph}px</b> tall, viewport is <b>{vh}px</b> "
            f"({steps} swipe(s) needed to reveal all content). "
            "The image below is a <b>stitched composite</b> of multiple scroll positions — "
            "do NOT implement as a single flat layout. "
            "In ArkUI/Cangjie, wrap the scrolling content with <code>Scroll() { … }</code>. "
            "Look for the dashed red <b>SCROLL FOLD</b> lines in the image to see where the user must scroll."
            "</div>"
        )

    screenshot_block = ""
    if screenshot_rel:
        screenshot_block = (
            f"<div class='shot'><img src='{html.escape(screenshot_rel)}' "
            f"alt='screenshot' style='max-width:{CANVAS_WIDTH}px'/></div>"
        )

    tree_html = _render_tree(children, depth=0)

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>{html.escape(page_id)} — UI capture</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
          margin: 0; padding: 16px; color: #222; background: #fafafa; }}
  h1 {{ font-size: 18px; margin: 0 0 8px; }}
  .panes {{ display: flex; gap: 16px; align-items: flex-start; }}
  .pane {{ background: white; border: 1px solid #ddd; border-radius: 6px; padding: 12px; }}
  .layout {{ position: relative; width: {CANVAS_WIDTH}px;
             min-height: 100px; background: #f3f3f3; outline: 1px dashed #bbb; }}
  .layout-flow {{ position: static; padding: 4px; }}
  .layout-flow .node {{ position: static; display: block; margin: 2px 0;
                        padding: 4px 6px; font-size: 11px; white-space: normal; }}
  .layout-flow .node.row > .node {{ display: inline-block; vertical-align: top;
                                     margin-right: 4px; }}
  .layout-flow .node .label {{ font-weight: 600; color: #444; font-size: 10px; }}
  .layout-flow .node .body {{ color: #666; font-size: 11px; }}
  .node {{ position: absolute; box-sizing: border-box; outline: 1px solid rgba(0,0,0,0.15);
           background: rgba(0,128,255,0.04); font-size: 9px; padding: 1px 3px;
           overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }}
  .node.text {{ background: rgba(255,150,0,0.10); outline-color: rgba(200,80,0,0.4); }}
  .node.image {{ background: rgba(0,180,0,0.08); outline-color: rgba(0,140,0,0.4); }}
  .node.button, .node.input {{ background: rgba(180,0,180,0.08); outline-color: rgba(140,0,140,0.4); }}
  .node.list, .node.scroll, .node.pager {{ background: rgba(0,0,200,0.05); outline-color: rgba(0,0,180,0.4); }}
  .tree {{ font: 11px/1.4 ui-monospace, Menlo, monospace; white-space: pre; }}
  .tree .k {{ color: #07a; }}
  .tree .t {{ color: #b50; }}
  .tree .i {{ color: #888; }}
  ul.ident {{ list-style: none; padding: 0; margin: 0 0 8px; font-size: 12px; }}
  ul.ident li {{ margin: 2px 0; }}
  .shot img {{ display: block; border: 1px solid #ddd; }}
  .scroll-banner {{ background: #fff5e0; border: 2px solid #d67000; color: #5a2e00;
                    padding: 10px 14px; margin: 0 0 12px; border-radius: 6px;
                    font-size: 13px; line-height: 1.5; }}
  .scroll-banner code {{ background: #ffe7c2; padding: 1px 6px; border-radius: 3px;
                         font-family: ui-monospace, Menlo, monospace; font-size: 12px; }}
  .manual-banner {{ background: #e3f0ff; border: 2px solid #2867b5; color: #18375f;
                    padding: 10px 14px; margin: 0 0 12px; border-radius: 6px;
                    font-size: 13px; line-height: 1.5; }}
  .manual-banner code {{ background: #c9dcf3; padding: 1px 6px; border-radius: 3px;
                         font-family: ui-monospace, Menlo, monospace; font-size: 12px; }}
  .manual-banner ul {{ margin: 6px 0 0; padding-left: 20px; }}
  .manual-banner li {{ margin: 2px 0; }}
  .tree .row {{ cursor: pointer; padding: 0 2px; border-radius: 2px; }}
  .tree .row:hover {{ background: #fff3c2; }}
  .node.hilite {{ outline: 3px solid #f70 !important; background: rgba(255,180,0,0.35) !important;
                  z-index: 10; }}
</style></head><body>
<h1>{html.escape(page_id)}</h1>
{manual_banner}
{scroll_banner}
{identity_block}
<div class="panes">
  <div class="pane">
    <div style="font-size:11px;color:#888;margin-bottom:6px">Structural projection</div>
    <div class="{layout_class}" style="{layout_extra_style}">
      {nodes_html}
    </div>
  </div>
  {screenshot_block}
  <div class="pane">
    <div style="font-size:11px;color:#888;margin-bottom:6px">Hierarchy tree — click a row to highlight its bounds</div>
    <div class="tree">{tree_html}</div>
  </div>
</div>
<script>
(() => {{
  let prev = null;
  document.querySelectorAll('.tree .row').forEach(row => {{
    row.addEventListener('click', () => {{
      const t = row.getAttribute('data-target');
      if (!t) return;
      if (prev) prev.classList.remove('hilite');
      const target = document.getElementById(t);
      if (target) {{
        target.classList.add('hilite');
        target.scrollIntoView({{block: 'center', behavior: 'smooth'}});
        prev = target;
      }}
    }});
  }});
}})();
</script>
</body></html>
"""


def _any_bounds(nodes: list[dict]) -> bool:
    for n in nodes:
        if n.get("bounds"):
            return True
        if _any_bounds(n.get("children", [])):
            return True
    return False


def _render_flow(node: dict, depth: int) -> str:
    """No-bounds nested-block renderer. Indents children inside parent blocks."""
    kind = node.get("kind", "view")
    cls = f"node {kind}"
    label_bits = [html.escape(node.get("class", kind))]
    if node.get("builder_ref"):
        label_bits.append(f"<i style='color:#888'>@Builder {html.escape(node['builder_ref'])}</i>")
    body_bits: list[str] = []
    text = node.get("text") or ""
    if text:
        body_bits.append(f"<span style='color:#b50'>“{html.escape(_short(text, 60))}”</span>")
    args = node.get("args")
    if args and not text:
        body_bits.append(f"<span style='color:#666'>{html.escape(_short(args, 60))}</span>")
    mods = node.get("modifiers") or {}
    if mods:
        # show a few key modifiers inline
        keys = [k for k in ("width", "height", "fontSize", "fontColor", "padding", "margin",
                            "backgroundColor", "alignItems", "justifyContent") if k in mods][:4]
        if keys:
            body_bits.append("<span style='color:#888;font-size:10px'>" +
                             " ".join(f"{html.escape(k)}={html.escape(_short(str(mods[k]), 18))}" for k in keys) +
                             "</span>")
    children_html = "".join(_render_flow(c, depth + 1) for c in node.get("children", []))
    indent_style = f"margin-left:{depth * 12}px;" if kind != "row" else ""
    return (f"<div class='{cls}' style='{indent_style}'>"
            f"<span class='label'>{' · '.join(label_bits)}</span>"
            f" <span class='body'>{' '.join(body_bits)}</span>"
            f"{children_html}"
            f"</div>")


def _estimate_device_width(nodes: list[dict]) -> Optional[int]:
    best = 0
    for n in nodes:
        b = n.get("bounds")
        if b and len(b) == 4:
            best = max(best, b[2])
    return best or None


def _max_bottom(nodes: list[dict]) -> int:
    best = 0
    for n in nodes:
        b = n.get("bounds")
        if b and len(b) == 4:
            best = max(best, b[3])
        best = max(best, _max_bottom(n.get("children", [])))
    return best


_NODE_ID_COUNTER = [0]


def _alloc_node_id() -> str:
    _NODE_ID_COUNTER[0] += 1
    return f"n{_NODE_ID_COUNTER[0]}"


def _render_node(node: dict, scale: float, depth: int) -> str:
    bounds = node.get("bounds")
    parts = []
    if bounds and len(bounds) == 4:
        l, t, r, b = bounds
        style = (
            f"left:{int(l*scale)}px;top:{int(t*scale)}px;"
            f"width:{max(1,int((r-l)*scale))}px;height:{max(1,int((b-t)*scale))}px;"
        )
        label = node.get("text") or node.get("content_desc") or node.get("id") or node.get("class", "")
        cls = f"node {node.get('kind','view')}"
        nid = node.get("_render_id") or _alloc_node_id()
        node["_render_id"] = nid
        parts.append(
            f"<div class='{cls}' id='{nid}' data-node='{nid}' style='{style}' "
            f"title='{html.escape(node.get('class', ''))}'>{html.escape(_short(label))}</div>"
        )
    for child in node.get("children", []):
        parts.append(_render_node(child, scale, depth + 1))
    return "".join(parts)


def _fmt_state(state: dict) -> str:
    """Compact one-line state summary, highlighting non-default values."""
    if not state:
        return ""
    bits = []
    # Negative-style: things being false when they "should" be true (enabled)
    if state.get("enabled") is False:
        bits.append("<span style='color:#c33;text-decoration:underline'>disabled</span>")
    # Positive-style: notable enabled flags
    for k in ("checked", "selected", "focused", "password"):
        if state.get(k):
            bits.append(f"<span style='color:#066'>{k}</span>")
    return " ".join(bits)


def _render_tree(nodes: list[dict], depth: int) -> str:
    out: list[str] = []
    for n in nodes:
        indent = "  " * depth
        kind = html.escape(n.get("kind", "view"))
        cls = html.escape(n.get("class", "").rsplit(".", 1)[-1])
        # Each tree row carries data-target so a click can highlight the
        # matching absolute-positioned node in the structural projection.
        render_id = n.get("_render_id") or ""
        attrs = f" data-target='{render_id}'" if render_id else ""
        bits = [f"{indent}<span class='row'{attrs}><span class='k'>{kind}</span> <span class='i'>{cls}</span>"]
        text = n.get("text")
        if text:
            bits.append(f" <span class='t'>{html.escape(_short(text))}</span>")
        nid = n.get("id")
        if nid:
            bits.append(f" <span class='i'>#{html.escape(nid.rsplit('/',1)[-1])}</span>")
        state_html = _fmt_state(n.get("state") or {})
        if state_html:
            bits.append(f" {state_html}")
        bits.append("</span>")
        out.append("".join(bits))
        if n.get("children"):
            out.append(_render_tree(n["children"], depth + 1))
    return "\n".join(out)


def _short(text: str, limit: int = 40) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hierarchy", required=True, help="path to hierarchy.json")
    parser.add_argument("--out", required=True, help="output HTML path")
    parser.add_argument("--page-id", required=True)
    parser.add_argument("--identity", help="path to meta.json or identity.json")
    parser.add_argument("--screenshot", help="relative path to screenshot.png next to output")
    args = parser.parse_args()

    ir = json.loads(Path(args.hierarchy).read_text(encoding="utf-8"))
    identity = None
    if args.identity and Path(args.identity).exists():
        identity = json.loads(Path(args.identity).read_text(encoding="utf-8"))
        identity = identity.get("identity", identity)

    out_html = render(ir, page_id=args.page_id, identity=identity, screenshot_rel=args.screenshot)
    Path(args.out).write_text(out_html, encoding="utf-8")
    print(f"[render_html] → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
