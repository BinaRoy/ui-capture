"""
Adapter interface for UI capture.

Each platform (Android, iOS) implements this contract. The orchestrator does not know
platform specifics — it only calls these methods.

Identity model: page identity is determined at runtime. probe_identity() returns the
top class name the platform reports for the current screen. That class name, mapped
back to a source file, is the authoritative page ID. Hints from discover_hints() are
only used to seed the nav script.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


class AdapterError(RuntimeError):
    """Raised when adapter setup or runtime fails in a way the orchestrator needs to know."""


@dataclass
class PageHint:
    """A best-effort static guess at a page that might exist in the input project."""
    # Stable slug for the page (used to scaffold nav scripts). Derived from source file name.
    slug: str
    # Source file the hint came from, relative to source.root if possible.
    source_file: str
    # Discovered class / type name (e.g. "MainSettingsFragment", "TodayViewController").
    class_name: str
    # Where the hint came from: "navgraph", "fragment_tx", "storyboard", "subclass_scan", etc.
    origin: str
    # Free-form notes (e.g. "found in res/navigation/main_nav.xml").
    note: str = ""


@dataclass
class PageIdentity:
    """Runtime-observed identity of the current page."""
    # Top-level component class name (Activity for Android, UIViewController for iOS).
    top_component: str
    # Sub-component currently shown (e.g. Fragment class on Android), may be None.
    current_fragment: Optional[str] = None
    # Source file resolved from class name, if we can map it.
    source_file: Optional[str] = None
    # Platform-specific extras (e.g. package name, navigation stack depth).
    extra: dict = field(default_factory=dict)

    def page_class(self) -> str:
        """The class that best represents 'what page is this'.

        For Android with Fragments, prefer the Fragment. For Activity-per-screen apps
        or iOS, the top_component is the page.
        """
        return self.current_fragment or self.top_component


@dataclass
class CaptureResult:
    """Outcome of a single page capture."""
    status: str                       # "captured" | "failed" | "identity_mismatch"
    identity: Optional[PageIdentity]  # what the platform actually showed
    raw_path: Optional[Path]          # raw platform dump on disk (xml/plist)
    screenshot_path: Optional[Path]   # PNG on disk — viewport (what user first sees)
    hierarchy: Optional[dict]         # normalized UI-IR (dict, not yet serialized)
    error: Optional[str] = None
    duration_ms: Optional[int] = None
    # For pages with scrollable content extending past the viewport. None if the
    # page fits in one screen. The fullpage variant carries visual fold-line
    # annotations so consumers cannot misread it as a single non-scrollable view.
    fullpage_screenshot_path: Optional[Path] = None


class Adapter(ABC):
    """Platform-specific UI capture adapter."""

    #: Short platform name, e.g. "android" / "ios". Used in output paths.
    platform: str = ""

    @abstractmethod
    def check_infrastructure(self) -> tuple[bool, str]:
        """Return (ok, message). On False, capture phase will be skipped with a clear reason."""

    @abstractmethod
    def discover_hints(self, source_root: Path) -> list[PageHint]:
        """Static scan of source_root for page candidates. Best-effort; partial results are fine.

        Implementations should layer multiple discovery strategies (e.g. NavGraph XML,
        then Fragment subclass scan, then FragmentTransaction grep) and union the results.
        Duplicates by class_name should be deduped.
        """

    @abstractmethod
    def render_nav_script(self, hints: list[PageHint], out_path: Path) -> None:
        """Write a starter nav script to out_path. Must not overwrite an existing file."""

    @abstractmethod
    def probe_identity(self) -> PageIdentity:
        """Ask the running app what page is currently on screen. Called right before capture."""

    @abstractmethod
    def capture(self, page_id: str, out_dir: Path) -> CaptureResult:
        """Dump raw hierarchy + screenshot for the page currently on screen.

        out_dir is the per-page directory; caller has already created it. Adapter should:
          1. screenshot → out_dir/screenshot.png
          2. raw dump → out_dir/raw.xml (or raw.plist)
          3. normalize raw → return CaptureResult.hierarchy (dict)
          4. probe identity and attach to result

        If anything fails partway, return CaptureResult(status="failed", error=...) with
        whatever was successfully captured.
        """

    @abstractmethod
    def normalize(self, raw_path: Path) -> dict:
        """Convert raw platform dump to UI-IR. Exposed separately so it can run on fixtures."""

    def extract_resources(
        self,
        source_file: Optional[str],
        source_root: Path,
        out_dir: Path,
    ) -> Optional[dict]:
        """Find and copy source-side resources (images, layouts, strings) referenced
        by the page whose code lives at `source_file`. Returns the resources_manifest
        dict, or None when no resources could be resolved.

        Default implementation: no-op. Override in platform adapter.
        Output layout convention (so consumers can rely on it):
            out_dir/resources/
                drawables/        (PNG / WebP / vector XML)
                mipmaps/          (icons)
                layouts/          (the page's layout XML files, verbatim)
                strings.txt       (key=value lines, one per referenced string resource)
                resources_manifest.json
        """
        return None
