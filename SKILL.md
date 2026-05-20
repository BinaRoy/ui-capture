---
name: ui-capture
description: Cross-platform UI capture + diff for the Android ↔ Cangjie/HarmonyOS migration workflow. Drives an Android emulator (`adb` + uiautomator) or HarmonyOS emulator (`hdc` + `uitest dumpLayout`) to capture screenshots, normalized view hierarchies (UI-IR JSON), and HTML projections of every key page. Pairs captures across two workflows and emits a structural diff. Always use this skill during /decompose Step 1.25 to seed /architecture-gen, during /review to compare an Android source page against its HarmonyOS counterpart, or whenever the user asks to refresh page screenshots, regenerate the UI manifest, capture an app's screens on either platform, or produce a cross-platform visual diff. iOS adapter is stubbed (not yet implemented). The manifest and HTML are consumed by /architecture-gen to plan the target layout and by Worker/Reviewer agents as visual references during feature implementation.
---

# UI Capture

Cross-platform UI surface capture for Android ↔ Cangjie/HarmonyOS migration.

| Side | Trigger | Mechanism | Needs running infra? |
|---|---|---|---|
| **Android capture** | `/decompose` Step 1.25; manual rerun | runtime: drive emulator via nav script, dump uiautomator XML + screenshot | yes — Android emulator + app installed |
| **HarmonyOS capture** | `/review`; manual; after Workers complete a feature | runtime: drive emulator via nav script, `hdc uitest dumpLayout` + `snapshot_display` | yes — OpenHarmony emulator + app installed |
| **Cross-platform diff** | `/review`; manual | join two captured workflows, produce side-by-side HTML + structural diff JSON/MD | no |

Both adapters emit the **same UI-IR shape** (see `schema/hierarchy.schema.json`), so the HTML renderer and comparison logic are reused. Page identity on the source side is **observed at runtime** (top Activity / NavDestination / etc. at capture time), not guessed from static analysis. Static discovery feeds the nav-script scaffolder and the auto-resolver only.

## Why this skill exists

`/architecture-gen` plans the target Cangjie module layout. Without knowing the page inventory of the input, it has to guess at how many pages exist and what role each plays. That guess is usually wrong on apps with non-trivial UI. This skill produces the missing input.

Worker and Reviewer agents also benefit: a Worker building a Cangjie page can read the source HTML to understand layout intent; a Reviewer can compare structure (not pixels) between source and target.

## When this skill runs

Three operations. Pick automatically based on the user's ask:

| Operation | When | Entry script |
|---|---|---|
| Capture, either side (`pipeline` or `standalone`) | `/decompose` Step 1.25; manual rerun on either Android or HarmonyOS | `scripts/run.py` (adapter selected by `workflow.config.json::adapter.name`) |
| Same-workflow compare (legacy) | comparing source vs a `target_pages/` snapshot in one workflow | `scripts/compare.py --page <page_id>` |
| Cross-workflow diff | `/review`; user asks "diff Android vs HarmonyOS" / "compare the two captures" | `scripts/compare.py --page <page_id> --target-workflow <adapter\|path> --dump-diff` |

Cross-workflow diff is the canonical path for Android ↔ HarmonyOS comparison since
2026-05-19. Output lands at `<captures_parent>/output_cross/<source>_vs_<target>/<slug>/`,
which the skill auto-creates from path rules in `_paths.py` — agents do not mkdir.

Source capture has two modes via `--mode`:

| Mode | Behavior |
|---|---|
| `pipeline` | Reads existing `feature.json`; failures degrade to partial manifest, do not block. Used by `/decompose`. |
| `standalone` | Same outputs; can target a subset of pages with `--pages <id>,<id>`. Used for manual reruns. |

Target extract and compare are always standalone — they don't need a mode flag.

## Prerequisites — check these first and stop if any fail

The skill cannot fabricate data. If the runtime environment is not present, emit a **stub manifest** with `status: "infrastructure_missing"` and tell the user exactly what to set up. Do not pretend pages were captured.

