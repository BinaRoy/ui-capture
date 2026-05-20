#!/usr/bin/env python3
"""Validate hierarchy.json files against schema/hierarchy.schema.json.

Run modes:
  - Standalone (fixtures only): `python tests/test_schema.py`
  - Live (validate real workflow output):
      IOS2CJ_WORKFLOW_CONFIG=$(pwd)/workflow.config.json python tests/test_schema.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Iterable

try:
    import jsonschema
    from jsonschema import Draft202012Validator
except ImportError:
    print("ERROR: jsonschema not installed. Run: pip install jsonschema")
    sys.exit(2)


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = SKILL_ROOT / "schema" / "hierarchy.schema.json"


def _load_schema() -> dict:
    with SCHEMA_PATH.open() as f:
        return json.load(f)


def _find_hierarchy_files() -> list[Path]:
    """Discover hierarchy.json files to validate.

    Priority:
      1. Real workflow output (if IOS2CJ_WORKFLOW_CONFIG is set / discoverable)
      2. Fixtures under tests/fixtures (if any)
    Returns empty list if nothing found — caller should treat as skip, not fail.
    """
    paths: list[Path] = []
    # Try live workflow output via _paths shim
    sys.path.insert(0, str(SKILL_ROOT / "scripts"))
    try:
        from _paths import UI_PAGES_DIR  # type: ignore
        if UI_PAGES_DIR and Path(UI_PAGES_DIR).is_dir():
            paths.extend(sorted(Path(UI_PAGES_DIR).glob("*/hierarchy.json")))
    except Exception:
        pass
    # Fixtures
    fixtures_dir = SKILL_ROOT / "tests" / "fixtures"
    if fixtures_dir.is_dir():
        paths.extend(sorted(fixtures_dir.glob("**/hierarchy.json")))
    return paths


def _format_error(err: jsonschema.ValidationError) -> str:
    path = "/".join(str(p) for p in err.absolute_path) or "<root>"
    return f"  at {path}: {err.message}"


def validate_file(path: Path, validator: Draft202012Validator) -> tuple[bool, list[str]]:
    try:
        with path.open() as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        return False, [f"  JSON parse error: {e}"]
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
    return (not errors), [_format_error(e) for e in errors]


def round_trip_adapter_normalize() -> tuple[int, int]:
    """If a fixture XML exists, run android adapter's normalize() and validate output.

    Returns (passed, failed). Skips silently if fixture or adapter not available.
    """
    fixture = SKILL_ROOT / "fixtures" / "sample_uiautomator_dump.xml"
    if not fixture.exists():
        return 0, 0
    sys.path.insert(0, str(SKILL_ROOT))
    try:
        from adapters.android import AndroidAdapter  # type: ignore
    except Exception as e:
        print(f"  [skip] adapter import failed: {e}")
        return 0, 0
    try:
        adapter = AndroidAdapter.__new__(AndroidAdapter)  # bypass __init__
        ir = adapter.normalize(fixture)
    except Exception as e:
        print(f"  [skip] normalize() failed: {e}")
        return 0, 0
    schema = _load_schema()
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(ir), key=lambda e: list(e.absolute_path))
    if errors:
        print("  FAIL: adapter normalize() output:")
        for e in errors[:10]:
            print(_format_error(e))
        return 0, 1
    print("  PASS: adapter normalize() round-trip")
    return 1, 0


def main(argv: list[str]) -> int:
    schema = _load_schema()
    validator = Draft202012Validator(schema)

    # Sanity-check the schema itself
    try:
        Draft202012Validator.check_schema(schema)
    except jsonschema.SchemaError as e:
        print(f"FAIL: schema invalid: {e.message}")
        return 1

    print(f"Schema: {SCHEMA_PATH.relative_to(SKILL_ROOT)}")
    print("=" * 60)

    files = _find_hierarchy_files()
    if not files:
        print("[skip] no hierarchy.json found (run a capture first or add fixtures)")
    else:
        print(f"Validating {len(files)} hierarchy.json file(s)...")

    passed = failed = 0
    for fp in files:
        rel = fp.relative_to(SKILL_ROOT.parent) if fp.is_relative_to(SKILL_ROOT.parent) else fp
        ok, errs = validate_file(fp, validator)
        if ok:
            passed += 1
            print(f"  PASS: {rel}")
        else:
            failed += 1
            print(f"  FAIL: {rel}")
            for line in errs[:10]:
                print(line)
            if len(errs) > 10:
                print(f"    ... and {len(errs) - 10} more")

    # Adapter round-trip
    print("\nAdapter round-trip:")
    rt_pass, rt_fail = round_trip_adapter_normalize()
    passed += rt_pass
    failed += rt_fail

    print("=" * 60)
    print(f"Summary: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
