#!/usr/bin/env python3
"""
Generate a side-by-side comparison HTML for a source page and its target Cangjie page.

Inputs:
  - source UI-IR / screenshot from <workflow.root>/ui/pages/<page_id>/
  - target UI-IR from        <workflow.root>/ui/target_pages/<slug>/

Pairing rule:
  - If --target is given, use that target page slug directly.
  - Else, try to match the source page_id to a target slug by:
      1. exact slug match (e.g. "weather_main" → "weather_main")
      2. feature mapping from ui_manifest.json (page.feature → matching target slug)
      3. fuzzy suffix match
  - Caller can override with --target.

Output:
  <workflow.root>/ui/compare/<page_id>.html
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _paths import (UI_ROOT, ensure_ui_dirs, ADAPTER_NAME,  # noqa: E402
                    derive_cross_page_dir, workflow_root_for)
from render_html import render as render_one_page, _short, _render_flow, _any_bounds  # noqa: E402
from diff_engine import compute_diff, render_markdown  # noqa: E402


def _safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in "_-." else "_" for c in s)


def find_target_slug(page_id: str, page_feature: Optional[str], target_root: Path) -> Optional[str]:
    if not target_root.exists():
        return None
    available = {p.name for p in target_root.iterdir() if p.is_dir()}
    if not available:
        return None

    # 1. exact slug match (page_id with '/' or '#' collapsed)
    candidates = [
        page_id.replace("/", "_").replace("#", "_"),
        page_id.split("/")[0],
        page_id.split("#")[0],
    ]
    for c in candidates:
        if c in available:
            return c

    # 2. feature-name match
    if page_feature and page_feature in available:
        return page_feature

    # 3. suffix overlap
    for slug in available:
        if any(c.endswith(slug) or slug.endswith(c) for c in candidates if c):
            return slug

    return None


def compare(page_id: str, target_slug: Optional[str] = None,
            *, dump_diff: bool = False, bounds_tol_pct: Optional[float] = None,
            target_workflow: Optional[Path] = None,
            target_adapter: Optional[str] = None) -> Path:
    """Render a compare report for `page_id`.

    Two modes:

    1. **Single-workflow legacy mode** (target_workflow is None):
       Reads target from `<workflow>/ui/target_pages/<slug>/` (same workflow root
       as source). Outputs into `<workflow>/ui/compare/<slug>.html` etc.

    2. **Cross-workflow mode** (target_workflow given):
       Reads target directly from `<target_workflow>/ui/pages/<slug>/`. Outputs
       go to the canonical cross-platform location:
           `<captures_parent>/output_cross/<source>_vs_<target>/<slug>/`
       The directory is auto-created. Skill is the single source of truth for
       this path — agent never needs to mkdir anything.
    """
    source_dir = UI_ROOT / "pages" / _safe(page_id)

    source_ir = None
    source_meta = {}
    source_screenshot_rel = None
    if (source_dir / "hierarchy.json").exists():
        source_ir = json.loads((source_dir / "hierarchy.json").read_text(encoding="utf-8"))
    if (source_dir / "meta.json").exists():
        source_meta = json.loads((source_dir / "meta.json").read_text(encoding="utf-8"))

    # ----- locate target (cross-workflow vs legacy) -----
    target_ir = None
    target_meta = {}
    target_dir: Optional[Path] = None

    if target_workflow is not None:
        # Cross-workflow mode. target_slug defaults to same slug as source.
        target_slug = target_slug or _safe(page_id)
        target_dir = Path(target_workflow) / "ui" / "pages" / target_slug
        if (target_dir / "hierarchy.json").exists():
            target_ir = json.loads((target_dir / "hierarchy.json").read_text(encoding="utf-8"))
        if (target_dir / "meta.json").exists():
            target_meta = json.loads((target_dir / "meta.json").read_text(encoding="utf-8"))
    else:
        # Legacy single-workflow mode.
        legacy_root = UI_ROOT / "target_pages"
        if target_slug is None:
            feature = source_meta.get("feature")
            target_slug = find_target_slug(page_id, feature, legacy_root)
        if target_slug:
            target_dir = legacy_root / target_slug
            if (target_dir / "hierarchy.json").exists():
                target_ir = json.loads((target_dir / "hierarchy.json").read_text(encoding="utf-8"))
            if (target_dir / "meta.json").exists():
                target_meta = json.loads((target_dir / "meta.json").read_text(encoding="utf-8"))

    # ----- decide output dir -----
    if target_workflow is not None:
        # Cross-platform: independent output_cross/<pair>/<slug>/ — skill-derived.
        # Infer target adapter from its config if not passed.
        if target_adapter is None:
            target_adapter = _infer_adapter_from_workflow(Path(target_workflow))
        out_dir = derive_cross_page_dir(_safe(page_id), target_adapter)
        out_filename = "compare.html"
        diff_json_name = "diff.json"
        diff_md_name = "diff.md"
        # Screenshot links must traverse from cross dir back to source/target workflow.
        source_screenshot_rel = _rel_screenshot(out_dir, source_dir)
        target_screenshot_rel = _rel_screenshot(out_dir, target_dir) if target_dir else None
    else:
        # Legacy: write under source workflow's compare/ dir.
        out_dir = UI_ROOT / "compare"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_filename = f"{_safe(page_id)}.html"
        diff_json_name = f"{_safe(page_id)}.diff.json"
        diff_md_name = f"{_safe(page_id)}.diff.md"
        if (source_dir / "screenshot_fullpage.png").exists():
            source_screenshot_rel = str(Path("..") / ".." / "pages" / _safe(page_id) / "screenshot_fullpage.png")
        elif (source_dir / "screenshot.png").exists():
            source_screenshot_rel = str(Path("..") / ".." / "pages" / _safe(page_id) / "screenshot.png")
        target_screenshot_rel = None

    out_path = out_dir / out_filename

    html_doc = _render_compare(
        page_id=page_id,
        source_ir=source_ir,
        source_meta=source_meta,
        source_screenshot_rel=source_screenshot_rel,
        target_slug=target_slug,
        target_ir=target_ir,
        target_meta=target_meta,
        target_screenshot_rel=target_screenshot_rel,
    )
    out_path.write_text(html_doc, encoding="utf-8")

    # diff.json + diff.md when both sides present
    if dump_diff and source_ir and target_ir and not source_ir.get("manual"):
        diff_result = compute_diff(
            source_ir, target_ir,
            bounds_tol_pct=bounds_tol_pct,
            source_page_id=page_id,
            target_page_id=target_slug or "",
        )
        (out_dir / diff_json_name).write_text(
            json.dumps(diff_result.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        (out_dir / diff_md_name).write_text(
            render_markdown(diff_result), encoding="utf-8",
        )
        # Cross-mode: drop a small meta.json so the pair dir is self-describing.
        if target_workflow is not None:
            (out_dir / "meta.json").write_text(json.dumps({
                "source_page": page_id,
                "target_page": target_slug,
                "source_adapter": ADAPTER_NAME,
                "target_adapter": target_adapter,
                "source_workflow": str(UI_ROOT.parent),
                "target_workflow": str(target_workflow),
            }, indent=2), encoding="utf-8")

    return out_path


def _rel_screenshot(out_dir: Path, page_dir: Path) -> Optional[str]:
    """Pick a screenshot from page_dir, return a relative path from out_dir."""
    for name in ("screenshot_fullpage.png", "screenshot.png"):
        p = page_dir / name
        if p.exists():
            try:
                return str(Path(*([".."] * len(out_dir.relative_to(out_dir.anchor).parts))) /
                           p.resolve().relative_to(Path(out_dir.anchor)))
            except Exception:
                # Fallback to absolute path — readable but less portable
                return str(p.resolve())
    return None


def _infer_adapter_from_workflow(workflow_root: Path) -> str:
    """Read <workflow_root>/../workflow.config.json to find adapter name.
    Falls back to inferring from directory name (output_<short>/...)."""
    cfg = workflow_root.parent / "workflow.config.json"
    if cfg.exists():
        try:
            return json.loads(cfg.read_text(encoding="utf-8")).get("adapter", {}).get("name", "")
        except Exception:
            pass
    # Heuristic: output_harmony → generic_harmony
    name = workflow_root.parent.name
    if name.startswith("output_"):
        return f"generic_{name[len('output_'):]}"
    return "unknown"


def _render_compare(*, page_id: str,
                    source_ir: Optional[dict], source_meta: dict,
                    source_screenshot_rel: Optional[str],
                    target_slug: Optional[str], target_ir: Optional[dict],
                    target_meta: dict,
                    target_screenshot_rel: Optional[str] = None) -> str:
    src_panel = _panel("Source (Android)", source_ir, source_meta,
                       screenshot_rel=source_screenshot_rel,
                       empty_msg="No source capture found for this page id.")
    tgt_panel = _panel(f"Target (Cangjie · {html.escape(target_slug or '—')})",
                       target_ir, target_meta,
                       screenshot_rel=target_screenshot_rel,
                       empty_msg=("Target page not yet generated. Either run capture "
                                  "in the target workflow, or use compare.py "
                                  "--target-workflow <path>."))

    # When source is a manual capture there is no real hierarchy → structural diff
    # is meaningless. Show a distinct banner and skip the diff table.
    is_manual_source = bool(source_ir and source_ir.get("manual"))
    manual_banner = ""
    if is_manual_source:
        manual_banner = (
            "<div class='manual-banner'>"
            "⚠ <b>Source is a MANUAL CAPTURE.</b> No view hierarchy is available — "
            "structural diff with the target is N/A. Use the screenshot for visual "
            "comparison and the source `resources/` folder for asset / string reference. "
            "Reviewers should not gate on structural-overlap counts here."
            "</div>"
        )

    # Banner if source is scrollable — Reviewer needs to verify target wraps in
    # Scroll() / List(). We also flag when SOURCE scrolls but TARGET seems flat:
    # that's a structural smell worth surfacing prominently.
    scroll_banner = "" if is_manual_source else _render_scroll_banner(source_ir, target_ir)

    diff_html = "" if is_manual_source else _structural_diff(source_ir, target_ir)

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>{html.escape(page_id)} — Source ↔ Target</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
          margin: 0; padding: 16px; color: #222; background: #fafafa; }}
  h1 {{ font-size: 18px; margin: 0 0 12px; }}
  h2 {{ font-size: 14px; margin: 0 0 8px; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; align-items: start; }}
  .panel {{ background: white; border: 1px solid #ddd; border-radius: 6px; padding: 12px;
            min-height: 200px; }}
  .empty {{ color: #999; font-style: italic; padding: 24px 4px; }}
  .meta {{ font-size: 11px; color: #555; margin-bottom: 8px; }}
  .meta b {{ color: #333; }}
  .shot img {{ max-width: 100%; border: 1px solid #ddd; display: block; }}
  .layout {{ position: relative; width: 360px; min-height: 60px;
             background: #f3f3f3; outline: 1px dashed #bbb; }}
  .layout-flow {{ position: static; padding: 4px; }}
  .layout-flow .node {{ display: block; margin: 2px 0; padding: 4px 6px; font-size: 11px;
                        background: rgba(0,128,255,0.04); outline: 1px solid rgba(0,0,0,0.1);
                        border-radius: 2px; }}
  .layout-flow .node.text {{ background: rgba(255,150,0,0.10); }}
  .layout-flow .node.button {{ background: rgba(180,0,180,0.08); }}
  .layout-flow .node.image {{ background: rgba(0,180,0,0.08); }}
  .layout-flow .node.list, .layout-flow .node.scroll {{ background: rgba(0,0,200,0.05); }}
  .layout-flow .node .label {{ font-weight: 600; color: #444; font-size: 10px; }}
  .layout-flow .node .body {{ color: #666; font-size: 11px; }}
  .node {{ position: absolute; box-sizing: border-box; outline: 1px solid rgba(0,0,0,0.15);
           background: rgba(0,128,255,0.04); font-size: 9px; padding: 1px 3px;
           overflow: hidden; }}
  .node.text {{ background: rgba(255,150,0,0.10); }}
  .node.button, .node.input {{ background: rgba(180,0,180,0.08); }}
  .tree {{ font: 11px/1.4 ui-monospace, Menlo, monospace; white-space: pre;
           background: #fafafa; border: 1px solid #eee; padding: 8px; border-radius: 4px;
           max-height: 400px; overflow: auto; }}
  .tree .k {{ color: #07a; }}
  .tree .t {{ color: #b50; }}
  .tree .i {{ color: #888; }}
  .diff {{ margin-top: 16px; background: white; border: 1px solid #ddd; border-radius: 6px; padding: 12px; }}
  .diff h2 {{ margin-top: 0; }}
  .diff table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
  .diff th, .diff td {{ text-align: left; padding: 4px 8px; border-bottom: 1px solid #eee; }}
  .diff .ok {{ color: #277; }}
  .diff .warn {{ color: #b50; }}
  .diff .miss {{ color: #c33; }}
  .diff code {{ background: #f0f0f0; padding: 1px 4px; border-radius: 2px; font-size: 11px; }}
  .scroll-banner {{ background: #fff5e0; border: 2px solid #d67000; color: #5a2e00;
                    padding: 10px 14px; margin: 0 0 16px; border-radius: 6px;
                    font-size: 13px; line-height: 1.5; }}
  .scroll-banner.severe {{ background: #ffe1e1; border-color: #c33; color: #802020; }}
  .scroll-banner code {{ background: #ffe7c2; padding: 1px 6px; border-radius: 3px;
                         font-family: ui-monospace, Menlo, monospace; font-size: 12px; }}
  .manual-banner {{ background: #e3f0ff; border: 2px solid #2867b5; color: #18375f;
                    padding: 10px 14px; margin: 0 0 16px; border-radius: 6px;
                    font-size: 13px; line-height: 1.5; }}
</style></head><body>
<h1>{html.escape(page_id)} — Source ↔ Target</h1>
{manual_banner}
{scroll_banner}
<div class="grid">
  {src_panel}
  {tgt_panel}
</div>
{diff_html}
</body></html>
"""