| Platform | Required | Check command |
|---|---|---|
| Android | `adb` in PATH, device or emulator booted, target app installed | `adb devices` shows a device; `adb shell pm list packages \| grep <package>` returns the package |
| HarmonyOS | `hdc` reachable (DevEco Studio installed at `/Applications/DevEco-Studio.app`), OpenHarmony emulator running, target HAP installed | `hdc list targets` shows a device; `hdc shell bm dump -a \| grep <bundleName>` returns the bundle |
| iOS | `xcrun simctl` available, a booted simulator, target app installed | `xcrun simctl list devices booted`; `xcrun simctl get_app_container booted <bundle_id>` — **iOS adapter is currently a stub; not functional** |

Also required:

- `workflow.config.json` resolvable (via `IOS2CJ_WORKFLOW_CONFIG` env or `<core.root>/workflow.config.json`)
- `feature.json` exists at `<workflow.root>/feature_develop/feature.json` (output of `/decompose` Step 1). If missing, the skill can still capture pages but `feature` field in the manifest will be `null` and a warning is emitted.

## Execution flow

The orchestrator `scripts/run.py` chains four phases. In normal usage just call it once; the per-phase scripts are exposed for troubleshooting.

```
phase 1  discover_hints   adapter.discover_hints()       (in-memory; fed into phase 2)
phase 2  scaffold         render nav-script template     → templates/<adapter>/nav_script.<ext>
                                                           (only if missing — never overwrites)
phase 3  capture          run nav script, per-page:      → pages/<id>/raw.{xml|plist}
                            adapter.probe_identity()       pages/<id>/screenshot.png
                            adapter.capture(page)          pages/<id>/hierarchy.json (UI-IR)
                            adapter.normalize(raw)         pages/<id>/view.html
phase 4  finalize         feature_map.attach()           → ui_manifest.json
                          render summary report          → report.md
```

Run as:

```bash
IOS2CJ_WORKFLOW_CONFIG=<workflow.config> \
  python <core.root>/.claude/skills/ui-capture/scripts/run.py \
    --mode pipeline    # or: --mode standalone [--pages a,b,c] [--skip-capture]
```

Notes on the phases:

- **Phase 1 (`discover_hints`)** is allowed to be lossy. On Android with Jetpack Compose, on iOS with SwiftUI, on apps with custom navigation frameworks, the static scan won't find all pages — that's fine. Hints feed phase 2 only.
- **Phase 2 (`scaffold`)** creates a starter nav script if one does not exist. It does not overwrite. The operator edits this file by hand to reach pages the static scan missed. Skill body never edits the nav script for the user — that's a project-specific concern.
- **Phase 3 (`capture`)** is the only phase that needs running infrastructure. It iterates page IDs from the nav script. For each, it dumps the raw platform hierarchy, screenshot, and probes "what page am I actually on" (top Activity + current Fragment / top UIViewController). The probed identity is the source of truth for the page ID. Pages that fail (nav timeout, screen mismatch, dump error) are recorded with `status: failed` and a reason; they do not abort the run.
- **Phase 4 (`finalize`)** joins captured pages to feature DAG by matching their source file (resolved from the class name) against the `files` array of each feature. Pages with no matching feature are still listed under `orphan_pages`.

## Outputs (canonical layout)

All under `<workflow.root>/ui/`:

```
ui/
  ui_manifest.json              ← canonical source-side artifact, consumed by arch-gen
  report.md                     ← human-readable summary
  nav_script/                   ← the operator-edited nav script lives here
  pages/                        ← source side (per captured screen)
    <page_id>/
      screenshot.png            ← PNG, viewport (what user first sees)
      screenshot_fullpage.png   ← PNG, stitched full page with fold-line annotations
                                  (only when the page is scrollable)
      hierarchy.json            ← normalized UI-IR (platform-agnostic, scroll metadata at root)
      raw.xml | raw.plist       ← platform-native dump (kept for debugging)
      view.html                 ← HTML projection of hierarchy.json
      meta.json                 ← {status, captured_at, source_file, feature, scroll, errors}
      resources/                ← source-side resources for the Worker to consume
        layouts/<name>.xml      ← the page's layout XML files, verbatim
        drawables/<name>.{xml,png,webp}  ← drawables directly referenced
        mipmaps/<name>.{xml,png}         ← app icons / launcher refs
        strings.txt             ← key⇥value lines for referenced string resources
        colors.txt              ← key⇥value lines for referenced color resources
        resources_manifest.json ← full provenance + policy notes for Worker
  target_pages/                 ← target side (per Cangjie page class)
    target_manifest.json        ← list of extracted target pages
    <slug>/
      hierarchy.json            ← UI-IR parsed statically from .cj
      view.html                 ← HTML projection (flow layout, no bounds)
      meta.json                 ← {class_name, source_file, builders_inlined}
  compare/                      ← cross-side comparisons
    <page_id>.html              ← side-by-side source + target + structural diff
```

Page IDs follow:

- Single main page per feature: `<feature_id>` (e.g. `weather_main`)
- Multiple main pages per feature: `<feature_id>/<slug>` (e.g. `settings/main`, `settings/detail`)
- Sub-card visible on a parent page (not navigated to as a screen): `<feature_id>#<slug>` (e.g. `weather_main#today_section`)

See `schema/ui_manifest.schema.json` for the manifest schema. The arch-gen contract is:

- `pages` array entries with `status: captured` are authoritative
- entries with `status: failed | skipped` are listed so arch-gen knows the page exists but lacks fidelity; arch-gen should plan a slot but mark it as approximate
- `shell_features` lists features that have no UI surface (e.g. `app_shell`) — these are legitimate, not failures

## Failure handling

Aim for **graceful degradation, not abort**. The manifest must be emitted even on partial capture, because arch-gen will block otherwise.

| Failure | Behavior |
|---|---|
| No device / emulator booted | Emit stub manifest with `status: infrastructure_missing`, write report explaining setup steps, exit 0 (in pipeline mode) so decompose can choose to proceed with a UI-blind arch-gen |
| Nav script missing | Generate scaffold via phase 2, emit empty manifest with `status: scaffold_pending`, tell operator where to edit |
| Nav script reaches wrong page | Record `status: identity_mismatch` for that page with both expected and actual identity, continue |
| Single page dump fails | Record `status: failed` with reason, continue with next page |
| All pages fail | Manifest still emitted, all entries `status: failed`, exit 1 (so caller can decide) |

In `pipeline` mode, exit 0 unless catastrophic (config unreadable, all pages failed). In `standalone` mode, exit non-zero on any failure so the operator notices.

## Manual capture (degraded mode for unscriptable pages)

Some pages cannot be driven by the nav script — typical reasons:

