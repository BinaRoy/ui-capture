# ui-capture 架构参考

Skill 的执行流程、产物文件含义、双端适配器差异、已知技术问题。

- 环境配置：[`INSTALL_macos.md`](INSTALL_macos.md)
- 公开入口：[`../README.md`](../README.md)
- Skill 契约：[`../SKILL.md`](../SKILL.md)

---

## Adapter 矩阵

| Adapter | 入口 | 设备控制 | 抓取层信号 | 滚动拼接 | 资源抽取 | 状态 |
|---|---|---|---|---|---|---|
| `generic_android` | `adapters/android.py` | `adb` | `uiautomator dump` (XML) + `screencap` | ✅ anchor-based | ✅ R.drawable / R.string / layout XML | 稳定 |
| `generic_harmony` | `adapters/harmony.py` | `hdc` | `uitest dumpLayout` (JSON) + `snapshot_display` | ✅ anchor-based + scroll-back-to-top | ❌ no-op stub | 功能完备 |
| `generic_ios` | `adapters/ios.py` | — | — | — | — | ❌ 56 行 stub |

两侧 adapter 都遵守 `adapters/base.py::Adapter` 接口，emit 的 `hierarchy.json` 共用同一份 `schema/hierarchy.schema.json`。

---

## 执行流程

`scripts/run.py` 编排，6 个阶段：

```
Phase 1   discover_hints       静态扫源，列页清单
Phase 2   scaffold             首跑写 nav script 脚手架（不覆盖人工版本）
Phase 2b  auto_resolve         静态解析 trigger（Android view_id / Cangjie pushPathByName）
Phase 2.5 manual_discovery     扫人工放置的截图（兜底）
Phase 3   capture              跑 nav script，逐页触发 adapter.capture()
Phase 4   finalize             feature 映射 → ui_manifest.json + report.md
```

跨端 diff 是**独立命令**，不在 run.py 阶段链上：

```
scripts/compare.py --page <slug> --target-workflow <adapter|path> --dump-diff
```

---

### Phase 1：discover_hints（静态发现）

不需要 emulator 或 adb/hdc。

**Android（`adapters/android.py`）扫描：**
- `res/navigation/*.xml`（NavGraph destinations）
- `AndroidManifest.xml`（Activity 声明）
- `.java` / `.kt` 中继承 `Fragment` 的子类

**HarmonyOS（`adapters/harmony.py`）扫描三层信号：**
1. **`module.json5` abilities** → launcher 类（origin=`module_json`）
2. **`@Entry` 装饰的 Cangjie 类** → 路由根（origin=`entry_annotation`）
3. **`@Builder pageMap` 中 `if (name == "X")` 链** → NavDestination 页（origin=`nav_destination`）

HarmonyOS 还做**反查 + 边界标签**：
- 页名出现在 pageMap 但工程内**无 `pushPathByName` 调用** → 标 `orphan`
- *Page 后缀类被某 `@Component` 直接内联实例化（条件渲染）→ 标 `conditional_render`，挂到父页

实测：cj_telegram 56 hint（48 nav_destination / 6 conditional_render / 1 entry / 1 module_json）。

> Best-effort：Compose / SwiftUI / 自定义导航的页可能漏，nav script 阶段补。

---

### Phase 2：scaffold（生成 nav script 模板）

只在 `nav_script_<adapter>.sh` 不存在时生成。模板：

| Adapter | 文件 | 关键差异 |
|---|---|---|
| Android | `templates/nav_script_android.sh` → `<UI_ROOT>/nav_script/nav_script_android.sh` | `am force-stop` + `am start -n PKG/ACT` |
| HarmonyOS | `templates/nav_script_harmony.sh` → `<UI_ROOT>/nav_script/nav_script_harmony.sh` | `aa force-stop` + `aa start -a ABILITY -b BUNDLE`（2026-05-19 起强制冷启动） |

模板列出每个 hint 作为 TODO 注释，由人/agent 填 tap 坐标。launcher（第一个 hint）默认直接 `capture_page`。

---

### Phase 2b：auto_resolve（静态触发解析）

`scripts/auto_resolve_nav.py`，按 manifest.adapter 分发 hint 集合，跑 5 个策略：