def _has_scrollable_kind(ir: Optional[dict]) -> bool:
    if not ir:
        return False
    def walk(n):
        if n.get("kind") in ("scroll", "list"):
            return True
        return any(walk(c) for c in n.get("children", []))
    return any(walk(c) for c in ir.get("children", []))


def _render_scroll_banner(source_ir: Optional[dict], target_ir: Optional[dict]) -> str:
    src_scrolled = bool(source_ir and source_ir.get("scrolled"))
    if not src_scrolled:
        return ""
    vh = source_ir.get("viewport_height", 0) if source_ir else 0
    ph = source_ir.get("page_total_height", 0) if source_ir else 0
    steps = source_ir.get("scroll_steps", 0) if source_ir else 0
    target_has_scroll = _has_scrollable_kind(target_ir)
    if target_ir is None:
        severity_class = ""
        target_note = ""
    elif target_has_scroll:
        severity_class = ""
        target_note = (
            " <b>Target check:</b> a Scroll/List container was detected in the Cangjie "
            "page — structurally consistent."
        )
    else:
        severity_class = " severe"
        target_note = (
            " <b>⚠ Target MISSING Scroll/List container.</b> The Cangjie page appears "
            "to render the content as a flat Column — the bottom portion of the page "
            "will be unreachable at runtime. Reviewer: block until fixed."
        )
    return (
        f"<div class='scroll-banner{severity_class}'>"
        f"⚠ <b>Source is scrollable.</b> "
        f"Page total {ph}px, viewport {vh}px, {steps} swipe(s). "
        f"In ArkUI/Cangjie the target page must wrap content with <code>Scroll() {{ … }}</code> "
        f"so users can reach the bottom.{target_note}"
        f"</div>"
    )


