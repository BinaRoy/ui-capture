#!/usr/bin/env bash
# Simulates a full capture run by stubbing `adb` so no real device is needed.
# The stub responds to: devices / shell dumpsys / shell uiautomator dump /
# pull / exec-out screencap with canned data drawn from the fixture.

set -euo pipefail

SKILL_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO_ROOT="$(cd "$SKILL_ROOT/../../.." && pwd)"
TMP="$(mktemp -d -t ui_capture_orch.XXXXXX)"

# Build a fake adb that responds based on argv
cat >"$TMP/adb" <<'ADB'
#!/usr/bin/env bash
case "$1 $2" in
  "devices ") echo "List of devices attached"; echo -e "emulator-5554\tdevice";;
  "shell dumpsys")
    # argv layout: $1=shell $2=dumpsys $3=activity $4={activities|<package>/<activity>}
    if [[ "$4" == "activities" ]]; then
      cat <<EOF
  mResumedActivity: ActivityRecord{abc 12345 com.wemaka.weatherapp/.ui.MainActivity t1}
EOF
    else
      # dumpsys activity <component>
      cat <<EOF
Added Fragments:
  #0: com.wemaka.weatherapp.ui.fragment.MainFragment{aaa} (id=...)
  #1: com.wemaka.weatherapp.ui.fragment.TodayWeatherFragment{bbb} (id=...)
EOF
    fi
    ;;
  "shell rm") exit 0;;
  "shell uiautomator")
    # `adb shell uiautomator dump /sdcard/...` — just succeed silently
    exit 0
    ;;
  "shell am") echo "Starting: Intent { ... }";;
  "shell input") exit 0;;
  "pull "*)
    # adb pull /sdcard/ui_capture_dump.xml <local-path>
    cp "${ADB_FIXTURE_XML}" "$3"
    ;;
  "exec-out screencap"|"exec-out screencap -p")
    # emit a 1x1 PNG
    printf '\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xcf\xc0\x00\x00\x00\x03\x00\x01h\x05\x96\xb6\x00\x00\x00\x00IEND\xaeB`\x82'
    ;;
  *)
    echo "fake-adb: unhandled args: $*" >&2
    exit 0
    ;;
esac
ADB
chmod +x "$TMP/adb"

# Also: the orchestrator calls `adb exec-out screencap -p` as subprocess.run directly.
# That's already handled above with case "exec-out screencap"/"exec-out screencap -p".

# Use a scratch nav script and a scratch UI root, both in the temp dir, so we never
# touch the operator-edited production artifacts at output/workflow_output/ui/.
NAV="$TMP/nav_script_test.sh"
cat >"$NAV" <<'NAV'
#!/usr/bin/env bash
set -euo pipefail
# (no PACKAGE/MAIN_ACTIVITY checks — fake adb is launched via PATH override)
capture_page weather_main
capture_page settings/main
NAV
chmod +x "$NAV"

UI_ROOT_TEST="$TMP/ui"
mkdir -p "$UI_ROOT_TEST"

export PATH="$TMP:$PATH"
export ADB_FIXTURE_XML="$SKILL_ROOT/fixtures/sample_uiautomator_dump.xml"
export IOS2CJ_WORKFLOW_CONFIG="$REPO_ROOT/workflow.config.json"

echo "--- running orchestrator with stubbed adb ---"
python "$SKILL_ROOT/scripts/run.py" --mode standalone \
  --nav-script "$NAV" --skip-scaffold --nav-timeout 30 \
  --ui-root "$UI_ROOT_TEST"

echo
echo "--- post-run artifacts (in temp UI root, not production) ---"
ls "$UI_ROOT_TEST/pages" || true
echo
echo "--- manifest pages ---"
python - <<PY
import json, pathlib
m = json.loads(pathlib.Path("$UI_ROOT_TEST/ui_manifest.json").read_text())
print("status:", m["status"])
for p in m.get("pages", []):
    print(f"  {p['id']:20s} status={p['status']:10s} feature={p.get('feature')} class={p.get('class_name')}")
print("shell_features:", m.get("shell_features"))
PY

# Sanity: each captured page directory must have screenshot, hierarchy, view.html, meta.
# Path naming: '/' in the page_id becomes a single '_' (Python orchestrator's _safe()).
fail=0
for pid in weather_main settings_main; do
  for f in screenshot.png hierarchy.json view.html meta.json raw.xml; do
    p="$UI_ROOT_TEST/pages/$pid/$f"
    if [[ ! -s "$p" ]]; then
      echo "MISSING or empty: $p" >&2
      fail=1
    fi
  done
done

# Verify both pages reached status=captured in the manifest.
python3 - <<PY || fail=1
import json, sys, pathlib
m = json.loads(pathlib.Path("$UI_ROOT_TEST/ui_manifest.json").read_text())
expected = {"weather_main", "settings/main"}
captured = {p["id"] for p in m.get("pages", []) if p.get("status") == "captured"}
missing = expected - captured
if missing:
    print("FAIL: pages not captured:", missing, file=sys.stderr)
    sys.exit(1)
print("OK: both pages captured")
PY

rm -rf "$TMP"
[[ $fail -eq 0 ]] && echo "OK — orchestrator dry run completed" || { echo "FAIL"; exit 1; }
