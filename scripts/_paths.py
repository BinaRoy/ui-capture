"""
Resolve workflow paths for ui-capture scripts.

Delegates to pandora-core's canonical paths.py so behaviour stays consistent with
the rest of the workflow (config discovery, env override, JSON5 quirks, etc.).
"""

from __future__ import annotations

import sys
from pathlib import Path

# This file lives at .claude/skills/ui-capture/scripts/_paths.py
# parents: [0]=scripts, [1]=ui-capture, [2]=skills, [3]=.claude, [4]=repo root
_SKILL_ROOT = Path(__file__).resolve().parents[1]
_CORE_ROOT = Path(__file__).resolve().parents[4]
_CLAUDE_LIB = _CORE_ROOT / ".claude" / "scripts" / "lib"
_CORE_SRC = _CORE_ROOT / "src"

# Post-restructure layout: canonical paths.py lives at .claude/scripts/lib/paths.py.
# Legacy src/feature_develop/tools/paths.py kept as a fallback.
# Also search ~/.claude/scripts/lib for standalone/macOS setups where parents[4] != $HOME.
_HOME_CLAUDE_LIB = Path.home() / ".claude" / "scripts" / "lib"
for _lib in (_CLAUDE_LIB, _HOME_CLAUDE_LIB):
    if _lib.is_dir() and str(_lib) not in sys.path:
        sys.path.insert(0, str(_lib))
if _CORE_SRC.is_dir() and str(_CORE_SRC) not in sys.path:
    sys.path.insert(0, str(_CORE_SRC))

try:
    import paths as core_paths  # noqa: E402  (post-restructure layout)
except ImportError:
    from feature_develop.tools import paths as core_paths  # noqa: E402  (legacy)

# Re-export the bits we need
SOURCE_ROOT = core_paths.SOURCE_ROOT
TARGET_PROJECT_ROOT = core_paths.TARGET_PROJECT_ROOT
WORKFLOW_ROOT = core_paths.WORKFLOW_ROOT
ADAPTER_NAME = core_paths.ADAPTER_NAME
FEATURE_JSON = core_paths.FEATURE_JSON
CORE_ROOT = core_paths.CORE_ROOT
CONFIG_PATH = core_paths.CONFIG_PATH

SKILL_ROOT = _SKILL_ROOT
UI_ROOT = WORKFLOW_ROOT / "ui"
UI_PAGES_DIR = UI_ROOT / "pages"
UI_MANIFEST = UI_ROOT / "ui_manifest.json"
UI_REPORT = UI_ROOT / "report.md"
UI_NAV_DIR = UI_ROOT / "nav_script"


def ensure_ui_dirs() -> None:
    UI_ROOT.mkdir(parents=True, exist_ok=True)
    UI_PAGES_DIR.mkdir(parents=True, exist_ok=True)
    UI_NAV_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Cross-platform diff output path.
#
# Rule: per-adapter workflow output stays in its own root; cross-platform diff
# products land in an INDEPENDENT root so neither end gets polluted.
#
# Layout:
#   <captures_parent>/
#     ├── output_android/     ← Android workflow root (legacy: output/)
#     │   └── workflow_output/ui/...
#     ├── output_harmony/     ← HarmonyOS workflow root
#     │   └── workflow_output/ui/...
#     └── output_cross/       ← ⭐ this module derives + auto-creates
#         └── <source_adapter>_vs_<target_adapter>/
#             └── <slug>/
#                 ├── compare.html
#                 ├── diff.json
#                 ├── diff.md
#                 └── meta.json
#
# Derivation walks up from this skill's workflow root:
#   WORKFLOW_ROOT          .../output_android/workflow_output
#   captures_parent        .../             (two levels up)
#   CROSS_OUTPUT_ROOT      <captures_parent>/output_cross/<pair>/
#
# Override the parent via config:
#   workflow.config.json → { "cross_output_root": "/abs/or/relative/path" }
# Override the pair name via config or argument to derive_cross_pair_dir().
# ---------------------------------------------------------------------------

_CROSS_DIR_NAME = "output_cross"

# Re-parse the config to access optional fields (captures_parent / cross_output_root)
# that the home shim doesn't surface.
import json as _json
_extra_cfg = _json.loads(Path(CONFIG_PATH).read_text(encoding="utf-8"))

_captures_parent_raw = _extra_cfg.get("captures_parent")
if _captures_parent_raw:
    _cp = Path(_captures_parent_raw).expanduser()
    CAPTURES_PARENT = _cp.resolve() if _cp.is_absolute() else (CONFIG_PATH.parent / _cp).resolve()
else:
    # WORKFLOW_ROOT = <captures_parent>/<output_xxx>/workflow_output → two up.
    CAPTURES_PARENT = WORKFLOW_ROOT.parent.parent

_cross_root_raw = _extra_cfg.get("cross_output_root")
if _cross_root_raw:
    _cr = Path(_cross_root_raw).expanduser()
    CROSS_OUTPUT_ROOT = _cr.resolve() if _cr.is_absolute() else (CONFIG_PATH.parent / _cr).resolve()
else:
    CROSS_OUTPUT_ROOT = CAPTURES_PARENT / _CROSS_DIR_NAME


def derive_cross_pair_dir(target_adapter: str,
                          source_adapter: str = ADAPTER_NAME,
                          *, ensure: bool = True) -> Path:
    """Return <CROSS_OUTPUT_ROOT>/<source>_vs_<target>/, auto-creating it.

    `source_adapter` defaults to whatever this workflow is configured as;
    callers typically pass only `target_adapter`.
    """
    pair = f"{_short_adapter(source_adapter)}_vs_{_short_adapter(target_adapter)}"
    out = CROSS_OUTPUT_ROOT / pair
    if ensure:
        out.mkdir(parents=True, exist_ok=True)
    return out


def derive_cross_page_dir(slug: str, target_adapter: str,
                          source_adapter: str = ADAPTER_NAME,
                          *, ensure: bool = True) -> Path:
    """Return <pair_dir>/<slug>/, auto-creating it. This is where compare.py
    writes its cross-platform diff artifacts."""
    pair_dir = derive_cross_pair_dir(target_adapter, source_adapter, ensure=ensure)
    out = pair_dir / slug
    if ensure:
        out.mkdir(parents=True, exist_ok=True)
    return out


def _short_adapter(name: str) -> str:
    """generic_android → android; generic_harmony → harmony; else: name verbatim."""
    if name.startswith("generic_"):
        return name[len("generic_"):]
    return name


def workflow_root_for(adapter_name: str) -> Path:
    """Convention-based lookup: <captures_parent>/output_<short>/workflow_output.

    Used by cross-platform tools that need to read another adapter's captures
    without the caller having to know absolute paths.
    """
    short = _short_adapter(adapter_name)
    return CAPTURES_PARENT / f"output_{short}" / "workflow_output"
