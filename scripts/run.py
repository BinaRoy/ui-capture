#!/usr/bin/env python3
"""
UI Capture orchestrator.

Pipelines the four phases:
  1. discover_hints   (static, best-effort)
  2. scaffold         (nav script — only if missing)
  3. capture          (runtime, drives nav script, dumps per page)
  4. finalize         (feature mapping → ui_manifest.json + report.md)

The nav script is a project-local bash file that the operator (or generator) writes.
The orchestrator invokes it as a child process with a CAPTURE_FIFO environment variable
pointing at a named pipe the script writes capture requests to. Each request is a single
line: `<page_id>`. The orchestrator reads requests off the pipe, performs the dump for
each, then continues. This decouples the navigation (project-specific) from the dumping
(adapter-specific) cleanly.

If `CAPTURE_FIFO` is not feasible (Windows-style nav script), we also accept a simpler
batch mode: the nav script can drop a sentinel file `pages.txt` listing IDs, in which
case the orchestrator just iterates them after the script exits.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

# Make sibling adapter package importable when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _paths import (  # noqa: E402
    ADAPTER_NAME,
    CONFIG_PATH,
    FEATURE_JSON,
    SOURCE_ROOT,
    UI_MANIFEST,
    UI_NAV_DIR,
    UI_PAGES_DIR,
    UI_REPORT,
    UI_ROOT,
    ensure_ui_dirs,
)
import _paths as _p_module  # for read-at-call-time path resolution after --ui-root override
from adapters import AdapterError, PageHint, get_adapter  # noqa: E402
from feature_map import (  # noqa: E402
    attach_features,
    resolve_source_file_from_class,
)


SCHEMA_VERSION = 1


def _override_ui_root(new_root: Path) -> None:
    """Redirect ui output to a different directory (e.g. a tempdir from tests).

    Module-level _paths constants are reassigned. Callers that imported them by name
    won't pick up the change, so we mutate the _paths module attributes too — the
    helpers and other scripts that import individual names from _paths use those at
    call time so changes propagate.
    """
    import _paths as _p
    global UI_ROOT, UI_PAGES_DIR, UI_MANIFEST, UI_REPORT, UI_NAV_DIR
    new_root = new_root.resolve()
    UI_ROOT = new_root
    UI_PAGES_DIR = new_root / "pages"
    UI_MANIFEST = new_root / "ui_manifest.json"
    UI_REPORT = new_root / "report.md"
    UI_NAV_DIR = new_root / "nav_script"
    _p.UI_ROOT = UI_ROOT
    _p.UI_PAGES_DIR = UI_PAGES_DIR
    _p.UI_MANIFEST = UI_MANIFEST
    _p.UI_REPORT = UI_REPORT
    _p.UI_NAV_DIR = UI_NAV_DIR
    new_root.mkdir(parents=True, exist_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("pipeline", "standalone"), default="standalone")
    parser.add_argument("--pages", help="comma-separated subset of page IDs (standalone only)")
    parser.add_argument("--skip-discover", action="store_true")
    parser.add_argument("--skip-scaffold", action="store_true")
    parser.add_argument("--skip-auto-resolve", action="store_true",
                        help="Skip static auto-resolve of nav targets (Phase 2b).")
    parser.add_argument("--skip-capture", action="store_true",
                        help="re-render HTML and rebuild manifest from existing dumps")
    parser.add_argument("--nav-script", help="explicit nav script path (overrides default)")
    parser.add_argument("--nav-timeout", type=int, default=600)
    parser.add_argument("--ui-root", help="override output directory (defaults to <workflow.root>/ui/). "
                                          "Useful for tests so they don't touch production artifacts.")
    parser.add_argument("--new-manual", metavar="PAGE_ID",
                        help="Scaffold a manual-capture page folder under pages/<id>/ with a "
                             "manual.json template + README, then exit. Drop your screenshot in "
                             "and re-run the skill normally to integrate.")
    parser.add_argument("--source-file", help="(with --new-manual) source file path to record")
    parser.add_argument("--feature", help="(with --new-manual) feature_id override")
    args = parser.parse_args()

    # Allow --ui-root override BEFORE ensure_ui_dirs / any output paths get used.
    if args.ui_root:
        _override_ui_root(Path(args.ui_root))

    ensure_ui_dirs()

    # --new-manual short-circuits — it just writes scaffold files and exits.
    if args.new_manual:
        return _scaffold_manual_page(args.new_manual, args.source_file, args.feature)
    summary: dict = {
        "schema_version": SCHEMA_VERSION,
        "adapter": ADAPTER_NAME,
        "mode": args.mode,
        "config": str(CONFIG_PATH),
        "source_root": str(SOURCE_ROOT),
        "feature_json": str(FEATURE_JSON),
        "platform": None,
        "status": "ok",
        "phases": {},
        "pages": [],
        "shell_features": [],
    }

    try:
        adapter = get_adapter(ADAPTER_NAME)
    except AdapterError as exc:
        return _emit_stub(summary, status="adapter_unsupported", reason=str(exc), exit_code=2)
    summary["platform"] = adapter.platform

    # -------------------------- Phase 1: discover hints
    hints: list[PageHint] = []
    if not args.skip_discover:
        try:
            hints = adapter.discover_hints(SOURCE_ROOT)
        except Exception as exc:  # discovery must never abort the run
            summary["phases"]["discover_hints"] = {"status": "error", "error": str(exc)}
        else:
            summary["phases"]["discover_hints"] = {"status": "ok", "count": len(hints)}
            _log(f"discover_hints: {len(hints)} candidate(s)")
            if len(hints) == 0:
                # Compose / DataBinding / non-conventional layouts fall through
                # all three discovery passes (navgraph, manifest, Fragment subclass).
                # Tell the user explicitly so they don't stare at an empty scaffold.
                _log("[warn] discover_hints found 0 candidates. Likely causes:")
                _log("       - source is pure Compose (no Fragments, no nav XML)")
                _log("       - source uses DataBinding without conventional patterns")
                _log("       - source.root in workflow.config.json points to wrong dir")
                _log("       → You will need to author the nav script manually.")
                _log(f"       → Edit: {_default_nav_script(adapter.platform)}")
                summary["phases"]["discover_hints"]["warning"] = "no_hints_found"

    # -------------------------- Phase 2: scaffold nav script
    nav_script_path = Path(args.nav_script) if args.nav_script else _default_nav_script(adapter.platform)
    if not args.skip_scaffold:
        try:
            adapter.render_nav_script(hints, nav_script_path)
            summary["phases"]["scaffold"] = {
                "status": "ok" if nav_script_path.exists() else "missing",
                "path": str(nav_script_path),
            }
        except Exception as exc:
            summary["phases"]["scaffold"] = {"status": "error", "error": str(exc)}

    # -------------------------- Phase 2b: auto-resolve nav targets
    # Static analyzer that finds the View id (R.id.foo) which opens each
    # un-captured hint. Output feeds Phase 2c (live coord fill) when adb
    # is available. Independent of capture infra — runs even if no
    # emulator. Always best-effort and never blocking.
    if hints and not args.skip_auto_resolve:
        try:
            from auto_resolve_nav import resolve as _auto_resolve  # type: ignore
            resolutions = _auto_resolve(hints, SOURCE_ROOT)
            resolution_path = nav_script_path.parent / "nav_resolution.json"
            resolution_path.parent.mkdir(parents=True, exist_ok=True)
            resolution_path.write_text(
                json.dumps({
                    "schema_version": 1,
                    "source_root": str(SOURCE_ROOT),
                    "resolutions": {k: asdict(v) for k, v in resolutions.items()},
                    "stats": {
                        "total_hints": len(resolutions),
                        "resolved": sum(1 for r in resolutions.values() if r.resolved),
                    },
                }, indent=2),
                encoding="utf-8",
            )
            summary["phases"]["auto_resolve"] = {
                "status": "ok",
                "path": str(resolution_path),
                "total_hints": len(resolutions),
                "resolved": sum(1 for r in resolutions.values() if r.resolved),
            }
            _log(f"auto_resolve: {summary['phases']['auto_resolve']['resolved']}/"
                 f"{summary['phases']['auto_resolve']['total_hints']} hints resolved")
        except Exception as exc:
            summary["phases"]["auto_resolve"] = {"status": "error", "error": str(exc)}

    # ------------------------- Phase 2.5: discover manual pages
    # User-provided screenshots / manual.json files take precedence over auto capture
    # for the same page_id. We collect the ids here so the capture loop can short-
    # circuit them, and we process them in a dedicated phase below.
    manual_specs = _discover_manual_pages()
    manual_ids = {spec["id"] for spec in manual_specs}
    summary["phases"]["manual_discovery"] = {
        "status": "ok",
        "count": len(manual_specs),
        "ids": sorted(manual_ids),
    }
    if manual_specs:
        _log(f"manual capture: {len(manual_specs)} page(s) — {', '.join(sorted(manual_ids))}")

    # -------------------------- Phase 3: capture
    captured_pages: list[dict] = []
    if args.skip_capture:
        captured_pages = _rebuild_from_existing()
        summary["phases"]["capture"] = {"status": "skipped", "rebuilt_from_existing": len(captured_pages)}
    else:
        ok, msg = adapter.check_infrastructure()
        if not ok:
            summary["phases"]["capture"] = {"status": "infrastructure_missing", "reason": msg}
            return _emit_stub(summary, status="infrastructure_missing", reason=msg,
                              exit_code=0 if args.mode == "pipeline" else 1)
        if not nav_script_path.exists():
            summary["phases"]["capture"] = {"status": "scaffold_pending", "nav_script": str(nav_script_path)}
            return _emit_stub(summary, status="scaffold_pending",
                              reason=f"nav script not present at {nav_script_path}; edit it then re-run",
                              exit_code=0 if args.mode == "pipeline" else 1)

        # Pre-run: detect unedited scaffold sentinels (`: "${PACKAGE:?...}"`).
        # If any sentinel var is unset, the nav script will exit non-zero on its
        # very first line — report scaffold_pending instead of pretending we ran.
        unset_sentinels = _scan_nav_script_sentinels(nav_script_path)
        if unset_sentinels:
            reason = (
                f"nav script at {nav_script_path} still contains unset scaffold "
                f"sentinel env vars: {', '.join(unset_sentinels)}. "
                f"Edit the nav script to remove the `: \"${{VAR:?...}}\"` guards "
                f"and supply concrete adb commands, or export the required vars."
            )
            summary["phases"]["capture"] = {
                "status": "scaffold_pending",
                "nav_script": str(nav_script_path),
                "unset_sentinels": unset_sentinels,
                "reason": reason,
            }
            return _emit_stub(summary, status="scaffold_pending", reason=reason,
                              exit_code=0 if args.mode == "pipeline" else 1)

        page_filter = set((args.pages or "").split(",")) - {""} or None
        captured_pages, capture_diag = _run_capture_loop(
            adapter, nav_script_path, args.nav_timeout, page_filter, manual_ids
        )
        captured_n = sum(1 for p in captured_pages if p.get("status") == "captured")
        failed_n = sum(1 for p in captured_pages if p.get("status") == "failed")
        rc = capture_diag.get("nav_returncode")
        timed_out = bool(capture_diag.get("timed_out"))

        # Categorize the capture phase outcome.
        if timed_out:
            phase_status = "timed_out"
        elif rc not in (None, 0) and captured_n == 0:
            phase_status = "nav_script_error"
        elif rc not in (None, 0) and captured_n > 0:
            phase_status = "partial_nav_error"
        elif captured_n == 0 and len(captured_pages) == 0:
            # No pages emitted at all — script ran cleanly but emitted no capture_page.
            phase_status = "no_pages_requested"
        else:
            phase_status = "ok"

        summary["phases"]["capture"] = {
            "status": phase_status,
            "count": len(captured_pages),
            "captured": captured_n,
            "failed": failed_n,
            "nav_returncode": rc,
        }
        if phase_status != "ok":
            summary["phases"]["capture"]["nav_output_tail"] = capture_diag.get("nav_output_tail", "")
        # If catastrophic at this point (no pages and nav crashed), emit stub now.
        if phase_status in ("nav_script_error", "no_pages_requested", "timed_out") and captured_n == 0:
            reason = (
                f"capture phase: {phase_status} "
                f"(nav_returncode={rc}, captured=0). See {UI_ROOT / 'nav_script.log'} for nav output."
            )
            return _emit_stub(summary, status=phase_status, reason=reason,
                              exit_code=0 if args.mode == "pipeline" else 1)

    # ------------------------- Phase 3.5: process manual pages
    # Manuals are processed AFTER the auto loop so the orchestrator has a clean
    # stable state. Each manual produces a page entry that joins the captured list.
    for spec in manual_specs:
        captured_pages.append(_process_manual_page(spec))
    if manual_specs:
        _log(f"manual capture: processed {len(manual_specs)} page(s)")

    # -------------------------- Phase 4: finalize
    # Resolve source files for any pages that don't yet have one (from runtime identity).
    for page in captured_pages:
        if not page.get("source_file"):
            cls = page.get("class_name") or (page.get("identity") or {}).get("page_class")
            if cls:
                page["source_file"] = resolve_source_file_from_class(cls)

    # Phase 4.5: per-page resource extraction (Android: layouts/drawables/strings).
    # Adapter no-ops on iOS until the adapter implements it. Errors are non-fatal —
    # missing resources do not block the manifest emission.
    res_count_total = 0
    for page in captured_pages:
        if page.get("status") != "captured":
            continue
        page_id = page.get("id", "")
        out_dir = _p_module.UI_PAGES_DIR / _safe(page_id)
        try:
            rmanifest = adapter.extract_resources(
                page.get("source_file"), SOURCE_ROOT, out_dir,
            )
        except Exception as exc:
            page["resources_error"] = f"{type(exc).__name__}: {exc}"
            continue
        if rmanifest is None:
            continue
        stats = rmanifest.get("stats", {})
        page["resources"] = {
            "dir": (out_dir / "resources").relative_to(_p_module.UI_ROOT).as_posix(),
            "policy": rmanifest.get("policy"),
            "stats": stats,
        }
        res_count_total += sum(int(v or 0) for v in stats.values())
    if res_count_total:
        summary["phases"]["resources"] = {"status": "ok", "items_total": res_count_total}

    captured_pages, shell_features = attach_features(captured_pages)
    summary["pages"] = captured_pages
    summary["shell_features"] = shell_features

    # Finalize top-level status BEFORE writing the manifest so disk reflects truth.
    captured_count = sum(1 for p in captured_pages if p.get("status") == "captured")
    failed_count = sum(1 for p in captured_pages if p.get("status") == "failed")
    if not args.skip_capture and captured_count == 0:
        summary["status"] = "all_pages_failed"
    elif failed_count > 0 and captured_count > 0:
        summary["status"] = "partial"
    else:
        summary["status"] = "ok"

    _write_manifest(summary)
    _write_report(summary)

    # Auto-generate precise diff (diff.json + diff.md) for every captured page
    # that has a corresponding target hierarchy. compare() is a no-op when the
    # target side is absent — it just writes the HTML panel with one side empty.
    _run_diff_phase(captured_pages, summary)

    # Pipeline mode: exit 0 unless catastrophic
    if summary["status"] == "all_pages_failed":
        if args.mode == "pipeline":
            return 0
        return 1
    return 0


def _run_diff_phase(captured_pages: list[dict], summary: dict) -> None:
    """For every successfully captured page, run compare + dump_diff.

    Writes compare/<page>.html + compare/<page>.diff.json + compare/<page>.diff.md.
    Skips silently when target_pages/<slug>/hierarchy.json is absent — diff.json
    and diff.md are only emitted when both sides are present.
    """
    try:
        from compare import compare as run_compare  # noqa: PLC0415
    except ImportError as exc:
        _log(f"diff phase skipped: cannot import compare ({exc})")
        return

    diff_results: list[str] = []
    for page in captured_pages:
        if page.get("status") != "captured":
            continue
        page_id = page.get("id", "")
        if not page_id:
            continue
        try:
            out_path = run_compare(page_id, dump_diff=True)
            target_root = UI_ROOT / "target_pages"
            # Report whether the precise diff files were actually written
            diff_json = out_path.parent / f"{out_path.stem}.diff.json"
            if diff_json.exists():
                diff_results.append(f"{page_id} → diff.json + diff.md")
            else:
                diff_results.append(f"{page_id} → compare HTML only (no target)")
        except Exception as exc:
            diff_results.append(f"{page_id} → ERROR: {exc}")

    if diff_results:
        summary.setdefault("phases", {})["diff"] = {"pages": diff_results}
        for r in diff_results:
            _log(f"diff: {r}")


# -----------------------------------------------------------------------------

def _default_nav_script(platform: str) -> Path:
    return UI_NAV_DIR / f"nav_script_{platform}.sh"


def _emit_stub(summary: dict, *, status: str, reason: str, exit_code: int) -> int:
    summary["status"] = status
    summary["stub_reason"] = reason
    _write_manifest(summary)
    _write_report(summary)
    _log(f"STUB MANIFEST emitted: {status} — {reason}")
    return exit_code


def _write_manifest(summary: dict) -> None:
    UI_MANIFEST.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    _log(f"manifest → {UI_MANIFEST}")


def _write_report(summary: dict) -> None:
    lines = [
        f"# UI Capture Report",
        "",
        f"- adapter: `{summary.get('adapter')}`",
        f"- mode: `{summary.get('mode')}`",
        f"- status: **{summary.get('status')}**",
    ]
    if summary.get("stub_reason"):
        lines.append(f"- stub reason: {summary['stub_reason']}")
    lines.extend(["", "## Phases", ""])
    for phase, info in (summary.get("phases") or {}).items():
        lines.append(f"- **{phase}**: `{json.dumps(info, ensure_ascii=False)}`")
    pages = summary.get("pages") or []
    if pages:
        lines.extend(["", "## Pages", "", "| id | status | feature | class | source |", "|---|---|---|---|---|"])
        for p in pages:
            lines.append(
                f"| {p.get('id','?')} | {p.get('status','?')} | "
                f"{p.get('feature') or '—'} | "
                f"`{p.get('class_name') or '—'}` | "
                f"`{p.get('source_file') or '—'}` |"
            )
    if summary.get("shell_features"):
        lines.extend(["", f"## Shell-only features (no UI surface)", ""])
        for f in summary["shell_features"]:
            lines.append(f"- `{f}`")
    UI_REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _log(msg: str) -> None:
    print(f"[ui-capture] {msg}", flush=True)


# ----------------------------------------------------------- capture loop

def _scan_nav_script_sentinels(nav_script: Path) -> list[str]:
    """Return a list of unedited scaffold sentinels found in the nav script.

    A 'sentinel' is a literal guard pattern like ``: "${PACKAGE:?...}"`` that the
    scaffold leaves behind for the operator to remove or set via env. If any
    sentinel is still present AND its env var is not set by the harness, the
    nav script will exit non-zero on its first line — meaning the operator
    hasn't edited it yet. We treat this as ``scaffold_pending``.
    """
    try:
        text = nav_script.read_text(encoding="utf-8")
    except OSError:
        return []
    found: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        # Match `: "${NAME:?msg}"` or `: ${NAME:?msg}` — bash mandatory-var pattern.
        m = re.match(r':\s+"?\$\{([A-Z_][A-Z0-9_]*):\?', stripped)
        if m:
            var = m.group(1)
            if var not in os.environ:
                found.append(var)
    return found


def _run_capture_loop(adapter, nav_script: Path, timeout: int, page_filter: Optional[set],
                      manual_ids: Optional[set] = None) -> tuple[list[dict], dict]:
    """Run the nav script with a FIFO; for each line on the FIFO, capture that page.

    The nav script gets two helpers via env: CAPTURE_FIFO (path to write to) and a
    bash function `capture_page` exported via the wrapper. We invoke through a small
    wrapper that defines `capture_page` to echo the id into the FIFO and then wait
    briefly for the orchestrator to finish, signaled by an ack file.
    """
    pages: list[dict] = []
    fifo_dir = Path(tempfile.mkdtemp(prefix="ui_capture_"))
    fifo = fifo_dir / "requests.fifo"
    ack_dir = fifo_dir / "ack"
    ack_dir.mkdir()
    os.mkfifo(str(fifo))

    wrapper = fifo_dir / "wrapper.sh"
    wrapper.write_text(_NAV_WRAPPER, encoding="utf-8")
    wrapper.chmod(0o755)

    env = os.environ.copy()
    env["CAPTURE_FIFO"] = str(fifo)
    env["CAPTURE_ACK_DIR"] = str(ack_dir)
    env["NAV_SCRIPT"] = str(nav_script)

    # Start nav script (writer side of FIFO). It will block on first capture_page until
    # the orchestrator (reader side) is up. We open the reader BEFORE launching the
    # script to avoid a deadlock.
    proc = subprocess.Popen(
        ["/bin/bash", str(wrapper)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    stop_at = time.time() + timeout

    # Drain stdout in a thread to avoid pipe deadlock
    nav_output: list[str] = []
    def _drain():
        if proc.stdout is None:
            return
        for line in proc.stdout:
            nav_output.append(line.rstrip("\n"))
    threading.Thread(target=_drain, daemon=True).start()

    try:
        with open(fifo, "r") as reader:
            while True:
                if time.time() > stop_at:
                    _log(f"nav script timed out after {timeout}s — terminating")
                    proc.terminate()
                    break
                if proc.poll() is not None:
                    # Drain any remaining lines from FIFO non-blockingly: just break
                    break
                line = reader.readline()
                if not line:
                    # Writer closed without sending anything — wait for proc to exit
                    if proc.poll() is None:
                        time.sleep(0.1)
                        continue
                    break
                page_id = line.strip()
                if not page_id:
                    continue
                if page_filter and page_id not in page_filter:
                    _log(f"skip {page_id} (not in --pages filter)")
                    (ack_dir / f"{_safe(page_id)}.done").write_text("skipped")
                    continue
                if manual_ids and page_id in manual_ids:
                    # User has supplied a manual screenshot for this page. Skip the
                    # adb-driven dump (would just clobber user artifacts) and ack so
                    # the nav script continues. Manual processing happens later.
                    _log(f"skip {page_id} (manual capture present — processed in Phase 3.5)")
                    (ack_dir / f"{_safe(page_id)}.done").write_text("manual")
                    continue
                _log(f"capturing {page_id}…")
                try:
                    pages.append(_capture_one(adapter, page_id))
                except Exception as exc:
                    _log(f"capture {page_id} raised {type(exc).__name__}: {exc}")
                    pages.append({"id": page_id, "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
                (ack_dir / f"{_safe(page_id)}.done").write_text("ok")
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(fifo_dir, ignore_errors=True)

    if nav_output:
        log_path = UI_ROOT / "nav_script.log"
        log_path.write_text("\n".join(nav_output) + "\n", encoding="utf-8")
        _log(f"nav script output saved to {log_path}")

    diag: dict = {
        "nav_returncode": proc.returncode if proc.poll() is not None else None,
        "nav_output_tail": "\n".join(nav_output[-20:]) if nav_output else "",
        "timed_out": time.time() > stop_at,
    }
    return pages, diag


_NAV_WRAPPER = """#!/usr/bin/env bash
# Wrapper that exports capture_page() and runs the project's nav script.
#
# CAPTURE_FIFO must stay open across multiple capture_page calls — otherwise the
# Python reader sees EOF after the first capture. We open it once on FD 3 and keep
# it open for the lifetime of this wrapper.
set -uo pipefail

