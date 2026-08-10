"""Pixel Dash view — live preview, output routing and journal notes.

Threading model: the worker thread only polls and renders. Publishing happens
back on the GUI thread, because two of the three sinks are Qt widgets and Qt
widgets may only be touched from the thread that owns them. Keeping the network
call off the GUI thread is what stops a slow broker from freezing the window.
"""

from __future__ import annotations

import datetime as _dt
import os
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QDate, QObject, QThread, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QDateEdit,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ...pixeldash.config import load_config, setup_instructions
from ...pixeldash.journal import Journal, default_journal_path
from ...pixeldash.models import KNOWN_TARGETS
from ...pixeldash.service import PixelDashService, ServiceTick
from ...pixeldash.sinks.base import Sink, group_by_target, publish_all
from ...pixeldash.sinks.files import FileSink
from ...pixeldash.sinks.ghost import GhostDisplaySink
from ...pixeldash.sinks.hover import Corner, HoverSink
from ...pixeldash.sinks.matrix import MatrixSink
from ...pixeldash.sinks.surface import build_surface_class
from ..widgets.product_controls import ProductComboBox


class _DashWorker(QObject):
    """Polls and renders off the GUI thread. Never publishes."""

    ticked = Signal(object)
    stopped = Signal()

    def __init__(self, service: PixelDashService) -> None:
        super().__init__()
        self.service = service
        self._running = False

    def run(self) -> None:
        self._running = True
        try:
            self.service.run_forever(on_tick=self.ticked.emit, stop=self.service.stop_event)
        finally:
            self._running = False
            self.stopped.emit()

    def refresh_once(self) -> None:
        self.ticked.emit(self.service.refresh(publish=False))


