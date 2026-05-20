#!/usr/bin/env python3
"""
Fixture-based dry run for ui-capture. Exercises:
  - Android adapter static discovery against the real weather_app input
  - normalize() against the canned uiautomator XML fixture
  - render_html() output (sanity, not pixel-perfect)
  - feature_map.attach_features() against real feature.json (if present)

Does NOT need an emulator or adb. Run as:
  IOS2CJ_WORKFLOW_CONFIG=$(pwd)/workflow.config.json \
    python .claude/skills/ui-capture/tests/test_dryrun.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT))
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from adapters.android import AndroidAdapter  # noqa: E402
from _paths import SOURCE_ROOT  # noqa: E402
from feature_map import attach_features, resolve_source_file_from_class  # noqa: E402
from render_html import render  # noqa: E402


FIXTURE = SKILL_ROOT / "fixtures" / "sample_uiautomator_dump.xml"


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
            except Exception as e:
                print(f"ERROR: {name} — {type(e).__name__}: {e}")
                raise
        return wrapper
    return deco


@step("Android adapter static discovery")
def t_discover():
    adapter = AndroidAdapter()
    hints = adapter.discover_hints(SOURCE_ROOT)
    print(f"  found {len(hints)} hints")
    classes = {h.class_name for h in hints}
    # The weather_app should expose these — adjust if your input differs
    expected_subset = {"MainActivity", "MainFragment", "TodayWeatherFragment",
                       "SettingsFragment", "MainSettingsFragment", "SearchMenuFragment"}
    missing = expected_subset - classes
    if missing:
        print(f"  WARNING: expected classes not found in hints: {missing}")
    for h in hints[:15]:
        print(f"  - {h.class_name:35s} {h.origin:20s} {h.source_file}")
    assert len(hints) > 0, "no hints discovered at all"


@step("normalize uiautomator XML fixture")
def t_normalize():
    adapter = AndroidAdapter()
    ir = adapter.normalize(FIXTURE)
    assert ir.get("kind") == "root"
    assert ir.get("platform") == "android"
    assert isinstance(ir.get("children"), list) and len(ir["children"]) > 0
    # Walk and find a text node with "Berlin"
    found = []
    def walk(n):
        if n.get("text"):
            found.append(n["text"])
        for c in n.get("children", []):
            walk(c)
    for c in ir["children"]:
        walk(c)
    assert "Berlin" in found, f"expected 'Berlin' in normalized text, got {found}"
    assert "22°" in found, f"expected '22°' in normalized text"
    print(f"  normalized OK, found texts: {found}")


@step("render HTML from normalized IR")
def t_render():
    adapter = AndroidAdapter()
    ir = adapter.normalize(FIXTURE)
    out = render(
        ir,
        page_id="weather_main",
        identity={"top_component": "MainActivity",
                  "current_fragment": "TodayWeatherFragment",
                  "source_file": "app/src/main/java/com/wemaka/weatherapp/ui/fragment/TodayWeatherFragment.java"},
        screenshot_rel=None,
    )
    assert "<!doctype html>" in out.lower()
    assert "weather_main" in out
    assert "Berlin" in out
    assert "TodayWeatherFragment" in out
    print(f"  HTML rendered: {len(out)} bytes")


@step("resolve source file from class name")
def t_resolve_class():
    f = resolve_source_file_from_class("TodayWeatherFragment")
    assert f is not None, "TodayWeatherFragment should be findable in source"
    assert "TodayWeatherFragment" in f
    print(f"  TodayWeatherFragment → {f}")
    # Unknown class returns None
    assert resolve_source_file_from_class("ThisDoesNotExistAnywhere") is None


@step("feature_map attaches features from real feature.json")
def t_feature_map():
    pages = [
        {"id": "weather_main", "class_name": "TodayWeatherFragment",
         "source_file": resolve_source_file_from_class("TodayWeatherFragment")},
        {"id": "settings/main", "class_name": "MainSettingsFragment",
         "source_file": resolve_source_file_from_class("MainSettingsFragment")},
        {"id": "settings/detail", "class_name": "SettingsFragment",
         "source_file": resolve_source_file_from_class("SettingsFragment")},
        {"id": "search_menu", "class_name": "SearchMenuFragment",
         "source_file": resolve_source_file_from_class("SearchMenuFragment")},
    ]
    pages, shell = attach_features(pages)
    for p in pages:
        print(f"  {p['id']:20s} feature={p.get('feature')}")
    print(f"  shell_features: {shell}")
    matched = {p["feature"] for p in pages if p.get("feature")}
    print(f"  matched features: {matched}")
    # Expected mapping based on the real feature.json content:
    expected = {
        "weather_main": "weather_main",
        "settings/main": "settings",
        "settings/detail": "settings",
        "search_menu": "search_menu",
    }
    by_id = {p["id"]: p.get("feature") for p in pages}
    missing = {pid: ex for pid, ex in expected.items() if by_id.get(pid) != ex}
    assert not missing, f"unexpected feature mapping: {by_id} (wanted {expected})"
    # app_shell has no UI surface in the input, so it should appear in shell_features
    assert "app_shell" in shell, f"app_shell should be a shell-only feature, shell={shell}"


def main() -> int:
    print(f"SOURCE_ROOT: {SOURCE_ROOT}")
    print(f"FIXTURE:     {FIXTURE}")
    t_discover()
    t_normalize()
    t_render()
    t_resolve_class()
    t_feature_map()
    print("\nAll dry-run checks completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
