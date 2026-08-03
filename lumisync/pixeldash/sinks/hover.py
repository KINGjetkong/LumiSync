"""The hover HUD — the dashboard floating over whatever you're actually doing.

A small always-on-top window, pinned to a screen corner, click-through by
default so it never intercepts a mouse event meant for the chart underneath.
This is the surface you glance at mid-trade; the ghost display is the one you
capture or mirror.

Click-through is the setting most worth understanding: with it on, the HUD
cannot be moved or dismissed by mouse, only through the app. That is the right
default for something parked over a trading platform, and it is one flag away
from being draggable when it is in the way.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional, Tuple

from ..models import DEFAULT_SCREEN_TARGET
from ..render.pipeline import RenderResult
from .base import Sink, SinkReport
from .surface import build_surface_class, find_screen, qt_available


class Corner(str, Enum):
    TOP_LEFT = "top-left"
    TOP_RIGHT = "top-right"
    BOTTOM_LEFT = "bottom-left"
    BOTTOM_RIGHT = "bottom-right"


DEFAULT_MARGIN = 24


class HoverSink(Sink):
    """A floating, always-on-top pixel dashboard."""

    name = "hover"
    #: Screens are not panels — render this one on the denser grid.
    target = DEFAULT_SCREEN_TARGET

    def __init__(
        self,
        *,
        scale: int = 6,
        corner: Corner = Corner.TOP_RIGHT,
        opacity: float = 0.9,
        margin: int = DEFAULT_MARGIN,
        click_through: bool = True,
        screen_hint: str = "",
        parent=None,
    ) -> None:
        self.scale = max(1, int(scale))
        self.corner = Corner(corner)
        self.opacity = min(1.0, max(0.15, float(opacity)))
        self.margin = max(0, int(margin))
        self.click_through = click_through
        self.screen_hint = screen_hint
        self._parent = parent
        self._window = None
        self._surface = None
        self._size: Optional[Tuple[int, int]] = None

    # --- window ---
    def _ensure_window(self, cols: int, rows: int):
        size = (cols * self.scale, rows * self.scale)
        if self._window is not None and self._size == size:
            return self._window
        if self._window is not None:
            # The panel geometry changed under us; rebuild rather than resize so
            # the corner anchoring stays correct.
            self.close()

        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QVBoxLayout, QWidget

        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        if self.click_through:
            flags |= Qt.WindowType.WindowTransparentForInput

        window = QWidget(self._parent)
        window.setWindowTitle("Pixel Dash")
        window.setWindowFlags(flags)
        window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        if self.click_through:
            window.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        window.setWindowOpacity(self.opacity)
        window.setStyleSheet("background: #000;")

        layout = QVBoxLayout(window)
        layout.setContentsMargins(0, 0, 0, 0)
        surface = build_surface_class()(window)
        layout.addWidget(surface)

        window.resize(*size)
        self._window = window
        self._surface = surface
        self._size = size
        self._reposition()
        window.show()
        window.raise_()
        return window

    def _reposition(self) -> None:
        if self._window is None or self._size is None:
            return
        screen = find_screen(prefer_virtual=False, name_hint=self.screen_hint)
        if screen is None:
            return

        area = screen.availableGeometry()
        width, height = self._size
        left = area.left() + self.margin
        top = area.top() + self.margin
        right = area.right() - width - self.margin + 1
        bottom = area.bottom() - height - self.margin + 1

        placement = {
            Corner.TOP_LEFT: (left, top),
            Corner.TOP_RIGHT: (right, top),
            Corner.BOTTOM_LEFT: (left, bottom),
            Corner.BOTTOM_RIGHT: (right, bottom),
        }[self.corner]
        self._window.move(*placement)

    # --- sink ---
    def publish(self, result: RenderResult) -> SinkReport:
        if not qt_available():
            return SinkReport.failure(self.name, "PySide6 is not available")

        from PySide6.QtWidgets import QApplication

        if QApplication.instance() is None:
            return SinkReport.failure(self.name, "no running Qt application")

        first = result.frames[0]
        window = self._ensure_window(first.cols, first.rows)
        if window is None:
            return SinkReport.failure(self.name, "could not create the hover window")

        self._surface.set_frames(result.frames, result.frame_ms)
        window.raise_()
        return SinkReport(
            sink=self.name,
            ok=True,
            detail=f"{first.cols}x{first.rows} at {self.scale}x, {self.corner.value}",
        )

    # --- runtime controls ---
    def set_corner(self, corner: Corner) -> None:
        self.corner = Corner(corner)
        self._reposition()

    def set_opacity(self, opacity: float) -> None:
        self.opacity = min(1.0, max(0.15, float(opacity)))
        if self._window is not None:
            self._window.setWindowOpacity(self.opacity)

    def set_visible(self, visible: bool) -> None:
        if self._window is not None:
            self._window.setVisible(bool(visible))

    def close(self) -> None:
        window, self._window = self._window, None
        self._surface = None
        self._size = None
        if window is not None:
            window.close()
            window.deleteLater()