- Authentication required (OAuth, Google / Apple sign-in, SMS code)
- Server-state-dependent screens (cart non-empty, unread message banner)
- Pages reached only via system dialogs (permission grants, share callbacks)
- WebView content (uiautomator can't dump the embedded web tree)

For these, the operator captures a screenshot manually and drops it into the
page's folder. The skill picks it up, runs static resource extraction, and
integrates the page into the manifest — clearly marked so downstream consumers
know there is no real view hierarchy.

### Workflow

1. **Scaffold a manual page folder** (optional but recommended):

   ```
   python <core.root>/.claude/skills/ui-capture/scripts/run.py \
     --new-manual auth_login \
     --source-file app/src/main/java/.../LoginFragment.java \
     --feature auth
   ```

   This writes `pages/auth_login/manual.json` (template) and `MANUAL_README.md`.

2. **Drop the screenshot** into `pages/<page_id>/` as `screenshot.png` (or
   `screenshot_fullpage.png` if you've pre-stitched a scrolled page).

3. **Edit `manual.json`** to set `source_file`, `notes`, optional `scroll`.

4. **Re-run the skill** normally — the auto capture loop skips this page,
   resource extraction runs against the declared source file, HTML is rendered
   with a blue MANUAL banner.

### Status enum (manifest)

| status | meaning |
|---|---|
| `captured` | normal auto capture |
| `captured_manual` | manual.json + screenshot + valid source_file — resources extracted |
| `manual_unbound` | screenshot only, no manual.json — manifest entry created with warning, no resources |
| `manual_invalid` | manual.json present but source_file points nowhere — tolerated, warning surfaced |
| `manual_pending` | manual.json present but no screenshot yet — operator hasn't finished |

All four manual statuses are non-blocking — the orchestrator continues and
emits the manifest. Reviewers / arch-gen are expected to treat manual pages
as visual-only references and skip structural diff.

### Auto / manual coexistence

`pages/*/manual.json` (and bare screenshots) are discovered BEFORE the capture
loop runs. The matching `page_id` is excluded from auto capture (no wasted adb
calls). When the same `page_id` exists in both nav script and manual folder,
manual wins.

### What manual mode does NOT do

- Does not auto-stitch loose screenshots — if you took 3 viewport shots of a
  scrolling page, pre-stitch them yourself before dropping.
- Does not probe Fragment / Activity identity at runtime — `source_file` in
  `manual.json` is your authoritative declaration.
- Does not validate that the screenshot actually matches the declared class —
  no good way to do that without a runtime dump.

## Resource extraction (source-side images, layouts, strings)

After each successful capture, the adapter walks the page's source code for resource
references and copies them into `pages/<id>/resources/` so the Worker has the exact
assets the original app uses. For Android this covers:

- `R.layout.X` and ViewBinding class usages → copies the layout XML
- `R.drawable.X` (in code) + `@drawable/X` (in layouts) → copies the drawable file
- `R.mipmap.X` + `@mipmap/X` → copies the mipmap file
- `R.string.X` + `@string/X` → looks up value in `values/strings.xml`
- `R.color.X` + `@color/X` → looks up value in `values/colors.xml`

The policy is **non-recursive**: a copied selector / shape / layer-list XML may
internally reference further drawables that we do NOT auto-follow. The
`resources_manifest.json` makes this explicit via a `policy_note` and per-entry
`has_inner_refs` flag. Workers must follow `copied_from` to find transitive
dependencies in the source tree.

Density buckets: only the highest-density raster is copied (xxxhdpi → mdpi
fallback chain). Vector XMLs are density-independent and copied verbatim.

iOS resource extraction is currently a no-op stub — the adapter returns None.
Adding it requires parsing `.xcassets` for image-set references; the interface
is `Adapter.extract_resources()`.

## Target-side extraction (Cangjie / ArkUI)

`scripts/extract_target.py` parses `.cj` page files into the same UI-IR as the source side. It recognizes:

- `@Component class XxxPage { ... func build() { ... } }` — the page root
- `@Builder func yyy() { ... }` — inlined when referenced via `this.yyy()`
- `Component(...args...) { children }.modifier(args)...` — the canonical ArkUI grammar
- `ForEach(seq, fn)` → emits a `list` node with one stand-in child
- `if (cond) { ... } else { ... }` → emits a `branch` node with both arms

Bounds are not available statically, so the HTML uses a nested-block flow layout instead of absolute positioning. The tree pane is the primary structural view.

Run:

```
python <core.root>/.claude/skills/ui-capture/scripts/extract_target.py
```

Defaults to `target.project_root` from `workflow.config.json`; override with `--target-root <path>` or `--file <single.cj>`.

For Cangjie syntax questions during this work, consult the `cangjie-kb` MCP server (queries against the official ArkUI documentation).

## Source ↔ target comparison

`scripts/compare.py --page <page_id>` joins a source capture and the corresponding target extraction into one HTML.

Pairing rule (auto):
1. Exact slug match: source `page_id` (with `/` → `_`) against target slug
2. Feature match: `page.feature` from `ui_manifest.json` against target slug
3. Suffix overlap fallback

Override with `--target <slug>` when auto-pairing picks wrong.

The output `<workflow.root>/ui/compare/<page_id>.html` shows:
- Source pane: screenshot + bounded layout + tree
- Target pane: flow layout + tree
- Structural diff: per-`kind` counts (text/button/list/etc), with notes (match, target-only, missing, Δ)
- Text content drift: literal strings present on one side only

This is the primary artifact for Reviewer agents to check whether a Cangjie page corresponds structurally to the Android source. **It does not do pixel diff** — that would always fail cross-platform.

## Adapter selection

`scripts/run.py` reads `adapter.name` from `workflow.config.json` and dispatches:

| `adapter.name` | Adapter |
|---|---|
| `generic_android` | `adapters/android.py` |
| `generic_ios` | `adapters/ios.py` (stub — not yet implemented) |
| anything else | error: "no UI adapter for adapter.name=<x>" |

Future iOS work: implement `adapters/ios.py` against `xcrun simctl` + `lldb` view-dump. The interface is `adapters/base.py:Adapter` — that contract is stable.

## Standalone CLI

When the user asks to rerun outside the pipeline:

- "regenerate the manifest" → `--mode standalone`
- "just re-screenshot the settings page" → `--mode standalone --pages settings/main,settings/detail`
- "rebuild HTML from existing dumps without recapturing" → `--mode standalone --skip-capture --skip-discover`

After standalone runs, summarize what changed and point the user at `report.md`.

## Auto-resolve nav targets (Phase 2b, generic)

The default scaffold leaves every non-launcher hint as a commented-out
TODO block — the operator has to fill `adb shell input tap X Y`
manually. For typical Android apps that's the largest cause of partial
UI coverage. Two scripts narrow that gap, with no app-specific code.

| Step | Script | Needs adb? | What it does |
|---|---|---|---|
| Static resolve | `scripts/auto_resolve_nav.py` | no | Scans Android source for the click handler that opens each hint's Fragment / Activity, walks back to the enclosing `R.id.<view_id>` listener install, emits `nav_resolution.json` mapping hint slug → view id |
| Live coord fill | `scripts/auto_fill_nav_script.py` | yes | Pulls `uiautomator dump` from the current screen, matches each resolved view id's `bounds`, computes tap center, rewrites matching TODO blocks in `nav_script_android.sh` with real `adb shell input tap X Y` lines (preserves a `.bak`) |

Static resolve is wired into `run.py --mode pipeline` as phase 2b
automatically (`--skip-auto-resolve` to disable). Live coord fill is
not auto-invoked — it needs adb + a booted emulator and only fills
hints reachable from the **current** screen. Multi-hop nav still
requires the operator to drive the emulator between fills.

The static resolver handles three Android idioms:
- `new Foo()` / `Foo.newInstance(...)` inside an `onClick` (with prior `findViewById(R.id.x).setOnClickListener` install)
- `binding.<camelCaseBtn>.setOnClickListener { ... new Foo() ... }`
- `NavController.navigate(R.id.action_to_X)` resolved via nav graph XML to a destination matching the hint class

`unresolved` is reported when a hint isn't a real navigation target
(host fragments, fragments instantiated only by `replace(R.id.container, ...)`
calls in the activity's own `onCreate`, etc.) — those cases should be
deleted from the nav script, not filled.

## What this skill does NOT do

- It does not write or edit the project-side nav script beyond initial scaffolding. The nav script encodes app-specific knowledge that only a human (or a project-aware agent run in a different turn) should set.
- It does not interpret the UI semantically (e.g. "this is a login screen"). It records structure. Semantic labeling, if needed, belongs in a later step.
- It does not do pixel-diff between source and target. Reviewer agents can build that on top of `hierarchy.json` if they want structural diff.

## Quick reference

- Entry script: `scripts/run.py`
- Adapter interface: `adapters/base.py`
- Manifest schema: `schema/ui_manifest.schema.json`
- Operator guide: `README.md`
- Testing without real device: see `tests/README.md` (fixture-driven dry run)
- Static nav resolver: `scripts/auto_resolve_nav.py` (writes `nav_resolution.json` beside nav script)
- Live coord filler: `scripts/auto_fill_nav_script.py` (rewrites TODO blocks in nav script)
