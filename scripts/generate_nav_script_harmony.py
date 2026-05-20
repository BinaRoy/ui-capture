"""Generate a runnable nav_script_harmony.sh from a nav_plan.json.

Strategy:
  - Read nav_plan.json (output of nav_graph_harmony.py).
  - Emit `aa start` + capture launcher (entryview) first.
  - For every page with status=ready AND parent=entryview, emit:
        click(tap.x, tap.y) → wait → capture_page <slug> → keyEvent 2 (back) → wait
  - For pages awaiting their parent, emit a `# TODO` line (no command) so the
    operator / next iteration can see what's left.

The orchestrator (run.py) interprets `capture_page <slug>` lines; everything
else is run as shell.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_BANNER = (
    "#!/usr/bin/env bash\n"
    "# Auto-generated from nav_plan.json by generate_nav_script_harmony.py.\n"
    "# Re-running this generator after each capture round may unlock more pages.\n"
)

_SETTLE_AFTER_TAP = 2     # seconds — wait for page transition + animation
_SETTLE_AFTER_BACK = 2


def render(plan_path: Path, out_path: Path,
           bundle: str = "com.example.ios2cj",
           ability: str = "EntryAbility") -> dict:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))["plan"]

    lines: list[str] = [_BANNER.rstrip()]
    lines += [
        "",
        "set -euo pipefail",
        'HDC="${HDC:-/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/toolchains/hdc}"',
        f'BUNDLE="{bundle}"',
        f'ABILITY="{ability}"',
        "",
        "# Cold-start: force-stop first so the app re-enters from launcher,",
        "# not from whatever NavDestination was on top last time. Without this,",
        "# subsequent runs replay residual state and tap coords drift.",
        '"$HDC" shell aa force-stop "$BUNDLE" 2>/dev/null || true',
        "sleep 1",
        '"$HDC" shell aa start -a "$ABILITY" -b "$BUNDLE"',
        "sleep 4",
        "capture_page entryview",
        "",
    ]

    counts = {"ready": 0, "todo": 0}
    for page_id, entry in sorted(plan.items()):
        if page_id == "entryview":
            continue
        status = entry.get("status")
        parent = entry.get("parent")
        if status == "ready" and parent == "entryview":
            tap = entry["tap"]
            lines += [
                f"# {entry['hint_class']} — label={entry['trigger_label']!r}",
                f'"$HDC" shell uitest uiInput click {tap["x"]} {tap["y"]}',
                f"sleep {_SETTLE_AFTER_TAP}",
                f"capture_page {page_id}",
                f'"$HDC" shell uitest uiInput keyEvent 2',
                f"sleep {_SETTLE_AFTER_BACK}",
                "",
            ]
            counts["ready"] += 1
        else:
            note = f"status={status}"
            if parent and parent != "entryview":
                note += f", parent={parent}"
            lines += [
                f"# TODO {entry['hint_class']}: {note} "
                f"(label={entry.get('trigger_label')!r})",
            ]
            counts["todo"] += 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        out_path.chmod(0o755)
    except OSError:
        pass
    return counts


def main(argv: list[str]) -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--plan", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--bundle", default="com.example.ios2cj")
    p.add_argument("--ability", default="EntryAbility")
    args = p.parse_args(argv)

    counts = render(Path(args.plan).resolve(), Path(args.out).resolve(),
                    bundle=args.bundle, ability=args.ability)
    print(f"[gen-nav-script] {counts['ready']} taps emitted, "
          f"{counts['todo']} TODOs -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
