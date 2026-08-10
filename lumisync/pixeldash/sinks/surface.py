"""Shared Qt widget for showing a rendered dashboard on screen.

Both the ghost display and the hover HUD are the same thing pointed at
different places: a frameless window that loops a list of frames with hard
nearest-neighbour scaling. Everything they do not share — placement, input
transparency, always-on-top — lives in their own modules.

Qt is imported inside the functions so the headless CLI and the feed/render
tests never need a display.
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from ..render.canvas import Frame

#: Identifiers the Virtual Display Driver puts on its monitors. ``Virtual
#: Display`` and ``VDD001`` come from the driver's EDID defaults; ``VDD by MTT``
#: and ``MTT1337`` are the names its own PowerShell helpers match on. Finding
#: the ghost by identity rather than by screen index matters because indices
#: shift the moment a real monitor is plugged in or woken up.
VDD_MODEL_HINTS = (
    "virtual display",
    "virtualdisplaydriver",
    "vdd by mtt",
    "mttvdd",
    "mtt1337",
)
VDD_SERIAL_HINTS = ("vdd001",)


def qt_available() -> bool:
    try:
        import PySide6.QtWidgets  # noqa: F401

        return True
    except Exception:
        return False


def frame_to_image(frame: Frame):
    """Convert a frame to a QImage that owns its own pixel buffer.

    The copy is deliberate: ``QImage`` does not take ownership of a foreign
    buffer, and a numpy array freed while Qt still holds a pointer to it is a
    crash that only shows up under memory pressure.
    """
    from PySide6.QtGui import QImage

    payload = frame.data.tobytes()
    image = QImage(payload, frame.cols, frame.rows, frame.cols * 3, QImage.Format.Format_RGB888)
    return image.copy()


def find_screen(
    *,
    prefer_virtual: bool = True,
    name_hint: str = "",
    index: Optional[int] = None,
):
    """Pick the screen to show a surface on.

    Resolution order: an explicit index, then a name/model substring, then a
    Virtual Display Driver monitor, then the primary screen. Returns ``None``
    only when Qt reports no screens at all.
    """
    from PySide6.QtGui import QGuiApplication

    screens = QGuiApplication.screens()
    if not screens:
        return None

    if index is not None and 0 <= index < len(screens):
        return screens[index]

    if name_hint:
        needle = name_hint.strip().lower()
        for screen in screens:
            haystack = " ".join(
                part.lower()
                for part in (screen.name(), screen.model(), screen.manufacturer())
                if part
            )
            if needle in haystack:
                return screen

    if prefer_virtual:
        virtual = find_virtual_screen(screens)
        if virtual is not None:
            return virtual

    return QGuiApplication.primaryScreen() or screens[0]


def find_virtual_screen(screens: Optional[Sequence] = None):
    """Locate a Virtual Display Driver monitor by its EDID identity."""
    from PySide6.QtGui import QGuiApplication

    screens = screens if screens is not None else QGuiApplication.screens()
    for screen in screens:
        model = (screen.model() or "").strip().lower()
        serial = (screen.serialNumber() or "").strip().lower()
        name = (screen.name() or "").strip().lower()
        if any(hint in model or hint in name for hint in VDD_MODEL_HINTS):
            return screen
        if any(hint in serial for hint in VDD_SERIAL_HINTS):
            return screen
    return None


def describe_screens() -> List[str]:
    """Human-readable screen list, for the CLI and the settings page."""
    from PySide6.QtGui import QGuiApplication

    lines = []
    for index, screen in enumerate(QGuiApplication.screens()):
        geometry = screen.geometry()
        parts = [f"[{index}] {screen.name()}", f"{geometry.width()}x{geometry.height()}"]
        if screen.model():
            parts.append(screen.model())
        lines.append(" · ".join(parts))
    return lines


def build_surface_class():
    """Define the surface widget lazily so importing this module is Qt-free."""
    from PySide6.QtCore import QRect, Qt, QTimer
    from PySide6.QtGui import QPainter
    from PySide6.QtWidgets import QWidget

    class PixelSurface(QWidget):
        """Loops a frame list, painted with hard pixel edges."""

        def __init__(self, parent=None) -> None:
            super().__init__(parent)
            self._images: List = []
            self._durations: List[int] = []
            self._index = 0
            self._timer = QTimer(self)
            self._timer.setSingleShot(True)
            self._timer.timeout.connect(self._advance)
            self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)

        def set_frames(self, frames: Sequence[Frame], frame_ms: int) -> None:
            """Replace the animation. Restarts from the first frame."""
            from .matrix import dedupe

            pairs = dedupe(frames)
            self._images = [frame_to_image(frame) for frame, _repeats in pairs]
            self._durations = [max(20, repeats * int(frame_ms)) for _frame, repeats in pairs]
            self._index = 0
            self._timer.stop()
            if self._images:
                self.update()
                if len(self._images) > 1:
                    self._timer.start(self._durations[0])

        def clear(self) -> None:
            self._timer.stop()
            self._images = []
            self._durations = []
            self.update()

        def _advance(self) -> None:
            if not self._images:
                return
            self._index = (self._index + 1) % len(self._images)
            self.update()
            self._timer.start(self._durations[self._index])

        def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
            painter = QPainter(self)
            painter.fillRect(self.rect(), Qt.GlobalColor.black)
            if not self._images:
                return

            image = self._images[self._index]
            # Integer scaling keeps every LED square. Only when the window is
            # smaller than the source do we fall back to a smooth fit.
            scale = min(self.width() // image.width(), self.height() // image.height())
            if scale >= 1:
                width, height = image.width() * scale, image.height() * scale
            else:
                width = image.width() * self.height() // image.height()
                height = self.height()
                if width > self.width():
                    width, height = self.width(), image.height() * self.width() // image.width()

            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
            painter.drawImage(
                QRect(
                    (self.width() - width) // 2,
                    (self.height() - height) // 2,
                    max(1, width),
                    max(1, height),
                ),
                image,
            )

    return PixelSurface
