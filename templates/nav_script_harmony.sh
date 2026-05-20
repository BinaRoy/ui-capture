#!/usr/bin/env bash
# Reference nav script template for HarmonyOS. The harmony adapter renders this
# on first scaffold; you will edit it by hand afterwards.
#
# Contract:
#   - The wrapper exports a function `capture_page <page_id>` that synchronously
#     signals the orchestrator to dump the current screen and waits for ack.
#   - BUNDLE and ABILITY are required env vars (set in the generated script or
#     by the operator).
#   - $HDC resolves to the hdc binary the adapter found at scaffold time; override
#     with `HDC=/path/to/hdc` if needed. Don't call `hdc` bare — DevEco Studio's
#     hdc is usually not on PATH. The adapter probes
#     /Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/toolchains/hdc.
#   - Use `"$HDC" shell aa start`, `"$HDC" shell uitest uiInput click`, etc.
#   - Sleep between navigation and capture so the screen settles (ArkUI's
#     LazyForEach and async data loads can take seconds).
#   - Coordinates: get them from a manual `hdc shell uitest dumpLayout`; bounds
#     are `[x1,y1][x2,y2]` so tap point is roughly the center.

set -euo pipefail

HDC="${HDC:-hdc}"
: "${BUNDLE:?set BUNDLE — module.json5 bundleName}"
: "${ABILITY:?set ABILITY — main ability class, e.g. EntryAbility}"

"$HDC" shell aa start -a "${ABILITY}" -b "${BUNDLE}"
sleep 4
echo 'capture_page <first_page_slug>'

# Example: tap into a settings page
# "$HDC" shell uitest uiInput click 600 1800
# sleep 2
# echo 'capture_page settings'
# "$HDC" shell uitest uiInput keyEvent 2   # back
# sleep 2
