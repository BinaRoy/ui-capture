#!/usr/bin/env python3
"""
Standalone normalizer: convert a raw platform dump to UI-IR.

Useful for testing the parser on fixtures without running an emulator. The adapter
class has the same logic; this CLI is a thin wrapper.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _paths import ADAPTER_NAME  # noqa: E402
from adapters import get_adapter  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", required=True, help="path to raw platform dump (xml/plist)")
    parser.add_argument("--adapter", default=ADAPTER_NAME)
    parser.add_argument("--out", help="output path (default: stdout)")
    args = parser.parse_args()

    adapter = get_adapter(args.adapter)
    ir = adapter.normalize(Path(args.raw))
    payload = json.dumps(ir, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
        print(f"[normalize] → {args.out}")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