def _panel(title: str, ir: Optional[dict], meta: dict, *,
           screenshot_rel: Optional[str], empty_msg: str) -> str:
    if ir is None:
        return f"""<div class="panel">
  <h2>{html.escape(title)}</h2>
  <div class="empty">{html.escape(empty_msg)}</div>
</div>"""

    meta_bits: list[str] = []
    for key in ("top_component", "current_fragment", "class_name", "feature", "source_file", "status"):
        v = meta.get(key) or (meta.get("identity") or {}).get(key)
        if v:
            meta_bits.append(f"<b>{html.escape(key)}</b>: {html.escape(str(v))}")
    meta_html = "<div class='meta'>" + " &middot; ".join(meta_bits) + "</div>" if meta_bits else ""

    children = ir.get("children", [])
    layout_extra_style = ""
    if _any_bounds(children):
        # Use the same absolute renderer as the per-page HTML by delegating
        # to a small adaptation: render a self-contained "layout" block.
        layout_inner = _bounded_block(children)
        layout_class = "layout"
        # Match render_html.py: pin the canvas height to scaled max bottom so
        # absolute-positioned children don't visually overflow into the .tree
        # and .diff sections below. Without this, tall stitched pages (e.g.
        # weather_main fullpage 3214px) overflow ~1000px down and overlap.
        from render_html import _max_bottom, _estimate_device_width, CANVAS_WIDTH
        device_w = _estimate_device_width(children) or 1080
        scale = CANVAS_WIDTH / device_w
        mb = _max_bottom(children)
        if mb:
            layout_extra_style = f"height:{int(mb * scale) + 8}px;"
    else:
        layout_inner = "".join(_render_flow(c, depth=0) for c in children)
        layout_class = "layout layout-flow"

    tree_html = _tree(children, 0)

    shot = ""
    if screenshot_rel:
        shot = f"<div class='shot'><img src='{html.escape(screenshot_rel)}' alt='screenshot'/></div>"

    return f"""<div class="panel">
  <h2>{html.escape(title)}</h2>
  {meta_html}
  {shot}
  <div class="{layout_class}" style="{layout_extra_style}">{layout_inner}</div>
  <div class="tree" style="margin-top:8px">{tree_html}</div>
</div>"""


