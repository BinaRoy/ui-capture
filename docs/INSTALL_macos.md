# macOS 环境配置（Android + HarmonyOS）

ui-capture 在 macOS（含 Apple Silicon）的完整环境安装说明。验证过的组合：macOS 14+ on M-series。

## 概览

| 平台 | 工具链 | 模拟器 |
|---|---|---|
| Android | JDK 17 + Android SDK + `adb` | AVD（M-series 必须用 arm64-v8a image） |
| HarmonyOS / Cangjie | DevEco Studio + `hdc` | DevEco 自带 OpenHarmony 模拟器 |
| iOS | （未实现） | — |

## 1. Android 工具链

```bash
# JDK 17 via conda
conda install -c conda-forge openjdk=17
export JAVA_HOME=~/miniforge3/lib/jvm

# Android command-line tools
mkdir -p ~/android-sdk/cmdline-tools
curl -L "https://dl.google.com/android/repository/commandlinetools-mac-11076708_latest.zip" -o /tmp/cmdline-tools.zip
unzip -q /tmp/cmdline-tools.zip -d ~/android-sdk/cmdline-tools
mv ~/android-sdk/cmdline-tools/cmdline-tools ~/android-sdk/cmdline-tools/latest

export ANDROID_HOME=~/android-sdk
export PATH=$PATH:$ANDROID_HOME/cmdline-tools/latest/bin:$ANDROID_HOME/platform-tools:$ANDROID_HOME/emulator

# Platform tools + emulator + a system image (arm64-v8a on Apple Silicon)
sdkmanager "platform-tools" "emulator" "platforms;android-34" "system-images;android-34;default;arm64-v8a"

# AVD
avdmanager create avd -n Pixel_7 -k "system-images;android-34;default;arm64-v8a"

# Verify
adb version       # should print Android Debug Bridge version 1.x
```

Optional convenience symlink:
```bash
mkdir -p ~/bin
ln -sf ~/android-sdk/platform-tools/adb ~/bin/adb
# ensure ~/bin is in PATH
```

## 2. HarmonyOS / OpenHarmony 工具链

Install [DevEco Studio](https://developer.huawei.com/consumer/en/deveco-studio/). Default SDK location on macOS:

```
/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/toolchains/hdc
```

Verify:
```bash
/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/toolchains/hdc list targets
# starts the daemon and lists connected devices
```

Start the OpenHarmony emulator from DevEco Studio's Device Manager (no headless mode currently).

## 3. ui-capture skill checkout

```bash
git clone <repo-url> ~/work/ui-capture
cd ~/work/ui-capture

# Optional Python deps (only Pillow is required, for fullpage stitching)
pip3 install Pillow jsonschema
```

The skill also expects a `paths.py` shim at `~/.claude/scripts/lib/paths.py`. The repo
ships one at `scripts/_paths.py` that auto-falls-back to `~/.claude/scripts/lib/`. If
running standalone, copy:

```bash
mkdir -p ~/.claude/scripts/lib
cp ~/work/ui-capture/scripts/_paths_shim.py ~/.claude/scripts/lib/paths.py  # if/when added
```

For now, the existing shim at `~/.claude/scripts/lib/paths.py` is loaded automatically.

## 4. Per-project workflow config

Create one `workflow.config.json` per platform you want to capture. The skill reads
the file pointed to by `$IOS2CJ_WORKFLOW_CONFIG`.

```json
{
  "source":   { "root": "/abs/path/to/your-app-source" },
  "workflow": { "root": "/abs/path/to/output_<platform>/workflow_output" },
  "adapter":  { "name": "generic_android"  } 
}
```

`adapter.name` is one of `generic_android` / `generic_harmony`.

## 5. Running

### Android example (WeatherApp)

```bash
# Start emulator headless (Apple Silicon needs arm64-v8a image)
~/android-sdk/emulator/emulator -avd Pixel_7 -no-snapshot -no-audio -no-window &
# Wait ~30s, then verify
adb devices

# Install the app (one-time)
adb install path/to/your-app.apk

# Configure
cat > /path/to/output_android/workflow.config.json <<EOF
{
  "source":   { "root": "/path/to/your-app-src" },
  "workflow": { "root": "/path/to/output_android/workflow_output" },
  "adapter":  { "name": "generic_android" }
}
EOF

# Run skill — first run scaffolds a nav script you'll edit, then re-run
IOS2CJ_WORKFLOW_CONFIG=/path/to/output_android/workflow.config.json \
  python3 ~/work/ui-capture/scripts/run.py --mode standalone
```

### HarmonyOS example (Cangjie WeatherApp)

```bash
# 1. Open the Cangjie project in DevEco Studio
# 2. Start an OpenHarmony emulator from Device Manager
# 3. Install/run the app to the emulator (DevEco "Run" button)

cat > /path/to/output_harmony/workflow.config.json <<EOF
{
  "source":   { "root": "/path/to/cangjie-project" },
  "workflow": { "root": "/path/to/output_harmony/workflow_output" },
  "adapter":  { "name": "generic_harmony" }
}
EOF

IOS2CJ_WORKFLOW_CONFIG=/path/to/output_harmony/workflow.config.json \
  python3 ~/work/ui-capture/scripts/run.py --mode standalone
```

### Cross-platform diff

After capturing both sides, no manual setup is needed — the skill derives the output
path automatically:

```bash
IOS2CJ_WORKFLOW_CONFIG=/path/to/output_android/workflow.config.json \
  python3 ~/work/ui-capture/scripts/compare.py \
    --page weather_main \
    --target-workflow generic_harmony \
    --dump-diff

# Output lands at:
# /path/to/output_cross/android_vs_harmony/weather_main/
#   compare.html  diff.json  diff.md  meta.json
```

## 6. Useful flags (run.py)

| Flag | Purpose |
|---|---|
| `--mode standalone` | Capture-only run (no upstream phase deps) |
| `--pages weather_main,settings` | Capture a subset, skip the rest |
| `--skip-capture` | Re-emit manifest + HTML without re-driving the device |
| `--skip-discover` | Skip static source scan (faster after the first run) |
| `--ui-root /tmp/test_out` | Send all outputs elsewhere (useful for experiments) |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `adb` can't find device | Wait ~30s after `emulator` boots; or check `pgrep -f qemu-system-aarch64` |
| `hdc` not found | `export HDC=/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/toolchains/hdc` |
| `nav_script_*.sh: not present` | First run scaffolds it; edit before re-running |
| `paths.py` ModuleNotFoundError | Confirm `~/.claude/scripts/lib/paths.py` exists |
| `Unable to locate a Java Runtime` | `export JAVA_HOME=~/miniforge3/lib/jvm` (system stub doesn't work) |
| Page captured but content wrong | Likely fullpage scroll didn't restore top — already fixed in adapter, but if writing custom adapter make sure to scroll back |
| HarmonyOS: capture identical across pages | Nav script must `aa force-stop` before `aa start` (cold-start) |

## Kill emulators

```bash
# Android
adb -s emulator-5554 emu kill

# HarmonyOS — close via DevEco Studio Device Manager
```