| 策略 | 平台 | 信号源 |
|---|---|---|
| `fragment_instantiate` | Android | `FragmentX().show(...)` / `replace(R.id.X, FragmentX())` |
| `navgraph_action` | Android | `findNavController().navigate(R.id.action_X)` 反查 NavGraph |
| `compose_clickable` | Android | `Modifier.clickable { navController.navigate("route") }` |
| `cangjie_router` | HarmonyOS | `pageStack.pushPathByName("X")` + 回溯 800 字符抽 `Text("...")` / `Button("...")` 字面量做 `trigger_label` |
| `conditional_render` | HarmonyOS | hint.origin=conditional_render 时挂父页文件，nav 阶段走"先抓父页再翻 state" |

输出 `nav_script/nav_resolution.json`：每页 `{trigger_file, trigger_method, trigger_label, view_id, via, test_tag}`。

`_enclosing_method` 支持 Java / Kotlin / Cangjie（`func`）三种语法。

---

### Phase 3：capture（核心捕获）

`run.py` 创建一个 FIFO，启动 nav script。nav script 每次调 `capture_page <id>`：
1. 把 page_id 写入 FIFO，阻塞
2. orchestrator 读 id → 调 `adapter.capture(id, out_dir)`
3. 完成后写 ack 文件，nav script 继续

这套把"导航（bash，项目相关）"和"捕获（Python，平台相关）"完全解耦。

#### `adapter.capture()` 内部步骤

```
ⓐ _wait_until_stable()      # 反复 dumpLayout → md5 → 连续 N 次相同算稳定
ⓑ probe_identity()          # 抽 ability/Activity/Fragment 名
ⓒ _snapshot_display()       # 写设备 tmp → file recv → screenshot.png
ⓓ _dump_layout_text()       # 再 dump 一次 → raw.json/xml
ⓔ normalize()               # 平台原始格式 → UI-IR
ⓕ _has_scrollable() ?       # 若 kind ∈ {scroll, list} 且 state.scrollable
   → _capture_fullpage()    # 锚点拼接 + 截图拼接 + 滚回顶（关键修复见下文）
```

#### Android vs HarmonyOS：步骤差异

| 步骤 | Android | HarmonyOS |
|---|---|---|
| 截图 | `adb exec-out screencap -p` → stdout PNG | `hdc shell snapshot_display -f /data/local/tmp/X.jpeg`（仅接 `.jpeg` 扩展名）+ `hdc file recv` |
| Layout dump | `adb shell uiautomator dump` → XML | `hdc shell uitest dumpLayout` → JSON 文件 + recv |
| identity | `dumpsys activity activities` + FragmentManager | `top_component`=ability 名（NavPathStack 不切 ability） |
| 资源 | `R.layout/drawable/string` 扫源码 + 复制 | no-op（Cangjie 资源体系不同，未实现） |

#### 滚动拼接：anchor-based 算法

两侧共用骨架：

1. step 0 = 视口首图 + 首次 dump
2. 循环（最多 SCROLL_MAX_STEPS = 8/10 次）：
   - swipe 顶 80% → 底 20%（把内容上推）
   - settle 0.7s ~ 0.8s
   - 重 dump + 截图
   - 用 `(kind, text)` 锚点节点，取**中位数 y-delta**（排除动画噪声）作累计 offset
   - delta < 阈值 / 锚点全丢 / 高 overlap 触底 → break
3. 拼接：
   - **截图**：Pillow 按 offset 上下拼，每个 fold 画虚线
   - **节点**：deepcopy base，每 step 把"新增子树"的 bounds y 加 cumulative offset，挂到最深 scrollable 容器
4. **HarmonyOS 额外做（2026-05-19 修复）**：拼接完反向 swipe (scroll_steps+1 次) 把屏幕回顶。否则下一次 nav-script tap 会落到滚动后的位置，settings/search 类页就被错位抓成"weather body"。

`_has_scrollable()` 限定 `kind ∈ {scroll, list}` —— Swiper（pager）虽然 `state.scrollable=true`，但是横向，不能误触发垂直 swipe。

#### 冷启动（HarmonyOS 强制）

Cangjie/ArkUI 用 NavPathStack 推栈。`aa start` **不会**重置栈，热启动会保留栈顶的 NavDestination。**残留状态导致后续 tap 坐标全部失效**（用 launcher 的坐标点不到新栈顶的元素）。

修复：nav script 必须 `aa force-stop` 前置（`templates/nav_script_harmony.sh` + `adapters/harmony.py::render_nav_script` + `scripts/generate_nav_script_harmony.py` 三处都已落实）。

---

### Phase 4：finalize