if [[ -z "${CAPTURE_FIFO:-}" || -z "${CAPTURE_ACK_DIR:-}" || -z "${NAV_SCRIPT:-}" ]]; then
  echo "[wrapper] missing required env (CAPTURE_FIFO, CAPTURE_ACK_DIR, NAV_SCRIPT)" >&2
  exit 64
fi

# Open the FIFO for writing on FD 3; this stays open for the whole nav run.
exec 3>"$CAPTURE_FIFO"

# Sanitize a page id for use as an ack filename.
# IMPORTANT: use printf, not echo. `echo` appends a trailing newline that the second
# tr converts to '_', producing 'weather_main_' instead of 'weather_main' and breaking
# the ack handshake with the Python orchestrator (which uses the same chars-only rule).
_ui_capture_safe() {
  printf '%s' "$1" | tr '/#' '__' | tr -c 'A-Za-z0-9_.-' '_'
}

# capture_page <page_id> — request a capture from the orchestrator and wait for ack.
capture_page() {
  local page_id="$1"
  local safe
  local ack
  local timeout=120
  local elapsed=0
  safe=$(_ui_capture_safe "$page_id")
  ack="${CAPTURE_ACK_DIR}/${safe}.done"
  echo "[wrapper] requesting capture: page_id='$page_id' ack='$ack'" >&2

  printf '%s\\n' "$page_id" >&3
  while [[ ! -e "$ack" ]]; do
    sleep 0.2
    elapsed=$((elapsed + 1))
    if (( elapsed > timeout * 5 )); then
      echo "[wrapper] capture ack timeout for $page_id (waited ${elapsed} ticks, ack_dir listing follows)" >&2
      ls -la "$CAPTURE_ACK_DIR" >&2 || true
      return 1
    fi
  done
  echo "[wrapper] ack received for $page_id after ${elapsed} ticks" >&2
}
export -f capture_page _ui_capture_safe