def _bounded_block(nodes: list[dict]) -> str:
    # Re-use the existing node renderer (absolute-positioned). Lazy import.
    from render_html import _render_node, _estimate_device_width, CANVAS_WIDTH
    device_w = _estimate_device_width(nodes) or 1080
    scale = CANVAS_WIDTH / device_w
    return "\n".join(_render_node(c, scale, depth=0) for c in nodes)


def _tree(nodes: list[dict], depth: int) -> str:
    out: list[str] = []
    for n in nodes:
        indent = "  " * depth
        kind = html.escape(n.get("kind", "view"))
        cls = html.escape(n.get("class", "").rsplit(".", 1)[-1])
        bits = [f"{indent}<span class='k'>{kind}</span> <span class='i'>{cls}</span>"]
        if n.get("text"):
            bits.append(f" <span class='t'>{html.escape(_short(n['text'], 40))}</span>")
        out.append("".join(bits))
        if n.get("children"):
            out.append(_tree(n["children"], depth + 1))
    return "\n".join(out)


# ----------------------------------------------------------- structural diff

def _structural_diff(source_ir: Optional[dict], target_ir: Optional[dict]) -> str:
    """Best-effort summary: count visible nodes by kind and surface gaps."""
    if not source_ir or not target_ir:
        return ""
    src_counts = _count_kinds(source_ir.get("children", []), {})
    tgt_counts = _count_kinds(target_ir.get("children", []), {})
    all_kinds = sorted(set(src_counts) | set(tgt_counts))

    rows: list[str] = []
    for k in all_kinds:
        s = src_counts.get(k, 0)
        t = tgt_counts.get(k, 0)
        if s == t:
            cls, note = "ok", "match"
        elif s == 0:
            cls, note = "warn", "target-only"
        elif t == 0:
            cls, note = "miss", "missing in target"
        else:
            cls, note = "warn", f"Δ {t - s:+d}"
        rows.append(
            f"<tr><td><code>{html.escape(k)}</code></td>"
            f"<td>{s}</td><td>{t}</td>"
            f"<td class='{cls}'>{note}</td></tr>"
        )
    src_texts = _collect_texts(source_ir.get("children", []))
    tgt_texts = _collect_texts(target_ir.get("children", []))
    missing_texts = [t for t in src_texts if t not in tgt_texts and len(t) >= 3]
    extra_texts = [t for t in tgt_texts if t not in src_texts and len(t) >= 3]

    text_html = ""
    if missing_texts or extra_texts:
        ml = "".join(f"<li><code>{html.escape(t)}</code></li>" for t in missing_texts[:20])
        el = "".join(f"<li><code>{html.escape(t)}</code></li>" for t in extra_texts[:20])
        text_html = (
            "<h2>Text content drift</h2>"
            "<div style='display:flex; gap:24px'>"
            f"<div><div style='font-size:11px;color:#888'>In source only</div><ul>{ml or '<li><i>—</i></li>'}</ul></div>"
            f"<div><div style='font-size:11px;color:#888'>In target only</div><ul>{el or '<li><i>—</i></li>'}</ul></div>"
            "</div>"
        )

    return f"""<div class="diff">
<h2>Structural overlap (node-kind counts)</h2>
<table><thead><tr><th>kind</th><th>source</th><th>target</th><th>note</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
{text_html}
</div>"""


