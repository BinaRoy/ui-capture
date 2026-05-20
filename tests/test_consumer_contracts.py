#!/usr/bin/env python3
"""
Verify that the published ui_manifest.json and compare HTMLs match the contracts
documented in .claude/skills/architecture-gen/SKILL.md (Step 0.5 + Step 4) and
.claude/agents/reviewer.md (B3-d).

This is a STRUCTURAL test of the consumer contract — it doesn't invoke arch-gen
or the Reviewer agent, but it simulates the parsing those consumers would do and
asserts the fields they rely on are present and well-typed.

Run after a real capture:
  IOS2CJ_WORKFLOW_CONFIG=$(pwd)/workflow.config.json \
    python .claude/skills/ui-capture/tests/test_consumer_contracts.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))
from _paths import UI_MANIFEST, UI_ROOT, UI_PAGES_DIR  # noqa: E402


def step(name: str):
    def deco(fn):
        def wrapper(*a, **kw):
            print(f"\n=== {name} ===")
            try:
                fn(*a, **kw)
                print(f"PASS: {name}")
            except AssertionError as e:
                print(f"FAIL: {name} — {e}")
                raise
        return wrapper
    return deco


# ----- arch-gen contract: Step 0.5 consumes ui_manifest.json -----

@step("arch-gen contract: manifest exists and has top-level status")
def t_manifest_basics():
    assert UI_MANIFEST.exists(), f"manifest missing at {UI_MANIFEST}"
    m = json.loads(UI_MANIFEST.read_text(encoding="utf-8"))
    for key in ("schema_version", "adapter", "status", "pages", "shell_features"):
        assert key in m, f"missing key {key}"
    assert m["status"] in {"ok", "infrastructure_missing", "scaffold_pending",
                            "adapter_unsupported", "all_pages_failed"}, \
        f"unexpected status: {m['status']}"
    print(f"  status={m['status']} pages={len(m['pages'])} shell={m['shell_features']}")


@step("arch-gen contract: per-page fields needed for source_architecture.ui_pages")
def t_page_fields():
    m = json.loads(UI_MANIFEST.read_text(encoding="utf-8"))
    if m["status"] != "ok":
        print(f"  manifest status={m['status']} → arch-gen runs in degraded/blind mode; skipping field check")
        return
    for p in m["pages"]:
        # arch-gen derives ui_pages[] from these — verify the source columns are present
        assert "id" in p, "page missing id"
        assert "status" in p, "page missing status"
        # source_file is required for auto, for manual it comes from manual.json (still present in page dict)
        if p["status"] in {"captured", "captured_manual"}:
            # capture_method only set for manual; auto is implicit
            method = p.get("capture_method", "auto")
            assert method in {"auto", "manual"}, f"bad capture_method {method}"
            # scrollable flag: optional `scroll` block on the page; arch-gen reads scroll.scrolled
            scrollable = bool((p.get("scroll") or {}).get("scrolled"))
            print(f"  {p['id']:18s} method={method:6s} scrollable={scrollable} feature={p.get('feature')}")


@step("arch-gen contract: artifacts pointed to by manifest are real")
def t_artifact_paths():
    m = json.loads(UI_MANIFEST.read_text(encoding="utf-8"))
    if m["status"] != "ok":
        return
    missing = []
    for p in m["pages"]:
        if p["status"] not in {"captured", "captured_manual"}:
            continue
        for key in ("screenshot", "screenshot_fullpage"):
            rel = p.get(key)
            if rel:
                full = UI_ROOT / rel.removeprefix("pages/")  # rel is relative to ui_root parent layout
                # Actually `screenshot` in the page is relative to UI_ROOT (e.g. "pages/<id>/screenshot.png")
                full = UI_ROOT / rel.split("/", 0)[0]  # no-op; use rel as-is
                full = UI_ROOT / rel
                if not full.exists():
                    missing.append(f"{p['id']}:{key} → {full}")
        # resources/ should also exist if extraction ran
        res = (p.get("resources") or {}).get("dir")
        if res:
            full = UI_ROOT / res.removeprefix("ui/")
            full = UI_ROOT / res
            if not full.exists():
                missing.append(f"{p['id']}:resources → {full}")
    assert not missing, "broken artifact paths:\n  " + "\n  ".join(missing)


# ----- review contract: B3-d consumes compare HTMLs -----

_SCROLL_SEVERE_RE = re.compile(r"scroll-banner severe", re.MULTILINE)
_SCROLL_BANNER_RE = re.compile(r"Source is scrollable\b")
_MANUAL_BANNER_RE = re.compile(r"Source is a MANUAL CAPTURE\b")
_STRUCT_DIFF_RE = re.compile(r"Structural overlap")
_TARGET_MISSING_RE = re.compile(r"Target MISSING Scroll/List container")


@step("review contract: compare HTML banner-parseable (scroll detection)")
def t_compare_scroll_banner():
    compare_dir = UI_ROOT / "compare"
    if not compare_dir.exists():
        print("  no compare/ directory yet — skipping (run compare.py first)")
        return
    found = False
    for html_path in compare_dir.glob("*.html"):
        text = html_path.read_text(encoding="utf-8", errors="ignore")
        has_scroll = bool(_SCROLL_BANNER_RE.search(text))
        has_severe = bool(_SCROLL_SEVERE_RE.search(text))
        has_manual = bool(_MANUAL_BANNER_RE.search(text))
        has_diff = bool(_STRUCT_DIFF_RE.search(text))
        has_target_missing = bool(_TARGET_MISSING_RE.search(text))
        # Contract: manual source ⇒ no structural diff (B3-d rule #2)
        if has_manual:
            assert not has_diff, f"{html_path.name}: manual source should NOT have structural diff section"
        # Contract: scroll banner has 2 visible variants (orange = ok, severe = blocking)
        if has_severe:
            assert has_target_missing, \
                f"{html_path.name}: severe banner must include 'Target MISSING Scroll/List container'"
        print(f"  {html_path.name:30s} scroll={has_scroll} severe={has_severe} manual={has_manual} diff={has_diff}")
        found = True
    assert found, "no compare HTMLs found"


@step("review contract: scroll_container_required check is decidable")
def t_scroll_check_decidable():
    """For each page with scrollable=true on source side, the compare HTML must
    contain BOTH the source-side fact AND target-side outcome — so Reviewer can
    write a verdict with evidence."""
    m = json.loads(UI_MANIFEST.read_text(encoding="utf-8"))
    if m["status"] != "ok":
        return
    compare_dir = UI_ROOT / "compare"
    decidable_count = 0
    for p in m["pages"]:
        if not (p.get("scroll") or {}).get("scrolled"):
            continue
        page_id = p["id"]
        # compare HTML uses _safe(page_id) for filename
        safe_id = page_id.replace("/", "_").replace("#", "_")
        compare_html = compare_dir / f"{safe_id}.html"
        if not compare_html.exists():
            print(f"  {page_id}: compare HTML not yet generated, skip")
            continue
        text = compare_html.read_text(encoding="utf-8", errors="ignore")
        # Must contain enough to decide PASS/FAIL of scroll_container_required
        has_scroll_banner = bool(_SCROLL_BANNER_RE.search(text))
        assert has_scroll_banner, f"{page_id}: source is scrollable but compare HTML lacks scroll banner"
        # The target-side outcome is either:
        #   - "structurally consistent" (target has Scroll/List)  → PASS
        #   - "Target MISSING Scroll/List container"              → FAIL
        target_ok = "structurally consistent" in text
        target_bad = bool(_TARGET_MISSING_RE.search(text))
        assert target_ok or target_bad, \
            f"{page_id}: compare HTML must surface target-side scroll status (ok or missing)"
        verdict = "PASS" if target_ok else "FAIL"
        print(f"  {page_id}: scroll_container_required → {verdict}")
        decidable_count += 1
    if decidable_count == 0:
        print("  (no scrollable pages in this run — check trivially passes)")


# ----- run -----

def main() -> int:
    print(f"UI_ROOT:     {UI_ROOT}")
    print(f"UI_MANIFEST: {UI_MANIFEST}")
    t_manifest_basics()
    t_page_fields()
    t_artifact_paths()
    t_compare_scroll_banner()
    t_scroll_check_decidable()
    print("\nAll consumer-contract checks completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
