"""The ghost display — the dashboard living on a monitor that isn't there.

Paired with the Virtual Display Driver, this puts a full-screen dashboard on a
headless virtual monitor: it never covers real work, never steals focus, and is
always capturable by OBS, a remote viewer, or a screen-mirroring app pointed at
that display.

The screen is found by EDID identity rather than by index, because plugging in a
real monitor renumbers everything. If no virtual display exists the sink says so
instead of silently taking over the primary screen — a dashboard that
full-screens itself over the trading platform is a bug with consequences.
"""

from __future__ import annotations

from typing import Optional

from ..models import DEFAULT_SCREEN_TARGET
from ..render.pipeline import RenderResult
from .base import Sink, SinkReport
from .surface import build_surface_class, find_virtual_screen, qt_available


class GhostDisplaySink(Sink):
    """Full-screen surface pinned to a virtual (or explicitly chosen) display."""

    name = "ghost"
    #: Screens are not panels — render this one on the denser grid.
    target = DEFAULT_SCREEN_TARGET

    def __init__(
        self,
        *,
        screen_hint: str = "",
        screen_index: Optional[int] = None,
        require_virtual: bool = True,
        parent=None,
    ) -> None:
        self.screen_hint = screen_hint
        self.screen_index = screen_index
        self.require_virtual = require_virtual
        self._parent = parent
        self._window = None
        self._surface = None

    def _ensure_window(self):
        if self._window is not None:
            return self._window

        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QVBoxLayout, QWidget

        screen = self._resolve_screen()
        if screen is None:
            return None

        surface_class = build_surface_class()
        window = QWidget(self._parent)
        window.setWindowTitle("Pixel Dash — Ghost")
        window.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            # The ghost never takes focus: it lives on a display nobody clicks.
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        window.setStyleSheet("background: #000;")

        layout = QVBoxLayout(window)
        layout.setContentsMargins(0, 0, 0, 0)
        surface = surface_class(window)
        layout.addWidget(surface)

        window.setScreen(screen)
        window.setGeometry(screen.geometry())
        window.showFullScreen()

        self._window = window
        self._surface = surface
        return window

    def _resolve_screen(self):
        from .surface import find_screen

        if self.screen_index is not None or self.screen_hint:
            return find_screen(
                prefer_virtual=True,
                name_hint=self.screen_hint,
                index=self.screen_index,
            )
        screen = find_virtual_screen()
        if screen is None and not self.require_virtual:
            return find_screen(prefer_virtual=False)
        return screen

    def publish(self, result: RenderResult) -> SinkReport:
        if not qt_available():
            return SinkReport.failure(self.name, "PySide6 is not available")

        from PySide6.QtWidgets import QApplication

        if QApplication.instance() is None:
            return SinkReport.failure(self.name, "no running Qt application")

        window = self._ensure_window()
        if window is None:
            return SinkReport.failure(
                self.name,
                "no virtual display found — install the Virtual Display Driver "
                "or point the ghost at a screen explicitly",
            )

        self._surface.set_frames(result.frames, result.frame_ms)
        screen = window.screen()
        label = screen.name() if screen else "unknown"
        return SinkReport(
            sink=self.name,
            ok=True,
            detail=f"{result.frame_count} frames on {label}",
        )

    def close(self) -> None:
        window, self._window = self._window, None
        self._surface = None
        if window is not None:
            window.close()
            window.deleteLater()
