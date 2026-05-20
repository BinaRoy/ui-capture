#!/usr/bin/env bash
# Reference nav script template for Android. The Android adapter renders this on
# first scaffold using static discovery; in practice you will edit it by hand
# afterwards. This file is here as documentation of the contract.
#
# Contract:
#   - The wrapper exports a function `capture_page <page_id>` that synchronously
#     signals the orchestrator to dump the current screen and waits for ack.
#   - PACKAGE and MAIN_ACTIVITY are required env vars (typically set by the operator
#     or written into the generated nav script).
#   - $ADB resolves to the adb binary the adapter found at scaffold time; override
#     with `ADB=/path/to/adb` if needed. Don't call `adb` bare — Claude Code Bash
#     sessions often don't have platform-tools in PATH.
#   - Use `"$ADB" shell am start`, `"$ADB" shell input tap`, etc. to navigate.
#   - Sleep between navigation and capture so the screen settles. uiautomator dump
#     can fail on transitions.

set -euo pipefail

ADB="${ADB:-adb}"
: "${PACKAGE:?set PACKAGE}"
: "${MAIN_ACTIVITY:?set MAIN_ACTIVITY}"

"$ADB" shell am start -n "${PACKAGE}/${MAIN_ACTIVITY}"
sleep 2

capture_page weather_main

# Navigate to settings (example — adjust taps to your app)
# "$ADB" shell input tap 950 200
# sleep 1
# capture_page settings/main

# Drill into settings detail
# "$ADB" shell input tap 540 600
# sleep 1
# capture_page settings/detail

# Open search menu
# "$ADB" shell input tap 540 200
# sleep 1
# capture_page search_menu