def _count_kinds(nodes: list[dict], acc: dict) -> dict:
    for n in nodes:
        k = n.get("kind", "view")
        acc[k] = acc.get(k, 0) + 1
        _count_kinds(n.get("children", []), acc)
    return acc


def _collect_texts(nodes: list[dict]) -> list[str]:
    out: list[str] = []
    for n in nodes:
        t = n.get("text")
        if isinstance(t, str) and t.strip():
            out.append(t.strip())
        out.extend(_collect_texts(n.get("children", [])))
    return out


# ------------------------------------------------------------------- CLI

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--page", required=True, help="source page id, e.g. 'weather_main'")
    parser.add_argument("--target", help="explicit target slug (default: same as --page in cross mode; auto-match in legacy mode)")
    parser.add_argument("--target-workflow",
                        help=("Path to the target end's <workflow_output> dir "
                              "(or 'auto' / adapter name like 'generic_harmony'). "
                              "When given, output goes to output_cross/<pair>/<slug>/; "
                              "agent does NOT need to create any directories."))
    parser.add_argument("--target-adapter",
                        help="Adapter name of the target (e.g. generic_harmony). "
                             "Inferred from --target-workflow config if omitted.")
    parser.add_argument("--dump-diff", action="store_true",
                        help="also write diff.json + diff.md alongside the HTML")
    parser.add_argument("--bounds-tol-pct", type=float, default=None,
                        help="bounds drift tolerance percentage (default: 5 same-platform, 15 cross-platform)")
    args = parser.parse_args()
    ensure_ui_dirs()

    target_workflow: Optional[Path] = None
    if args.target_workflow:
        tw = args.target_workflow
        # Convenience: accept an adapter name and look it up.
        if tw.startswith("generic_") or "/" not in tw:
            try:
                target_workflow = workflow_root_for(tw if tw.startswith("generic_") else f"generic_{tw}")
            except Exception:
                target_workflow = Path(tw).resolve()
        else:
            target_workflow = Path(tw).expanduser().resolve()
        if not target_workflow.exists():
            print(f"[compare] target workflow does not exist: {target_workflow}", file=sys.stderr)
            return 2

    out = compare(args.page, target_slug=args.target,
                  dump_diff=args.dump_diff,
                  bounds_tol_pct=args.bounds_tol_pct,
                  target_workflow=target_workflow,
                  target_adapter=args.target_adapter)
    print(f"[compare] → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