# Run the project nav script. It inherits FD 3 so its capture_page calls reuse the
# same open writer end of the FIFO. We must capture its exit code so we can
# propagate it — the wrapper without `set -e` would otherwise mask nav crashes.
bash "$NAV_SCRIPT"
NAV_RC=$?

# Close the FIFO writer; the reader will see EOF and exit its loop cleanly.
exec 3>&-

exit "$NAV_RC"
"""


def _safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in "_-." else "_" for c in s)


def _capture_one(adapter, page_id: str) -> dict:
    out_dir = UI_PAGES_DIR / _safe(page_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    result = adapter.capture(page_id, out_dir)
    page: dict = {
        "id": page_id,
        "status": result.status,
        "duration_ms": result.duration_ms,
    }
    if result.identity:
        page["identity"] = {
            "top_component": result.identity.top_component,
            "current_fragment": result.identity.current_fragment,
            "page_class": result.identity.page_class(),
            "extra": result.identity.extra,
        }
        page["class_name"] = result.identity.page_class()
    if result.hierarchy is not None:
        (out_dir / "hierarchy.json").write_text(
            json.dumps(result.hierarchy, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        # Pick the visual reference for HTML: prefer the stitched full page (with
        # fold-line annotations) when the page scrolled, else the viewport.
        if result.fullpage_screenshot_path and result.fullpage_screenshot_path.exists():
            shot_rel = "screenshot_fullpage.png"
        elif result.screenshot_path:
            shot_rel = "screenshot.png"
        else:
            shot_rel = None
        try:
            from render_html import render
            html_path = out_dir / "view.html"
            html_path.write_text(
                render(
                    result.hierarchy,
                    page_id=page_id,
                    identity=(page.get("identity") if result.identity else None),
                    screenshot_rel=shot_rel,
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            page["html_error"] = str(exc)
        # A.5 side-products: git-diff friendly text tree + leaf SVG overlay.
        # Failures here must not block the capture — they're auxiliary artefacts.
        try:
            from hierarchy_md import render as render_md
            (out_dir / "hierarchy.md").write_text(render_md(result.hierarchy), encoding="utf-8")
        except Exception as exc:
            page["hierarchy_md_error"] = str(exc)
        try:
            from svg_overlay import render as render_svg
            (out_dir / "overlay.svg").write_text(render_svg(result.hierarchy), encoding="utf-8")
        except Exception as exc:
            page["overlay_svg_error"] = str(exc)
    if result.screenshot_path:
        page["screenshot"] = result.screenshot_path.relative_to(UI_ROOT).as_posix()
    if result.fullpage_screenshot_path:
        page["screenshot_fullpage"] = result.fullpage_screenshot_path.relative_to(UI_ROOT).as_posix()
    # Surface scroll metadata from the unified hierarchy so the manifest is
    # self-describing and arch-gen / Workers don't need to open hierarchy.json.
    if result.hierarchy and result.hierarchy.get("scrolled"):
        page["scroll"] = {
            "scrolled": True,
            "viewport_height": result.hierarchy.get("viewport_height"),
            "page_total_height": result.hierarchy.get("page_total_height"),
            "scroll_steps": result.hierarchy.get("scroll_steps"),
        }
    if result.raw_path:
        page["raw"] = result.raw_path.relative_to(UI_ROOT).as_posix()
    if result.error:
        page["error"] = result.error
    (out_dir / "meta.json").write_text(json.dumps(page, indent=2, ensure_ascii=False), encoding="utf-8")
    return page


# =============================================================== manual mode

def _discover_manual_pages() -> list[dict]:
    """Scan UI_PAGES_DIR for user-provided screenshots / manual.json declarations.

    A page directory is considered MANUAL if either:
      - it contains a manual.json file (explicit declaration), or
      - it contains a screenshot.png (or screenshot_fullpage.png) but no meta.json
        from a prior auto-capture run.

    The folder name is the page_id. Source-of-truth fields (source_file, feature,
    scroll, notes) come from manual.json if present. Returns a list of spec dicts
    with the fields needed by _process_manual_page().
    """
    specs: list[dict] = []
    base = _p_module.UI_PAGES_DIR
    if not base.exists():
        return specs

    for page_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        manual_json = page_dir / "manual.json"
        screenshot = page_dir / "screenshot.png"
        fullpage = page_dir / "screenshot_fullpage.png"
        meta_json = page_dir / "meta.json"

        has_manual = manual_json.exists()
        has_screenshot = screenshot.exists() or fullpage.exists()

        # Heuristic for "auto-captured page": meta.json exists AND the meta was
        # produced by an auto run (no manual marker). We detect by reading the meta
        # if present. If meta says capture_method=manual that's still a manual.
        if not has_manual:
            if meta_json.exists():
                try:
                    existing_meta = json.loads(meta_json.read_text(encoding="utf-8"))
                    if existing_meta.get("capture_method") != "manual":
                        # This is an auto-captured page; skip
                        continue
                except (OSError, json.JSONDecodeError):
                    pass
            if not has_screenshot:
                # Empty folder — nothing to do
                continue
            # No manual.json but a bare screenshot → manual_unbound mode
            spec = {
                "id": page_dir.name,
                "page_dir": page_dir,
                "manual_decl": None,
                "screenshot_present": True,
                "binding_status": "unbound",
            }
            specs.append(spec)
            continue

        # Has manual.json → parse it
        try:
            decl = json.loads(manual_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            _log(f"manual: invalid JSON at {manual_json} → {exc}; treating as unbound")
            decl = {}
        specs.append({
            "id": page_dir.name,
            "page_dir": page_dir,
            "manual_decl": decl,
            "screenshot_present": has_screenshot,
            "binding_status": "bound",
        })

    return specs


def _process_manual_page(spec: dict) -> dict:
    """Build a manifest page entry from a manual capture spec.

    Tolerant mode: invalid source_file / missing screenshot does not block; the
    entry gets a status that downstream consumers can recognize and degrade on.

    Status enum:
      captured_manual  — manual.json + screenshot + valid source_file (resources extracted)
      manual_unbound   — screenshot but no manual.json (no source_file, no resources)
      manual_invalid   — manual.json present but key fields broken (warned, NOT blocked)
      manual_pending   — manual.json present but no screenshot dropped yet
    """
    page_id = spec["id"]
    page_dir = spec["page_dir"]
    decl = spec.get("manual_decl") or {}
    warnings: list[str] = []

    # Locate screenshots
    screenshot = page_dir / "screenshot.png"
    fullpage = page_dir / "screenshot_fullpage.png"
    have_screenshot = screenshot.exists()
    have_fullpage = fullpage.exists()

    # Determine source_file (tolerant — warn but don't block on invalid)
    source_file = decl.get("source_file")
    source_file_valid = False
    if source_file:
        candidate = (SOURCE_ROOT / source_file).resolve() if not Path(source_file).is_absolute() else Path(source_file)
        if candidate.exists():
            source_file_valid = True
        else:
            warnings.append(
                f"manual.json declares source_file={source_file!r} but the file was not "
                f"found relative to source.root ({SOURCE_ROOT}). Resources will be empty; "
                f"please correct the path."
            )

    # Pick a status
    if spec["binding_status"] == "unbound":
        status = "manual_unbound"
        warnings.append(
            "No manual.json found alongside the user-dropped screenshot — page is "
            "unbound. Drop a manual.json with at least `source_file` to enable "
            "resource extraction and feature mapping."
        )
    elif not have_screenshot and not have_fullpage:
        status = "manual_pending"
        warnings.append(
            f"manual.json present at {page_dir / 'manual.json'} but no screenshot.png / "
            f"screenshot_fullpage.png was dropped yet. Capture the screen on your "
            f"device and save it here, then re-run the skill."
        )
    elif source_file and not source_file_valid:
        status = "manual_invalid"
    else:
        status = "captured_manual"

    # Build a minimal hierarchy stub so render_html / compare.py have something to
    # work with. The `manual: true` flag is the canonical "no real hierarchy" signal.
    hierarchy_stub = {
        "kind": "root",
        "platform": "manual",
        "manual": True,
        "children": [],
    }
    scroll_decl = decl.get("scroll") or {}
    if scroll_decl:
        vh = scroll_decl.get("viewport_height")
        ph = scroll_decl.get("page_total_height")
        if vh and ph and ph > vh:
            hierarchy_stub["scrolled"] = True
            hierarchy_stub["viewport_height"] = vh
            hierarchy_stub["page_total_height"] = ph
            hierarchy_stub["scroll_steps"] = scroll_decl.get("scroll_steps", 1)

    # Write hierarchy.json (stub)
    (page_dir / "hierarchy.json").write_text(
        json.dumps(hierarchy_stub, indent=2, ensure_ascii=False), encoding="utf-8")

    # Run resource extraction if source_file is valid
    resources_summary: Optional[dict] = None
    if source_file_valid:
        try:
            adapter = get_adapter(ADAPTER_NAME)
            rmanifest = adapter.extract_resources(source_file, SOURCE_ROOT, page_dir)
        except Exception as exc:
            warnings.append(f"resource extraction failed: {type(exc).__name__}: {exc}")
            rmanifest = None
        if rmanifest:
            resources_summary = {
                "dir": (page_dir / "resources").relative_to(_p_module.UI_ROOT).as_posix(),
                "policy": rmanifest.get("policy"),
                "stats": rmanifest.get("stats", {}),
            }

    # Render HTML with manual banner
    try:
        from render_html import render
        # Prefer fullpage for the visual reference, else viewport, else None
        if have_fullpage:
            shot_rel = "screenshot_fullpage.png"
        elif have_screenshot:
            shot_rel = "screenshot.png"
        else:
            shot_rel = None
        identity = {
            "top_component": decl.get("class_name") or "(manual)",
            "source_file": source_file or "(not declared)",
        }
        (page_dir / "view.html").write_text(
            render(
                hierarchy_stub,
                page_id=page_id,
                identity=identity,
                screenshot_rel=shot_rel,
                manual_meta={
                    "status": status,
                    "notes": decl.get("notes", ""),
                    "warnings": warnings,
                    "screenshot_present": have_screenshot or have_fullpage,
                },
            ),
            encoding="utf-8",
        )
    except Exception as exc:
        warnings.append(f"HTML render failed: {type(exc).__name__}: {exc}")

    # Build manifest entry
    page: dict = {
        "id": page_id,
        "status": status,
        "capture_method": "manual",
        "hierarchy_available": False,
        "source_file": source_file,
        "feature": decl.get("feature"),  # may be None — feature_map will try to fill from source_file
        "manual_notes": decl.get("notes", ""),
    }
    if have_screenshot:
        page["screenshot"] = (page_dir / "screenshot.png").relative_to(_p_module.UI_ROOT).as_posix()
    if have_fullpage:
        page["screenshot_fullpage"] = (page_dir / "screenshot_fullpage.png").relative_to(_p_module.UI_ROOT).as_posix()
    if hierarchy_stub.get("scrolled"):
        page["scroll"] = {
            "scrolled": True,
            "viewport_height": hierarchy_stub.get("viewport_height"),
            "page_total_height": hierarchy_stub.get("page_total_height"),
            "scroll_steps": hierarchy_stub.get("scroll_steps"),
            "source": "manual_declaration",
        }
    if resources_summary:
        page["resources"] = resources_summary
    if warnings:
        page["warnings"] = warnings

    # Write meta.json
    (page_dir / "meta.json").write_text(
        json.dumps(page, indent=2, ensure_ascii=False), encoding="utf-8")

    return page


def _scaffold_manual_page(page_id: str, source_file: Optional[str], feature: Optional[str]) -> int:
    """Create a manual-capture page folder with a manual.json template + README.

    Idempotent: refuses to overwrite an existing manual.json. Adds nothing else
    risky — the user does the rest by dropping screenshots.
    """
    page_dir = _p_module.UI_PAGES_DIR / _safe(page_id)
    page_dir.mkdir(parents=True, exist_ok=True)
    manual_json_path = page_dir / "manual.json"
    if manual_json_path.exists():
        _log(f"manual.json already exists at {manual_json_path} — not overwriting")
        return 0

    template = {
        "source_file": source_file or "REPLACE/ME/with/path/to/Fragment.java",
        "notes": "EDIT: Why is this page captured manually? (e.g. requires auth, complex flow…)",
    }
    if feature:
        template["feature"] = feature
    template["scroll"] = {
        "_comment": "If your screenshot is a pre-stitched full page, declare viewport+total here. "
                    "Otherwise delete this block.",
        "viewport_height": 2400,
        "page_total_height": 2400,
    }
    manual_json_path.write_text(
        json.dumps(template, indent=2, ensure_ascii=False), encoding="utf-8")

    readme = page_dir / "MANUAL_README.md"
    readme.write_text(
        f"# Manual capture: `{page_id}`\n\n"
        "1. Capture the screen on your device or emulator (e.g. via Android Studio "
        "screenshot, `adb exec-out screencap -p`, or your phone's screenshot button).\n"
        "2. Save it here as `screenshot.png` (or `screenshot_fullpage.png` if it's a "
        "pre-stitched scrolled page).\n"
        "3. Edit `manual.json` — fill in `source_file` (path relative to source.root) "
        "and `notes`. Delete the `scroll` block if the page fits one viewport.\n"
        "4. Re-run the skill: it will pick up this folder, extract resources from the "
        "declared source file, and integrate the page into the manifest.\n\n"
        "## What the skill will do for you\n\n"
        "- Copy source-side layouts / drawables / strings / arrays into `resources/`\n"
        "- Render `view.html` with a MANUAL banner so consumers know hierarchy data is "
        "absent\n"
        "- Add the page to `ui_manifest.json` with `status: captured_manual`\n\n"
        "## What the skill will NOT do\n\n"
        "- Stitch multiple loose screenshots — pre-stitch yourself if needed\n"
        "- Probe Fragment / Activity identity at runtime — `source_file` is your declaration\n",
        encoding="utf-8",
    )
    _log(f"scaffolded manual page at {page_dir}")
    _log(f"  edit {manual_json_path} and drop screenshot.png in {page_dir}")
    return 0


# ================================================================ end manual mode


def _rebuild_from_existing() -> list[dict]:
    """When --skip-capture: scan UI_PAGES_DIR for meta.json files and rebuild manifest entries."""
    pages: list[dict] = []
    if not UI_PAGES_DIR.exists():
        return pages
    for meta_file in sorted(UI_PAGES_DIR.glob("*/meta.json")):
        try:
            pages.append(json.loads(meta_file.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
    return pages


if __name__ == "__main__":
    raise SystemExit(main())
