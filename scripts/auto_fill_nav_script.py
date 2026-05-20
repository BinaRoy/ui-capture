#!/usr/bin/env python3
"""Auto-fill `input tap X Y` lines into the nav script.

Reads `nav_resolution.json` (produced by `auto_resolve_nav.py`) and the
current screen's uiautomator dump XML, matches each resolved view_id to
its `bounds` attribute, computes the tap center, and rewrites the
matching TODO block in `nav_script_android.sh` with a real
`adb shell input tap X Y` line.

Round 1 limitation: this only fills hints whose trigger View is visible
on the **current** screen at invocation time. Multi-hop nav (e.g. tap to
open MainSettings, then tap inside to open Settings) needs a chained
runner. We emit the hops we can and leave deeper TODOs alone — that's
still strictly more useful than the 1-of-6 static-only baseline.

Generic: no app-specific knowledge. The hints already carry the view_id,
the live dump provides the bounds. Only the regex bridging the two is
here.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional, Iterable


# resource-id format: "<pkg>:id/<name>"  (also ":id/" without package on system widgets)
_RESOURCE_ID_RE = re.compile(r'resource-id="(?:[\w.]+)?:id/([\w_]+)"')
# bounds="[x1,y1][x2,y2]"
_BOUNDS_RE = re.compile(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"')


def dump_via_adb(adb: str, dest: Path, device: Optional[str] = None) -> None:
    """Run `adb shell uiautomator dump` and pull the XML to `dest`."""
    cmd = [adb]
    if device:
        cmd += ["-s", device]
    # uiautomator dump writes to /sdcard/window_dump.xml by default
    sub = subprocess.run(cmd + ["shell", "uiautomator", "dump", "/sdcard/window_dump.xml"],
                         capture_output=True, text=True, timeout=20)
    if sub.returncode != 0:
        raise RuntimeError(f"adb uiautomator dump failed: {sub.stderr.strip()}")
    pull = subprocess.run(cmd + ["pull", "/sdcard/window_dump.xml", str(dest)],
                          capture_output=True, text=True, timeout=20)
    if pull.returncode != 0:
        raise RuntimeError(f"adb pull failed: {pull.stderr.strip()}")


def find_bounds_for_id(xml_text: str, view_id: str) -> Optional[tuple[int, int]]:
    """Return (cx, cy) center of the node whose resource-id ends with `view_id`.
    None if not found.

    uiautomator XML attributes for a node are all on one tag line, but
    `resource-id` and `bounds` may appear in any order. Strategy: scan
    each <node ... /> tag, check both attrs, return on first match.
    """
    # Each node line looks like: <node ... resource-id="..." ... bounds="..." ... />
    # Split crudely on `<node` boundaries.
    pos = 0
    while True:
        i = xml_text.find("<node", pos)
        if i < 0:
            return None
        j = xml_text.find(">", i)
        if j < 0:
            return None
        tag = xml_text[i:j + 1]
        pos = j + 1
        rid_match = _RESOURCE_ID_RE.search(tag)
        if not rid_match or rid_match.group(1) != view_id:
            continue
        b_match = _BOUNDS_RE.search(tag)
        if not b_match:
            continue
        x1, y1, x2, y2 = (int(v) for v in b_match.groups())
        return ((x1 + x2) // 2, (y1 + y2) // 2)


_TODO_BLOCK_RE = re.compile(
    r'# --- (?P<cls>\w+) \([^)]+\) ---\n'
    r'# "\$ADB" shell input tap   X   Y[^\n]*\n'
    r'# sleep 1\n'
    r'# capture_page (?P<slug>[\w_]+)\n',
    re.MULTILINE,
)


def fill_nav_script(
    nav_script: Path,
    resolutions: dict,
    bounds_for_id,
    *,
    dry_run: bool = False,
) -> dict:
    text = nav_script.read_text(encoding="utf-8")
    replaced = []
    skipped = []

    def _replace(m: re.Match) -> str:
        cls = m.group("cls")
        slug = m.group("slug")
        # Look up resolution
        res = resolutions.get(slug)
        if not res or not res.get("view_id"):
            skipped.append({"slug": slug, "reason": "no_view_id"})
            return m.group(0)
        coords = bounds_for_id(res["view_id"])
        if not coords:
            skipped.append({"slug": slug, "reason": f"view_id_not_on_screen:{res['view_id']}"})
            return m.group(0)
        x, y = coords
        new = (
            f"# --- {cls} (auto-resolved: trigger={res.get('via')} view_id={res['view_id']}) ---\n"
            f'"$ADB" shell input tap {x} {y}\n'
            f"sleep 1\n"
            f"capture_page {slug}\n"
            f'"$ADB" shell input keyevent KEYCODE_BACK\n'
            f"sleep 1\n"
        )
        replaced.append({"slug": slug, "view_id": res["view_id"], "x": x, "y": y})
        return new

    new_text = _TODO_BLOCK_RE.sub(_replace, text)
    if not dry_run and new_text != text:
        # Preserve a `.bak` so the operator can see what changed.
        nav_script.with_suffix(nav_script.suffix + ".bak").write_text(text, encoding="utf-8")
        nav_script.write_text(new_text, encoding="utf-8")
    return {"replaced": replaced, "skipped": skipped, "modified": new_text != text}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--nav-script", required=True)
    parser.add_argument("--resolution", required=True,
                        help="Path to nav_resolution.json (from auto_resolve_nav.py)")
    parser.add_argument("--dump-xml", help="Path to a pre-pulled uiautomator dump XML")
    parser.add_argument("--adb", default="adb",
                        help="adb path (used if --dump-xml not given)")
    parser.add_argument("--device", help="adb -s <serial>")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    nav_script = Path(args.nav_script).resolve()
    resolution = Path(args.resolution).resolve()
    if not nav_script.exists():
        print(f"nav script not found: {nav_script}", file=sys.stderr)
        return 2
    if not resolution.exists():
        print(f"resolution not found: {resolution}", file=sys.stderr)
        return 2

    res_data = json.loads(resolution.read_text(encoding="utf-8"))
    resolutions = res_data.get("resolutions", {})

    if args.dump_xml:
        xml_text = Path(args.dump_xml).read_text(encoding="utf-8", errors="ignore")
    else:
        tmp = nav_script.parent / "_window_dump.xml"
        try:
            dump_via_adb(args.adb, tmp, args.device)
        except Exception as exc:
            print(f"[auto-fill-nav-script] could not dump via adb: {exc}", file=sys.stderr)
            return 3
        xml_text = tmp.read_text(encoding="utf-8", errors="ignore")

    def bounds_for_id(view_id: str):
        return find_bounds_for_id(xml_text, view_id)

    report = fill_nav_script(nav_script, resolutions, bounds_for_id, dry_run=args.dry_run)
    print(f"[auto-fill-nav-script] replaced={len(report['replaced'])} "
          f"skipped={len(report['skipped'])} modified={report['modified']}")
    for r in report["replaced"]:
        print(f"  + {r['slug']} via {r['view_id']} at ({r['x']},{r['y']})")
    for s in report["skipped"]:
        print(f"  - {s['slug']}: {s['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