class PixelDashView(QWidget):
    """The Pixel Dash page."""

    def __init__(self, device_controller, settings=None) -> None:
        super().__init__()
        self.controller = device_controller
        self.settings = settings

        self.config = load_config()
        self._apply_saved_settings()
        self.journal = Journal(default_journal_path(self.config.output_dir))
        self.service = PixelDashService(self.config, sinks=[], journal=self.journal)

        self._thread: Optional[QThread] = None
        self._worker: Optional[_DashWorker] = None
        self._file_sink = FileSink(self.config.output_dir)
        self._hover_sink: Optional[HoverSink] = None
        self._ghost_sink: Optional[GhostDisplaySink] = None
        self._panel_sink: Optional[MatrixSink] = None
        self._last_tick: Optional[ServiceTick] = None

        # The preview is custom-painted; the nav fade's opacity effect fights it.
        self.setProperty("noFadeEffect", True)
        self._build()
        self._connect_device_signals()
        self._refresh_devices()
        self._refresh_status()

    # --- construction ---
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 28)
        root.setSpacing(16)

        header = QVBoxLayout()
        header.setSpacing(3)
        title = QLabel("Pixel Dash")
        title.setProperty("role", "title")
        header.addWidget(title)
        intro = QLabel(
            "Live trade stats rendered as pixel art, then routed to a panel, "
            "a ghost display and a hover overlay."
        )
        intro.setProperty("role", "pageDescription")
        intro.setWordWrap(True)
        header.addWidget(intro)
        root.addLayout(header)

        workspace = QHBoxLayout()
        workspace.setSpacing(16)
        workspace.addWidget(self._build_controls())
        workspace.addWidget(self._build_preview(), 1)
        root.addLayout(workspace, 1)

        root.addWidget(self._build_journal())

        self.status = QLabel("")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

    def _build_controls(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("DrawToolsPanel")
        panel.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        panel.setFixedWidth(248)

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(9)

        layout.addWidget(_eyebrow("PANEL"))
        self.target_combo = ProductComboBox()
        for name, target in KNOWN_TARGETS.items():
            self.target_combo.addItem(f"{name} · {target.cols}×{target.rows}", name)
        self.target_combo.setCurrentIndex(max(0, self.target_combo.findData(self.config.target.name)))
        self.target_combo.currentIndexChanged.connect(self._on_target_changed)
        layout.addWidget(self.target_combo)

        layout.addSpacing(4)
        layout.addWidget(_eyebrow("OUTPUTS"))

        self.hover_check = QCheckBox("Hover overlay")
        self.hover_check.setChecked(self.config.hover_enabled)
        self.hover_check.toggled.connect(self._on_hover_toggled)
        layout.addWidget(self.hover_check)

        self.corner_combo = ProductComboBox()
        for corner in Corner:
            self.corner_combo.addItem(corner.value.replace("-", " ").title(), corner.value)
        self.corner_combo.setCurrentIndex(max(0, self.corner_combo.findData(Corner.TOP_RIGHT.value)))
        self.corner_combo.currentIndexChanged.connect(self._on_corner_changed)
        layout.addWidget(self.corner_combo)

        scale_row = QHBoxLayout()
        scale_row.addWidget(QLabel("Size"), 1)
        self.scale_slider = QSlider(Qt.Orientation.Horizontal)
        self.scale_slider.setRange(2, 14)
        self.scale_slider.setValue(self.config.hover_scale)
        self.scale_slider.valueChanged.connect(self._on_scale_changed)
        scale_row.addWidget(self.scale_slider, 2)
        layout.addLayout(scale_row)

        self.ghost_check = QCheckBox("Ghost display")
        self.ghost_check.setToolTip(
            "Full-screen the dashboard on a Virtual Display Driver monitor"
        )
        self.ghost_check.setChecked(self.config.ghost_enabled)
        self.ghost_check.toggled.connect(self._on_ghost_toggled)
        layout.addWidget(self.ghost_check)

        layout.addSpacing(4)
        layout.addWidget(_eyebrow("SEND TO DEVICE"))
        self.device_combo = ProductComboBox()
        self.device_combo.currentIndexChanged.connect(self._on_device_changed)
        layout.addWidget(self.device_combo)
        self.device_hint = QLabel("")
        self.device_hint.setProperty("role", "subtle")
        self.device_hint.setWordWrap(True)
        layout.addWidget(self.device_hint)

        layout.addStretch(1)

        self.start_button = QPushButton("Start")
        self.start_button.setProperty("role", "primary")
        self.start_button.clicked.connect(self._toggle_running)
        layout.addWidget(self.start_button)

        self.refresh_button = QPushButton("Refresh Now")
        self.refresh_button.clicked.connect(self._refresh_once)
        layout.addWidget(self.refresh_button)

        self.folder_button = QPushButton("Open Output Folder")
        self.folder_button.clicked.connect(self._open_output)
        layout.addWidget(self.folder_button)
        return panel

    def _build_preview(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("DrawCanvasPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 14, 16, 16)
        layout.setSpacing(10)

        head = QHBoxLayout()
        caption = QLabel("Preview")
        caption.setProperty("role", "strong")
        head.addWidget(caption)
        head.addStretch(1)
        self.meta_label = QLabel("idle")
        self.meta_label.setProperty("role", "subtle")
        head.addWidget(self.meta_label)
        layout.addLayout(head)

        self.surface = build_surface_class()(panel)
        self.surface.setMinimumHeight(220)
        self.surface.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self.surface, 1)

        self.feeds_label = QLabel("No broker configured.")
        self.feeds_label.setProperty("role", "subtle")
        self.feeds_label.setWordWrap(True)
        layout.addWidget(self.feeds_label)
        return panel

    def _build_journal(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("DrawTimelinePanel")
        layout = QHBoxLayout(panel)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        label = QLabel("Journal")
        label.setProperty("role", "strong")
        layout.addWidget(label)

        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDate(QDate.currentDate())
        self.date_edit.dateChanged.connect(self._load_note)
        layout.addWidget(self.date_edit)

        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText("What happened on this date…")
        self.note_edit.returnPressed.connect(self._save_note)
        layout.addWidget(self.note_edit, 1)

        self.save_note_button = QPushButton("Save Note")
        self.save_note_button.clicked.connect(self._save_note)
        layout.addWidget(self.save_note_button)

        self._load_note()
        return panel

    # --- settings ---
    def _apply_saved_settings(self) -> None:
        if self.settings is None:
            return
        target = str(self.settings.value("pixeldash/target", self.config.target.name))
        if target in KNOWN_TARGETS:
            self.config.target = KNOWN_TARGETS[target]
        self.config.hover_enabled = _as_bool(self.settings.value("pixeldash/hover", True))
        self.config.ghost_enabled = _as_bool(self.settings.value("pixeldash/ghost", False))
        try:
            self.config.hover_scale = int(self.settings.value("pixeldash/hover_scale", 6))
        except (TypeError, ValueError):
            pass

    def _remember(self, key: str, value: Any) -> None:
        if self.settings is not None:
            self.settings.setValue(f"pixeldash/{key}", value)

    # --- devices ---
    def _connect_device_signals(self) -> None:
        for name in ("devices_discovered", "device_added", "device_removed", "device_updated"):
            signal = getattr(self.controller, name, None)
            if signal is not None:
                signal.connect(lambda *_: self._refresh_devices())

    def _refresh_devices(self) -> None:
        previous = self._device_id(self.device_combo.currentData())
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        self.device_combo.addItem("None — screen only", None)

        selected = 0
        for device in getattr(self.controller, "devices", []) or []:
            entry = dict(device)
            label = str(entry.get("model") or entry.get("ip") or "Device")
            size = entry.get("matrix_size")
            self.device_combo.addItem(f"{label} · {size}" if size else label, entry)
            if self._device_id(entry) == previous and previous:
                selected = self.device_combo.count() - 1
        self.device_combo.setCurrentIndex(selected)
        self.device_combo.blockSignals(False)
        self._on_device_changed()

    @staticmethod
    def _device_id(device: Optional[Dict[str, Any]]) -> str:
        if not device:
            return ""
        return str(device.get("ble_address") or device.get("mac") or device.get("ip") or "")

    def _on_device_changed(self) -> None:
        device = self.device_combo.currentData()
        if self._panel_sink is not None:
            self._panel_sink.close()
            self._panel_sink = None

        if not device:
            self.device_hint.setText("Rendering to screen and disk only.")
            return

        self._panel_sink = MatrixSink(device, brightness=self.config.brightness)
        # Probing opens the transport, so describe lazily and tolerate failure —
        # a device that is asleep should not block the page.
        self.device_hint.setText(f"Output mode: {self._panel_sink.describe()}")

    # --- controls ---
    def _on_target_changed(self) -> None:
        name = self.target_combo.currentData()
        if name in KNOWN_TARGETS:
            self.config.target = KNOWN_TARGETS[name]
            self._remember("target", name)
            self._refresh_once()

    def _on_hover_toggled(self, enabled: bool) -> None:
        self.config.hover_enabled = bool(enabled)
        self._remember("hover", bool(enabled))
        if not enabled and self._hover_sink is not None:
            self._hover_sink.close()
            self._hover_sink = None
        elif enabled:
            self._republish()

    def _on_corner_changed(self) -> None:
        if self._hover_sink is not None:
            self._hover_sink.set_corner(Corner(self.corner_combo.currentData()))

    def _on_scale_changed(self, value: int) -> None:
        self.config.hover_scale = int(value)
        self._remember("hover_scale", int(value))
        if self._hover_sink is not None:
            # Scale is baked into the window size, so rebuild it.
            self._hover_sink.close()
            self._hover_sink = None
            self._republish()

    def _on_ghost_toggled(self, enabled: bool) -> None:
        self.config.ghost_enabled = bool(enabled)
        self._remember("ghost", bool(enabled))
        if not enabled and self._ghost_sink is not None:
            self._ghost_sink.close()
            self._ghost_sink = None
        elif enabled:
            self._republish()

    def _open_output(self) -> None:
        os.makedirs(self.config.output_dir, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(self.config.output_dir))

    # --- run loop ---
    @property
    def running(self) -> bool:
        return self._thread is not None

    def _toggle_running(self) -> None:
        self._stop() if self.running else self._start()

    def _start(self) -> None:
        if self.running:
            return
        self.service.config = self.config
        self.service.sinks = self._active_sinks()
        self._thread = QThread()
        self._worker = _DashWorker(self.service)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.ticked.connect(self._on_tick)
        self._worker.stopped.connect(self._thread.quit)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(self._clear_thread)
        self._thread.start()

        self.start_button.setText("Stop")
        self._set_status(f"Polling every {self.config.refresh_seconds:g}s.")

    def _stop(self) -> None:
        self.service.stop()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.quit()
            thread.wait(3000)
        self._clear_thread()

    def _clear_thread(self) -> None:
        self._thread = None
        self._worker = None
        self.start_button.setText("Start")

    def _refresh_once(self) -> None:
        """Render one tick synchronously.

        Only used when the loop is stopped — while it runs, the worker is
        already producing ticks and a second poll would double the API calls.
        """
        if self.running:
            self._set_status("Already polling; the next tick is on its way.")
            return
        self.refresh_button.setEnabled(False)
        try:
            self.service.config = self.config
            # The service renders one geometry per attached sink, so it needs
            # the view's sinks even though the view does the publishing.
            self.service.sinks = self._active_sinks()
            self._on_tick(self.service.refresh(publish=False))
        finally:
            self.refresh_button.setEnabled(True)

    def _republish(self) -> None:
        if self._last_tick is not None and self._last_tick.result is not None:
            self._publish(self._last_tick)

    # --- tick handling (GUI thread) ---
    def _on_tick(self, tick: ServiceTick) -> None:
        self._last_tick = tick
        if tick.error:
            self._set_status(tick.error, error=True)
            return

        if tick.result is not None:
            self.surface.set_frames(tick.result.frames, tick.result.frame_ms)
            self.meta_label.setText(
                f"{tick.result.frame_count} frames · {' → '.join(tick.result.scene_ids)} · "
                f"{tick.result.plan.source}"
            )

        self._refresh_status()
        self._publish(tick)

    def _publish(self, tick: ServiceTick) -> None:
        if tick.result is None:
            return

        # Route each sink to the geometry it asked for. The worker already
        # rendered every geometry any sink wants, so the hover and ghost
        # windows get the denser screen grid while the panel keeps hardware
        # resolution.
        reports = []
        for target_name, sinks in group_by_target(
            self._active_sinks(), self.config.target.name
        ).items():
            result = tick.renders.get(target_name, tick.result)
            reports.extend(publish_all(sinks, result))
        problems = [entry.summary for entry in reports if not entry.ok or entry.degraded]
        if problems:
            self._set_status(" · ".join(problems), error=any(not e.ok for e in reports))
        elif tick.events:
            headline = tick.events[-1]
            self._set_status(f"{headline.title} — {headline.detail}".strip(" —"))
        else:
            self._set_status(tick.summary())

    def _active_sinks(self) -> List[Sink]:
        sinks: List[Sink] = [self._file_sink]

        if self.config.hover_enabled:
            if self._hover_sink is None:
                self._hover_sink = HoverSink(
                    scale=self.config.hover_scale,
                    corner=Corner(self.corner_combo.currentData() or Corner.TOP_RIGHT.value),
                    opacity=self.config.hover_opacity,
                )
            sinks.append(self._hover_sink)

        if self.config.ghost_enabled:
            if self._ghost_sink is None:
                self._ghost_sink = GhostDisplaySink()
            sinks.append(self._ghost_sink)

        if self._panel_sink is not None:
            sinks.append(self._panel_sink)
        return sinks

    def _refresh_status(self) -> None:
        tick = self._last_tick
        if tick is None:
            notes = setup_instructions(self.config)
            self.feeds_label.setText(
                "Not configured — " + ", ".join(notes) if notes else "Ready."
            )
            return

        parts = []
        for status in tick.snapshot.feeds:
            state = "ok" if status.ok else status.state.value
            parts.append(f"{status.name}: {state}" + (f" ({status.detail})" if status.detail else ""))
        self.feeds_label.setText(" · ".join(parts) or "No feeds configured.")

    def _set_status(self, message: str, *, error: bool = False) -> None:
        self.status.setText(message)
        self.status.setProperty("state", "error" if error else "")
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

    # --- journal ---
    def _current_date(self) -> _dt.date:
        value = self.date_edit.date()
        return _dt.date(value.year(), value.month(), value.day())

    def _load_note(self) -> None:
        note = self.journal.get(self._current_date())
        self.note_edit.setText(note.text if note else "")

    def _save_note(self) -> None:
        self.journal.set(self._current_date(), self.note_edit.text())
        self._set_status(f"Journal note saved for {self._current_date().isoformat()}.")

    # --- lifecycle ---
    def shutdown(self) -> None:
        """Stop polling and tear down every window this page created."""
        self._stop()
        for sink in (self._hover_sink, self._ghost_sink, self._panel_sink):
            if sink is not None:
                try:
                    sink.close()
                except Exception:
                    pass
        self._hover_sink = None
        self._ghost_sink = None
        self._panel_sink = None

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # Navigating away keeps the loop running on purpose: the hover overlay
        # and the panel are the point, and both outlive this page being visible.
        super().hideEvent(event)


def _eyebrow(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("role", "eyebrow")
    return label


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in ("true", "1", "yes")