- 对每个 captured 页用 `class_name` 在源码反查 `.java` / `.cj` 路径
- 抽取资源（Android 才有意义）
- 如有 `feature.json`，把页面挂到 feature DAG（无则跳过，标 `feature: null`）
- 写 `ui_manifest.json` 汇总 + `report.md`

---

## nav 自动化（HarmonyOS 用，Android 暂仍人工）

3 个独立脚本，组合起来把 resolution.json → nav_plan.json → 可跑的 sh：

| 脚本 | 输入 | 输出 |
|---|---|---|
| `scripts/auto_resolve_nav.py` | manifest.json + 源码 | `nav_script/nav_resolution.json`（每页 trigger 信息） |
| `scripts/nav_graph_harmony.py` | resolution.json + 已抓 hierarchies | `nav_plan.json`（每页 `{status, parent, tap_x?, tap_y?, command?}`），按 4 档文本匹配（exact / NFKC / emoji-stripped / 受限子串）查 bounds |
| `scripts/generate_nav_script_harmony.py` | nav_plan.json | `nav_script_harmony.sh`（带 `aa force-stop` + 对 ready 页输出 `click + capture + back` 三连） |

三者**目前没缝在 run.py 里**（已记入 ROADMAP B.0.6 + Q3 缺口）。Cangjie 项目可手动跑这条链；Android 项目仍走人工 nav 脚本。

---

## 每一类输出文件

以 `weather_main` 为例，完整输出在 `pages/weather_main/` 下。

### `screenshot.png`
视口首屏。Android = `adb exec-out screencap -p` stdout；HarmonyOS = `hdc shell snapshot_display`。

### `screenshot_fullpage.png`（仅可滚动页）
拼接成品，每个 scroll fold 画虚线 + 文字标注。

### `raw.xml` (Android) / `raw.json` (HarmonyOS)
adapter 原始 dump。provenance / 调试用。

### `hierarchy.json`
归一化后的 UI-IR，schema 见 `schema/hierarchy.schema.json`。**这是 diff 的核心数据**。例：

```json
{
  "kind": "root",
  "platform": "harmony",
  "scrolled": true,
  "viewport_height": 2756,
  "page_total_height": 3076,
  "scroll_steps": 1,
  "children": [
    { "kind": "text", "class": "Text", "text": "Chinatown",
      "bounds": [54, 198, 940, 277] }
  ]
}
```

### `view.html` / `overlay.svg` / `hierarchy.md`
人 review 用。`render_html.py` 生成 view.html（左结构投影 / 中截图 / 右层级树）；`overlay.svg` 是 bounds 叠加；`hierarchy.md` 是缩进列表。

### `meta.json`
每页元数据（id、status、duration_ms、identity、screenshot/scroll 字段、feature 映射等）。`ui_manifest.json` 的数据源。

### `resources/`（Android only）
静态分析 Java/Kotlin 抽 `R.layout/drawable/string/color/array`，layout XML 内引用的 drawable 一并复制（非递归，selector 内部二级引用标 `has_inner_refs: true`，需人工跟进）。weather_main 共 90 项。

---

## ui_manifest.json（汇总）

```json
{
  "schema_version": 1,
  "adapter": "generic_harmony",
  "platform": "harmony",
  "status": "ok",
  "phases": { "discover": {...}, "resolve": {...}, "capture": {...} },
  "pages": [ { "id": "weather_main", "status": "captured", "class_name": "...",
               "screenshot": "pages/weather_main/screenshot.png",
               "scroll": {...} } ]
}
```

`status` 枚举：`captured` / `failed` / `captured_manual` / `manual_pending` / `manual_unbound`。

---

## Output 目录约定（B.0.5，2026-05-19 已落地）

```
<captures_parent>/
├── output_android/                    # adapter=generic_android
│   ├── workflow.config.json
│   └── workflow_output/ui/
│       ├── pages/<slug>/              # 单页产物
│       ├── nav_script/                # nav_script.sh + nav_resolution.json + nav_plan.json
│       ├── compare/                   # 单端 review（target 缺时仅 source 一栏）
│       ├── ui_manifest.json
│       └── report.md
│
├── output_harmony/                    # adapter=generic_harmony，layout 同上
│
└── output_cross/                      # 跨端 diff 独立 root
    └── <source>_vs_<target>/          # 例：android_vs_harmony
        └── <slug>/
            ├── compare.html
            ├── diff.json
            ├── diff.md
            └── meta.json
```

### 路径派生（agent 不需要 mkdir）

`scripts/_paths.py` 暴露：

