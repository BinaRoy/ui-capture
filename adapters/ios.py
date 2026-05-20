"""
iOS adapter — stub.

Implement against `xcrun simctl` for screenshots and lldb / view-hierarchy export for
the structural dump. The interface (base.Adapter) is stable; implementing the bodies
below is enough to enable iOS capture without changes elsewhere.
"""

from __future__ import annotations

from pathlib import Path

from .base import Adapter, AdapterError, CaptureResult, PageHint, PageIdentity


_NOT_IMPL = (
    "iOS adapter is not implemented yet. To add iOS support, implement adapters/ios.py "
    "against xcrun simctl (screenshots) and lldb view-hierarchy dumps (structure). "
    "The base.Adapter interface is stable — no orchestrator changes needed."
)


class IOSAdapter(Adapter):
    platform = "ios"

    def check_infrastructure(self) -> tuple[bool, str]:
        return False, _NOT_IMPL

    def discover_hints(self, source_root: Path) -> list[PageHint]:
        return []

    def render_nav_script(self, hints: list[PageHint], out_path: Path) -> None:
        if out_path.exists():
            return
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            "# iOS nav script — not yet supported.\n"
            "# See adapters/ios.py for implementation notes.\n",
            encoding="utf-8",
        )

    def probe_identity(self) -> PageIdentity:
        raise AdapterError(_NOT_IMPL)

    def capture(self, page_id: str, out_dir: Path) -> CaptureResult:
        return CaptureResult(
            status="failed",
            identity=None,
            raw_path=None,
            screenshot_path=None,
            hierarchy=None,
            error=_NOT_IMPL,
        )

    def normalize(self, raw_path: Path) -> dict:
        raise AdapterError(_NOT_IMPL)
