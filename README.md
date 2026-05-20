# ui-capture

Cross-platform UI capture + diff for mobile-app migration workflows.

Drives an **Android emulator** or **HarmonyOS / OpenHarmony emulator** to capture each page's
screenshot, view hierarchy (as a normalized UI-IR JSON), and HTML projection. Pairs
captures across platforms and emits a structural diff — primarily designed for verifying
Android → Cangjie/HarmonyOS translation.

## Status

| Platform | Adapter | Status |
|---|---|---|
| Android | `adapters/android.py` | ✅ Stable. uiautomator dump + screencap + scroll stitching + resource extraction |
| HarmonyOS / Cangjie | `adapters/harmony.py` | ✅ Functional. `hdc uitest dumpLayout` + `snapshot_display` + anchor-based scroll stitching + Cangjie static nav discovery |
| iOS | `adapters/ios.py` | ❌ Stub only |

End-to-end validated on Android WeatherApp ↔ Cangjie WeatherApp translation.

## What you get

For each captured page:

```
pages/<slug>/
├── hierarchy.json          ← normalized UI-IR (the diff target)
├── screenshot.png          ← viewport screenshot
├── screenshot_fullpage.png ← stitched scroll-full image (if scrollable)
├── view.html               ← human-readable visualization
├── overlay.svg             ← bounds overlay
├── meta.json               ← capture provenance
├── raw.json / raw.xml      ← adapter's original dump (for debugging)
└── resources/              ← extracted strings / drawables (Android only)
```

Cross-platform diff:

```
output_cross/<source>_vs_<target>/<slug>/
├── compare.html            ← side-by-side viewer
├── diff.json               ← structured node-level diff
├── diff.md                 ← human-readable diff
└── meta.json               ← which workflows were paired
```

## Quick start

**Prereqs:** Python 3.9+, an emulator running, the target app installed.

Detailed environment setup for macOS: [`docs/INSTALL_macos.md`](docs/INSTALL_macos.md).

```bash
# 1. Configure a workflow per platform you want to capture.
cat > output_android/workflow.config.json <<EOF
{
  "source":   { "root": "/path/to/your/AndroidApp" },
  "workflow": { "root": "/path/to/output_android/workflow_output" },
  "adapter":  { "name": "generic_android" }
}
EOF

# 2. Capture one platform. The nav script is the project-specific piece you
#    customize (which buttons to tap, which pages to visit).
IOS2CJ_WORKFLOW_CONFIG=$(pwd)/output_android/workflow.config.json \
  python3 ui-capture/scripts/run.py --mode standalone

# 3. Capture the other platform the same way (different config / adapter).

# 4. Cross-platform diff (skill derives output path; no mkdir needed):
IOS2CJ_WORKFLOW_CONFIG=$(pwd)/output_android/workflow.config.json \
  python3 ui-capture/scripts/compare.py \
    --page weather_main \
    --target-workflow generic_harmony \
    --dump-diff
```

## Directory layout

```
<your-captures-parent>/
├── output_android/          ← per-platform capture root (one per adapter)
│   ├── workflow.config.json
│   └── workflow_output/ui/
│       ├── pages/<slug>/    ← per-page artifacts
│       ├── nav_script/      ← project-specific nav scripts
│       └── ui_manifest.json
├── output_harmony/          ← same shape, HarmonyOS captures
└── output_cross/            ← cross-platform diff (auto-created by skill)
    └── <source>_vs_<target>/<slug>/
```

Path derivation is encoded in `scripts/_paths.py` — callers pass adapter names or
workflow roots, the skill computes the rest.

## Architecture / contributing

- [`SKILL.md`](SKILL.md) — the skill's contract with Claude Code (frontmatter +
  operations + integration points).
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — adapter matrix, capture
  pipeline phases, nav-automation chain, output file catalogue, output
  directory convention.

## Failure modes (quick reference)

| Symptom | Likely cause | Fix |
|---|---|---|
| `infrastructure_missing` | no device attached / `adb` or `hdc` not in PATH | start emulator / install platform-tools / DevEco Studio |
| `scaffold_pending` | nav script not written yet | first run scaffolds it; edit before re-running |
| page captured but content wrong (e.g. settings shows weather body) | previous capture left page scrolled; subsequent tap coords drift | fixed in 2026-05-19; `_capture_fullpage` now scrolls back to top |
| identical hierarchy across two pages | `aa start` reused stale state | nav script must `aa force-stop` before `aa start` (HarmonyOS) |
| `uitest dumpLayout` empty | screen mid-transition | adapter waits for stable layout via dump hash polling; increase `STABLE_MAX_WAIT_S` if your app is slow |

## Testing without a device

```bash
python3 ui-capture/tests/test_dryrun.py
python3 ui-capture/tests/test_schema.py
```

Exercises discover, normalize, render_html, schema, and diff using on-disk fixtures.

## License

MIT — see [`LICENSE`](LICENSE).