```python
CAPTURES_PARENT     # WORKFLOW_ROOT 向上两级 / 或 config.captures_parent override
CROSS_OUTPUT_ROOT   # <CAPTURES_PARENT>/output_cross/  / 或 config.cross_output_root
derive_cross_pair_dir(target_adapter)        # → output_cross/<source>_vs_<target>/
derive_cross_page_dir(slug, target_adapter)  # 同上加 /<slug>/，自动 mkdir
workflow_root_for(adapter_name)              # 反查另一端 workflow（用于 compare.py）
```

`scripts/compare.py --target-workflow <adapter|abs-path>` 调上述派生：传入 adapter 名（`generic_harmony` / 短名 `harmony`）或绝对路径都行。

### 单页内部约定（`pages/<slug>/`）

| 文件 | 用途 | 谁消费 |
|---|---|---|
| `hierarchy.json` | UI-IR | diff 引擎 / compare.html / render_html |
| `raw.json` / `raw.xml` | adapter 原始 dump | provenance |
| `screenshot.png` | 视口首图 | overlay / compare |
| `screenshot_fullpage.png` | 滚动拼接全图 | overlay / compare |
| `scroll/` | 各步原始图 + dump | 滚动调试 |
| `meta.json` | 抓取元数据 | 全流程 |
| `view.html` / `overlay.svg` / `hierarchy.md` | 单页可视化 | 人 review |
| `resources/` | Android only：strings / drawables | 翻译工作流 |

---

## 已知技术问题

| 问题 | 位置 | 说明 | 状态 |
|---|---|---|---|
| 投影画布高度裁切 | `render_html.py` | `.layout` div 高度未跟随节点最大 bottom 值，>800px 节点不可见 | 已知，未修 |
| RecyclerView / LazyForEach 只有可见项 | uiautomator / uitest 限制 | dump 只输出 viewport 内的渲染节点；滚动捕获可缓解，不能根治 | 工具限制 |
| `auto_resolve` Android 仅识别 setOnClickListener | `auto_resolve_nav.py` | ButterKnife / DataBinding / Compose 兜底未实现 | ROADMAP Task 2.1 |
| `discover_hints` 对 Compose / SwiftUI 失效 | `adapters/android.py` | 静态扫描假设传统 View 体系 | ROADMAP Task 2.2 |
| 跨端 bounds drift 1000%+ | `diff_engine.py` 容差 | Android 1080×2400 vs Harmony 1272×2756 + DPI 不同，绝对像素无法对齐 | ROADMAP B.2 |
| nav 生成器未串到 run.py | run.py phase 链 | `nav_graph_harmony` + `generate_nav_script_harmony` 三步要手动跑 | ROADMAP Q3 缺口 |
| diff 与 capture 强绑定 | `run.py::_run_diff_phase` | 单跑 diff 必须重跑 capture | ROADMAP B.0.6 |
| iOS adapter 是 stub | `adapters/ios.py` | 56 行 placeholder | ROADMAP B.4 |
| Cangjie 被测项目部分页面渲染为空 / 含 weather body | 不在 skill 内 | 被测项目自身 bug，skill 抓取正确反映了被测状态 | 非 skill 问题 |

---

## 开发时快速参考

| 需要做什么 | 命令 / 路径 |
|---|---|
| 完整环境配置 | [`INSTALL_macos.md`](INSTALL_macos.md) |
| 单端抓取 | `IOS2CJ_WORKFLOW_CONFIG=<config> python3 scripts/run.py --mode standalone` |
| 只重抓一页 | `--pages weather_main` |
| 不重抓只重建 HTML | `--skip-capture` |
| 跨端 diff | `scripts/compare.py --page <slug> --target-workflow generic_harmony --dump-diff` |
| Cangjie 静态发现 | `python3 -c "from adapters.harmony import HarmonyAdapter; print(HarmonyAdapter().discover_hints(...))"` |
| 仅测 normalize + render | `python3 tests/test_dryrun.py`（需 `IOS2CJ_WORKFLOW_CONFIG`） |
| schema 校验 | `python3 tests/test_schema.py` |
| Adapter 实现 | `adapters/{android,harmony}.py` |
| 路径派生 | `scripts/_paths.py` + `~/.claude/scripts/lib/paths.py` shim |
| nav script 模板 | `templates/nav_script_{android,harmony}.sh` |
| 生成可跑 nav（HarmonyOS） | `scripts/{nav_graph_harmony,generate_nav_script_harmony}.py` |
