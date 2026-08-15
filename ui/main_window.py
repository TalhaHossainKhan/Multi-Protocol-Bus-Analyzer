"""MainWindow — top-level UI orchestrator.

A step-by-step wizard drives the whole app. ``QStackedWidget`` rotates
between three pages:

    Page 0 — LaunchScreen          (two hero buttons)
    Page 1 — ConnectionScreen      (protocol tabs + device picker)
    Page 2 — MainWorkspaceWidget   (plots + unified Settings sidebar)

Flow
----
Real-time:
    Launch → "Real-Time Analysis" → Connection → user picks device → Connect
    →  SessionManager spawns ingestion worker
    →  device_ready fires → SignalMappingDialog (modal)
    →  on accept, WorkspaceWidget.from_config(mapping) → workspace shows
    →  Settings sidebar controls every visual aspect

Post-Analysis:
    Launch → "Post-Analysis" → file pickers (data + optional layout JSON)
    →  workspace shows with overlay pre-loaded (no live device)

Engine integration
------------------
All previously-built ingestion code (TelemetryStreamer, TcpStreamer,
BleStreamer, SessionManager, DataLogger, DeviceRegistry, …) is reused
unchanged.  This module is *only* the new UI front-end.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Dict, Optional

import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QStackedWidget,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from engine.device_registry import ChannelRecord, DeviceRecord, DeviceRegistry
from engine.port_scanner import PortScanner
from engine.protocol import DeviceMetadataV2
from engine.session import DeviceSession, SessionManager
from engine.notifications import send_alarm_webhook
from engine.session_folder import SessionFolder
from engine.session_serializer import (
    load_session_config,
    save_session_config,
)
from engine.telemetry_stream import DEFAULT_BAUD

from ui.analysis_panel import AnalysisPanel
from ui.connection_screen import ConnectionRequest, ConnectionScreen
from ui.digital_dashboard import DigitalDashboard
from ui.launch_screen import LaunchScreen
from ui.settings_panel import SettingsPanel
from ui.signal_mapping import SignalMappingDialog
from ui.style import ENTERPRISE_QSS
from ui.workspace import WorkspaceWidget

# ── PyQtGraph global perf flags ──────────────────────────────────────────────
pg.setConfigOptions(antialias=False, useOpenGL=False, foreground="#cfcfcf")

# Pages.
_PAGE_LAUNCH     = 0
_PAGE_CONNECTION = 1
_PAGE_WORKSPACE  = 2

UI_REFRESH_MS = 16          # ~60 Hz plot refresh


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _autorange_all_vbs(panel) -> None:
    """Re-enable auto-range and fit every ViewBox on *panel*.

    Used after loading historical overlays in post-analysis — the in-place
    autoRange call inside load_overlay is a no-op while the widget is still
    hidden, so we have to re-run it after the workspace becomes visible.
    """
    for vb in getattr(panel, "_all_vbs", []) or []:
        try:
            vb.enableAutoRange(axis="xy")
            vb.autoRange()
        except Exception:
            pass


# ── Recording setup dialog ────────────────────────────────────────────────────

class _RecordingSetupDialog(QDialog):
    """Asks the user for file format + base filename before recording starts."""

    def __init__(self, default_name: str = "session", parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Start Recording")
        self.setMinimumWidth(440)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        layout.addWidget(QLabel("Name this recording:"))
        from PySide6.QtWidgets import QLineEdit as _QLineEdit
        self._name_edit = _QLineEdit(default_name)
        self._name_edit.setPlaceholderText("e.g. cold_start_run_3")
        layout.addWidget(self._name_edit)

        layout.addSpacing(4)
        layout.addWidget(QLabel("Choose the file format for this recording:"))

        self._grp = QButtonGroup(self)

        self._rb_csv = QRadioButton("CSV  (.csv)  — opens directly in Excel / Google Sheets")
        self._rb_csv.setChecked(True)
        self._grp.addButton(self._rb_csv)
        layout.addWidget(self._rb_csv)

        self._rb_parquet = QRadioButton("Parquet  (.parquet)  — compressed, faster to read back")
        self._grp.addButton(self._rb_parquet)
        layout.addWidget(self._rb_parquet)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def selected_ext(self) -> str:
        return ".parquet" if self._rb_parquet.isChecked() else ".csv"

    def base_name(self) -> str:
        import re as _re
        raw = self._name_edit.text().strip() or "session"
        # Filesystem-safe: replace anything not alnum/dash/underscore.
        return _re.sub(r"[^A-Za-z0-9_-]+", "_", raw).strip("_") or "session"


# ── Inner widget: workspace + full-width Settings tab ────────────────────────

class MainWorkspaceWidget(QWidget):
    """The 'editing' surface with two full-width tabs:

    • 📈 Data    — the live DockArea plot workspace (full screen)
    • ⚙  Settings — the unified settings panel (full screen, no scrolling)

    Switching tabs never loses state; the workspace keeps running at 60 Hz
    even while the Settings tab is visible.
    """

    def __init__(self, parent_window: "MainWindow") -> None:
        super().__init__()
        self._parent_window = parent_window

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Top bar ──────────────────────────────────────────────────────────
        self.top_bar = QFrame(objectName="topBar")
        self.top_bar.setFixedHeight(48)
        bar = QHBoxLayout(self.top_bar)
        bar.setContentsMargins(14, 6, 14, 6)

        self.btn_home = QPushButton("←  Home")
        self.btn_home.setObjectName("homeBtn")
        self.btn_home.clicked.connect(parent_window._on_home_requested)
        bar.addWidget(self.btn_home)

        bar.addStretch()

        self.btn_record = QPushButton("Record")
        self.btn_record.setObjectName("primary")
        self.btn_record.setCheckable(True)
        self.btn_record.setMinimumWidth(130)
        self.btn_record.clicked.connect(parent_window._on_record_toggled)
        bar.addWidget(self.btn_record)

        bar.addSpacing(8)

        self._btn_capture = QPushButton("Capture")
        self._btn_capture.setObjectName("secondary")
        self._btn_capture.setMinimumWidth(130)
        self._btn_capture.setToolTip("Capture a plot panel as a PNG image")
        self._btn_capture.clicked.connect(self._show_capture_menu)
        bar.addWidget(self._btn_capture)

        layout.addWidget(self.top_bar)

        # ── Body: Data tab | Settings tab ────────────────────────────────────
        self.body_tabs = QTabWidget()
        self.body_tabs.setDocumentMode(True)

        # Tab 0 — Data (the live plot DockArea).
        self.workspace = WorkspaceWidget()
        self.body_tabs.addTab(self.workspace, "Data")

        # Tab 1 — Settings (full-width, replaces the narrow sidebar).
        self.settings_panel = SettingsPanel()
        self.settings_panel.setObjectName("")     # clear #sidebar bg in this context
        self.settings_panel.setMinimumWidth(0)
        self.settings_panel.setMaximumWidth(16_777_215)   # remove the 440px cap
        self.settings_panel.attach_workspace(self.workspace)
        self.settings_panel.save_layout_requested.connect(
            parent_window._on_save_layout
        )
        self.settings_panel.load_layout_requested.connect(
            parent_window._on_load_layout
        )
        self.settings_panel.load_layout_from_path_requested.connect(
            parent_window._on_load_layout_from_path
        )
        self.body_tabs.addTab(self.settings_panel, "Settings")

        # Tab 2 — Analysis (DSP, formula engine, special views).
        self.analysis_panel = AnalysisPanel()
        self.analysis_panel.setObjectName("")
        self.analysis_panel.attach_workspace(self.workspace)
        self.body_tabs.addTab(self.analysis_panel, "Analysis")

        layout.addWidget(self.body_tabs, stretch=1)

        # When the user lands on the Data tab, re-fit any panels that hold
        # overlay curves. PyQtGraph's autoRange is a no-op while the widget
        # is hidden, so post-analysis loads need a second chance to fit once
        # geometry exists.
        self.body_tabs.currentChanged.connect(self._on_body_tab_changed)

        # Recording status strip — styled via QSS [recording] property.
        self.recording_status = QLabel("No active recording")
        self.recording_status.setObjectName("recordingStrip")
        self.recording_status.setProperty("recording", "false")
        layout.addWidget(self.recording_status)

    def _show_capture_menu(self) -> None:
        """Show a dropdown menu listing every plot panel; chosen panel is captured."""
        panels = self.workspace.get_panels()
        if not panels:
            QMessageBox.warning(
                self, "Capture", "No plot panels open. Add a device first."
            )
            return
        menu = QMenu(self)
        for panel in panels:
            action = menu.addAction(panel.title)
            action.setData(panel)
        chosen = menu.exec(
            self._btn_capture.mapToGlobal(self._btn_capture.rect().bottomLeft())
        )
        if chosen is not None:
            self._parent_window._on_capture_plot(chosen.data())

    def set_recording_active(self, active: bool, text: str = "") -> None:
        """Toggle the recording strip style and text atomically."""
        self.recording_status.setProperty("recording", "true" if active else "false")
        self.recording_status.style().unpolish(self.recording_status)
        self.recording_status.style().polish(self.recording_status)
        if text:
            self.recording_status.setText(text)
        elif not active:
            self.recording_status.setText("No active recording")

    def _on_body_tab_changed(self, idx: int) -> None:
        """Fit panels that hold overlay curves when the Data tab is shown."""
        if idx != 0:
            return
        for panel in self.workspace.get_panels():
            if getattr(panel, "_overlay_curves", None):
                _autorange_all_vbs(panel)


# ── MainWindow ───────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    """Top-level orchestrator window."""

    def __init__(self, registry: DeviceRegistry) -> None:
        super().__init__()
        self.setWindowTitle("Multi-Protocol Bus Analyzer")
        self.resize(1440, 880)
        self.setMinimumSize(1100, 700)

        # ── Core state ──
        self._registry = registry
        self.session = SessionManager(parent=self)
        self.scanner = PortScanner(self)

        # Most-recently-requested device (used to match incoming signals).
        self._active_device_path: Optional[str] = None
        self._active_device_label: Optional[str] = None
        self._active_device_uid:   Optional[str] = None
        self._active_transport:    Optional[str] = None

        # All device paths that have been requested and are in-flight or ready.
        self._pending_paths: set = set()

        # Build the stacked UI.
        self.stack = QStackedWidget()
        self.launch_screen     = LaunchScreen()
        self.connection_screen = ConnectionScreen(self.scanner)
        self.workspace_widget  = MainWorkspaceWidget(self)

        self.stack.addWidget(self.launch_screen)        # 0
        self.stack.addWidget(self.connection_screen)    # 1
        self.stack.addWidget(self.workspace_widget)     # 2

        self.setCentralWidget(self.stack)
        self._build_statusbar()

        # ── Live Values DRO dock ────────────────────────────────────
        # Docked below the plot area.  Hidden until the user enters the
        # workspace page so it doesn't appear on launch / connection screens.
        self.dro_dashboard = DigitalDashboard(session=self.session, parent=self)
        self.dro_dashboard.attach_workspace(self.workspace_widget.workspace)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.dro_dashboard)
        self.dro_dashboard.hide()
        # Let the Settings sidebar drive the live-values check-list.
        self.workspace_widget.settings_panel.attach_dashboard(self.dro_dashboard)

        # Session folder state (set on record start, cleared on stop).
        self._session_folder: Optional[SessionFolder] = None
        self._session_format: str = ".csv"   # ".csv" or ".parquet"
        self._session_basename: str = "session"

        # Recording elapsed-time tracking. _recording_t0 is the monotonic
        # timestamp at recording start; _recording_label is the recording-strip
        # prefix ("● Recording → folder/"). _recording_timer fires every second
        # and rewrites the strip with the updated MM:SS suffix.
        self._recording_t0: Optional[float] = None
        self._recording_label: str = ""
        self._recording_timer = QTimer(self)
        self._recording_timer.setInterval(1000)
        self._recording_timer.timeout.connect(self._tick_recording_elapsed)

        # ── Wire UI signals ──
        self.launch_screen.mode_selected.connect(self._on_mode_selected)
        self.connection_screen.home_requested.connect(self._on_home_requested)
        self.connection_screen.connect_requested.connect(self._on_connect_requested)
        self.connection_screen.view_plots_requested.connect(self._on_view_plots_clicked)

        # Alarm webhook — connected once to the aggregated workspace signal.
        self.workspace_widget.workspace.alarm_triggered.connect(self._on_alarm_webhook)

        # ── Wire session manager signals ──
        self.session.file_headers_detected.connect(self._on_file_headers_detected)
        self.session.device_ready.connect(self._on_session_device_ready)
        self.session.device_failed.connect(self._on_session_device_failed)
        self.session.device_removed.connect(self._on_session_device_removed)
        self.session.recording_changed.connect(self._on_recording_changed)
        self.session.recording_rows.connect(self._on_recording_rows)
        self.session.recording_error.connect(self._on_recording_error)
        self.session.session_ended.connect(self._on_session_ended)

        # ── Refresh timer (60 Hz) ──
        self._plot_timer = QTimer(self)
        self._plot_timer.setTimerType(Qt.PreciseTimer)
        self._plot_timer.timeout.connect(self._refresh_plots)
        self._plot_timer.start(UI_REFRESH_MS)

        # Start scanner so the USB tab is responsive when entered.
        self.scanner.start()

        # Initial page.
        self.stack.setCurrentIndex(_PAGE_LAUNCH)

    # ── Status bar ───────────────────────────────────────────────────────────

    def _build_statusbar(self) -> None:
        self.setStatusBar(QStatusBar(self))
        self.statusBar().showMessage("Ready")

    # ── Mode selection on launch screen ──────────────────────────────────────

    def _on_mode_selected(self, mode: str) -> None:
        if mode == "realtime":
            self.stack.setCurrentIndex(_PAGE_CONNECTION)
            self.statusBar().showMessage("Pick a transport and select a device.")
        elif mode == "post_analysis":
            self._enter_post_analysis()

    # ── Home button ──────────────────────────────────────────────────────────

    def _on_home_requested(self) -> None:
        """Navigate back to the launch screen, tearing down any live device."""
        # If there's any active device, confirm before disconnecting everything.
        if self.session.ready_devices():
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Question)
            box.setWindowTitle("Disconnect Devices?")
            box.setText("Returning to the launch screen will disconnect all active devices.")
            box.setInformativeText("Any unsaved layout changes will be lost. Continue?")
            box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
            box.setDefaultButton(QMessageBox.No)
            if box.exec() != QMessageBox.Yes:
                return
            for s in list(self.session.list_active()):
                self.session.remove_device(s.device_path)

        # Drop any stale UI state from the connection screen so the next visit
        # starts clean (no leftover entries claiming a device is still ready).
        self.connection_screen.clear_connected_devices()
        self.connection_screen.reset_connect_button()
        # Empty the workspace too — otherwise the next Signal Mapping merges
        # onto the leftover plots and the user sees ghost panels.
        self.workspace_widget.workspace.clear()
        self.dro_dashboard.clear()
        self.dro_dashboard.hide()
        self.workspace_widget.workspace.clear_baseline()

        # Full state reset so the next run starts as if the app was just
        # launched. Previously a stale _pending_paths entry or _active_*
        # value could let the *next* connect's device_ready handler match the
        # wrong device, leaving one freshly-created panel without channels.
        self._pending_paths.clear()
        self._active_device_path  = None
        self._active_device_label = None
        self._active_device_uid   = None
        self._active_transport    = None
        self._session_folder      = None
        self._session_basename    = "session"
        self._session_format      = ".csv"
        self._rate_warning_shown  = False

        # Reset record button visual state.
        self.workspace_widget.btn_record.setChecked(False)
        self.workspace_widget.btn_record.setText("Record")
        self.workspace_widget.btn_record.setEnabled(True)
        self.workspace_widget.set_recording_active(False)

        # Detach the side panels from the now-empty workspace so they don't
        # hold dangling references to dead PlotPanel objects.
        self.workspace_widget.settings_panel.set_active_panel(None)
        self.workspace_widget.analysis_panel.set_active_panel(None)
        self.workspace_widget.analysis_panel.attach_session(None)
        self.workspace_widget.analysis_panel.refresh()

        self.stack.setCurrentIndex(_PAGE_LAUNCH)
        self.statusBar().showMessage("Ready")

    # ── Connection screen ────────────────────────────────────────────────────

    def _resolve_device_path(self, req: ConnectionRequest) -> Optional[str]:
        """Compute the canonical device_path for *req* without side effects."""
        try:
            if req.transport == "usb":
                return req.params["device_path"]
            if req.transport == "tcp":
                from engine.tcp_stream import tcp_device_path
                return tcp_device_path(req.params["host"], req.params["port"])
            if req.transport == "ble":
                from engine.ble_stream import ble_device_path
                return ble_device_path(req.params["address"])
            if req.transport == "can":
                from engine.can_stream import can_device_path
                return can_device_path(req.params["interface"], req.params["channel"])
            if req.transport == "file":
                from engine.file_stream import file_device_path
                return file_device_path(req.params["file_path"])
        except Exception:
            return None
        return None

    def _is_path_active(self, path: str) -> bool:
        """True if *path* is already pending or ready in the session manager."""
        if path in self._pending_paths:
            return True
        return any(s.device_path == path for s in self.session.list_active())

    def _on_connect_requested(self, req: ConnectionRequest) -> None:
        """Spin up the matching ingestion worker via SessionManager."""
        # Resolve the device_path *before* mutating session state so we can
        # reject duplicate connects without leaving the screen in a half-state.
        candidate_path = self._resolve_device_path(req)
        if candidate_path is not None and self._is_path_active(candidate_path):
            QMessageBox.information(
                self, "Already Connected",
                f"{req.display_label} is already connected. "
                "Click View Plots to open the workspace."
            )
            self.connection_screen.reset_connect_button()
            return

        self._active_transport    = req.transport
        self._active_device_label = req.display_label
        self.statusBar().showMessage(f"Connecting to {req.display_label}…")

        try:
            if req.transport == "usb":
                session = self.session.add_device(
                    req.params["device_path"],
                    baud=req.params.get("baud_rate", DEFAULT_BAUD),
                )
                self._active_device_path = req.params["device_path"]
            elif req.transport == "tcp":
                from engine.tcp_stream import tcp_device_path
                session = self.session.add_tcp_device(
                    req.params["host"], req.params["port"]
                )
                self._active_device_path = tcp_device_path(
                    req.params["host"], req.params["port"]
                )
            elif req.transport == "ble":
                from engine.ble_stream import ble_device_path
                session = self.session.add_ble_device(req.params["address"])
                self._active_device_path = ble_device_path(req.params["address"])
            elif req.transport == "can":
                from engine.can_stream import can_device_path
                session = self.session.add_can_device(
                    req.params["interface"],
                    req.params["channel"],
                    dbc_path=req.params.get("dbc_path"),
                    bitrate=req.params.get("bitrate", 500_000),
                )
                self._active_device_path = can_device_path(
                    req.params["interface"], req.params["channel"]
                )
            elif req.transport == "file":
                from engine.file_stream import file_device_path
                session = self.session.add_file_device(req.params["file_path"])
                self._active_device_path = file_device_path(req.params["file_path"])
            else:
                raise ValueError(f"Unknown transport: {req.transport}")
            if self._active_device_path:
                self._pending_paths.add(self._active_device_path)
        except Exception as exc:
            self._active_transport    = None
            self._active_device_path  = None
            self._active_device_label = None
            QMessageBox.critical(self, "Connection Error", str(exc))
            self.statusBar().showMessage("Connection failed.")

    # ── File-transport header negotiation ────────────────────────────────────

    def _on_file_headers_detected(self, device_path: str, col_names: list) -> None:
        """Show ColumnMappingDialog then unblock the LiveFileWorker."""
        from ui.file_dialogs import ColumnMappingDialog
        from engine.file_stream import LiveFileWorker
        dlg = ColumnMappingDialog(device_path, col_names, parent=self)
        if dlg.exec() == dlg.DialogCode.Accepted:
            selected = dlg.selected_columns()
            dev_session = self.session.get_device(device_path)
            if dev_session and isinstance(dev_session.streamer, LiveFileWorker):
                dev_session.streamer.set_column_mapping(selected)
        else:
            dev_session = self.session.get_device(device_path)
            if dev_session:
                dev_session.streamer.abort()
            self._pending_paths.discard(device_path)
            self.connection_screen.reset_connect_button()
            self.statusBar().showMessage("File column selection cancelled.")

    # ── SessionManager signal handlers ───────────────────────────────────────

    def _on_session_device_ready(
        self, device_path: str, metadata: DeviceMetadataV2
    ) -> None:
        if device_path not in self._pending_paths:
            return

        self._active_device_uid = metadata.device_uid
        self._upsert_registry(device_path, metadata)

        label     = self._active_device_label or device_path
        transport = self._active_transport or "?"

        # Show the Signal Mapping dialog so the user can configure scales,
        # y-axes, and channel-to-plot assignment for this device.
        dlg = SignalMappingDialog(
            device_path=device_path,
            device_label=label,
            metadata=metadata,
            parent=self,
        )
        if dlg.exec() != dlg.DialogCode.Accepted:
            # User cancelled — disconnect this device cleanly.
            self.session.remove_device(device_path)
            self._pending_paths.discard(device_path)
            self.connection_screen.reset_connect_button()
            self.statusBar().showMessage("Signal mapping cancelled — disconnected.")
            return

        layout_cfg = dlg.result_layout()
        dro_channels = dlg.result_digital_readouts()
        workspace  = self.workspace_widget.workspace

        # Merge into existing workspace if other devices are already plotted;
        # replace everything if this is the first device.
        already_has_panels = bool(workspace.get_panels())
        workspace.from_config(layout_cfg, merge=already_has_panels)

        # Wire sidebar and analysis panel.
        self.workspace_widget.settings_panel.attach_workspace(workspace)
        self.workspace_widget.analysis_panel.attach_workspace(workspace)
        self.workspace_widget.analysis_panel.attach_session(self.session)
        self.workspace_widget.analysis_panel.refresh()
        panels = workspace.get_panels()
        if panels:
            self.workspace_widget.settings_panel.set_active_panel(panels[0])
            self.workspace_widget.analysis_panel.set_active_panel(panels[0])

        workspace.update_device_status(label, True)

        # Populate the DRO dock with any channels the user ticked in the
        # Signal Mapping dialog.  set_channels replaces this device's
        # readouts so reconnects don't duplicate boxes.
        if dro_channels:
            units = {c.name: c.unit for c in metadata.channels}
            self.dro_dashboard.set_channels(
                device_path, dro_channels, device_label=label, units=units,
            )
        # Kick the workspace once so the freshly-added panel(s) start showing
        # data on the next paint instead of staying empty until the next 16 ms
        # refresh tick — and for second-device-connects, this also re-walks
        # the existing panel(s) so a slow handshake doesn't leave the first
        # MCU's plot looking frozen.
        workspace.refresh(self.session)

        # Add to the Connected Devices list and stay on the connection screen.
        # Navigation to the workspace happens only when the user clicks View Plots.
        self.connection_screen.add_connected_device(label, transport)
        self.statusBar().showMessage(
            f"{label} ready — connect another device or click View Plots."
        )

    def _on_session_device_failed(self, device_path: str, error: str) -> None:
        if device_path not in self._pending_paths:
            return
        self._pending_paths.discard(device_path)
        self._active_device_path  = None
        self._active_device_label = None
        self._active_device_uid   = None
        self.connection_screen.reset_connect_button()
        QMessageBox.critical(
            self, "Handshake Failed",
            f"Could not establish a session.\n\n{error}"
        )
        self.statusBar().showMessage("Handshake failed.")

    def _on_session_device_removed(self, device_path: str) -> None:
        self._pending_paths.discard(device_path)
        # Strip channels from this device out of all plots.
        self.workspace_widget.workspace.remove_device(device_path)
        # Strip its DRO tiles too — they'd otherwise show stale frozen values.
        self.dro_dashboard.remove_device(device_path)
        # If no devices remain, reset status bar indicators.
        if not self.session.ready_devices():
            self.workspace_widget.workspace.update_device_status("", False)
            self._active_device_path  = None
            self._active_device_label = None
            self._active_device_uid   = None
            self._active_transport    = None
            self.workspace_widget.btn_record.setChecked(False)
            self.workspace_widget.btn_record.setText("Record")

    def _on_view_plots_clicked(self) -> None:
        """Navigate to the workspace. Plots are already configured by Signal Mapping."""
        if not self.session.ready_devices():
            return

        n = len(self.session.ready_devices())
        self._pending_paths.clear()
        self.stack.setCurrentIndex(_PAGE_WORKSPACE)
        # Force the Data tab — body_tabs preserves its index across sessions,
        # and if the user left it on Settings/Analysis the 60 Hz refresh would
        # skip and live curves wouldn't update (only the previously-cached
        # frame would be visible).
        self.workspace_widget.body_tabs.setCurrentIndex(0)
        # Reveal the Live Values dock when there's something to show.
        if self.dro_dashboard._boxes:
            self.dro_dashboard.show()
        # Kick one refresh immediately so panels created in this signal-mapping
        # round get their first data tick without waiting up to 16 ms for the
        # 60 Hz timer — useful when a panel was added late.
        self.workspace_widget.workspace.refresh(self.session)
        self.statusBar().showMessage(
            f"Streaming from {n} device{'s' if n > 1 else ''}."
        )

    def _on_session_ended(self) -> None:
        self.statusBar().showMessage("Session ended.")

    def _disconnect_active_device(self) -> None:
        path = self._active_device_path
        if path is None:
            return
        self.session.remove_device(path)
        # _on_session_device_removed will clear the state.

    # ── Recording ────────────────────────────────────────────────────────────

    def _on_record_toggled(self, checked: bool) -> None:
        if checked:
            self._start_recording()
        else:
            self._stop_recording()

    def _start_recording(self) -> None:
        ready = self.session.ready_devices()
        if not ready:
            self.workspace_widget.btn_record.setChecked(False)
            return

        # ask the user for a name + file format.
        default_name = self._active_device_label or "session"
        fmt_dlg = _RecordingSetupDialog(default_name, self)
        if fmt_dlg.exec() != QDialog.DialogCode.Accepted:
            self.workspace_widget.btn_record.setChecked(False)
            return
        self._session_format = fmt_dlg.selected_ext()
        self._session_basename = fmt_dlg.base_name()

        # pick the parent folder; session sub-folder is created inside.
        folder_root = SessionFolder.default_root()
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose Session Folder Location", folder_root
        )
        if not chosen:
            self.workspace_widget.btn_record.setChecked(False)
            return

        label = self._active_device_label or "device"
        # Use the user-chosen base name for the session folder so the folder
        # itself reflects what the user typed (e.g. "cold_start_run_3_<ts>/").
        self._session_folder = SessionFolder(chosen, self._session_basename)

        save_paths: dict[str, str] = {}
        for s in ready:
            save_paths[s.device_path] = str(
                self._session_folder.data_path(
                    s.device_path, self._session_basename,
                    ext=self._session_format,
                )
            )

        actual = self.session.start_recording_all(save_paths)
        self.workspace_widget.btn_record.setText("Stop")
        folder_name = self._session_folder.path.name
        self._recording_label = f"● Recording  →  {folder_name}/"
        self._recording_t0 = time.monotonic()
        self.workspace_widget.set_recording_active(
            True, f"{self._recording_label}   00:00"
        )
        self.workspace_widget.workspace.update_recording_status(True, 0.0)
        self._recording_timer.start()

    def _stop_recording(self) -> None:
        self.session.stop_recording_all()
        self._recording_timer.stop()
        self._recording_t0 = None
        self._recording_label = ""
        self.workspace_widget.btn_record.setText("Record")
        self.workspace_widget.set_recording_active(False)
        self.workspace_widget.workspace.update_recording_status(False)

        # Auto-save session_config.json alongside the data — no extra dialog.
        folder = self._session_folder
        if folder is not None:
            basename = getattr(self, "_session_basename", None) or (
                self._active_device_label or "device"
            )
            data_files: dict[str, str] = {}
            for s in self.session.list_active():
                rel = folder.data_path(
                    s.device_path, basename, ext=self._session_format
                ).name
                data_files[s.device_path] = rel
            try:
                save_session_config(
                    self.workspace_widget.workspace.to_config(),
                    str(folder.config_path()),
                    device_uid=self._active_device_uid,
                    device_name=self._active_device_label,
                    transport=self._active_transport,
                    analysis=self.workspace_widget.analysis_panel.to_config(),
                    session_folder=True,
                    data_files=data_files,
                )
                self.statusBar().showMessage(
                    f"Session saved → {folder.path.name}/", 8_000
                )
            except Exception as exc:
                self.statusBar().showMessage(f"Auto-save failed: {exc}", 6_000)
            self._session_folder = None

    def _on_recording_changed(self, device: str, is_recording: bool) -> None:
        if not is_recording and not self.session.is_any_recording():
            self._recording_timer.stop()
            self._recording_t0 = None
            self._recording_label = ""
            self.workspace_widget.btn_record.setChecked(False)
            self.workspace_widget.btn_record.setText("Record")
            self.workspace_widget.set_recording_active(False)

    def _on_recording_rows(self, device: str, total_rows: int, path: str) -> None:
        if self._recording_t0 is None:   # already stopped — discard stale signal
            return
        folder = self._session_folder
        display = f"{folder.path.name}/" if folder else os.path.basename(path)
        # Update the cached label so the next timer tick keeps the row count
        # visible alongside the live elapsed time.
        self._recording_label = (
            f"● Recording  →  {display}   ({total_rows:,} rows)"
        )
        self.workspace_widget.set_recording_active(
            True, f"{self._recording_label}   {self._format_elapsed()}"
        )

    def _on_recording_error(self, device: str, error: str) -> None:
        QMessageBox.critical(self, "Recording Error", error)
        self._recording_timer.stop()
        self._recording_t0 = None
        self._recording_label = ""
        self.workspace_widget.btn_record.setChecked(False)
        self.workspace_widget.set_recording_active(False)

    def _format_elapsed(self) -> str:
        if self._recording_t0 is None:
            return "00:00"
        secs_total = int(time.monotonic() - self._recording_t0)
        return f"{secs_total // 60:02d}:{secs_total % 60:02d}"

    def _tick_recording_elapsed(self) -> None:
        if self._recording_t0 is None:
            return
        elapsed = time.monotonic() - self._recording_t0
        self.workspace_widget.workspace.update_recording_status(True, elapsed)
        self.workspace_widget.set_recording_active(
            True, f"{self._recording_label}   {self._format_elapsed()}"
        )

    # ── Plot capture ─────────────────────────────────────────────────────────

    def _on_capture_plot(self, panel) -> None:
        if panel is None:
            return
        ts      = datetime.now().strftime("%Y%m%d-%H%M%S")
        title   = panel.title.replace(" ", "_")
        default = os.path.join(
            os.path.expanduser("~/Documents"), f"{title}_{ts}.png"
        )
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Plot Image", default, "PNG Images (*.png)"
        )
        if not path:
            return
        if not path.lower().endswith(".png"):
            path += ".png"
        try:
            panel.capture(path)
            self.statusBar().showMessage(f"Plot captured → {os.path.basename(path)}", 5_000)
        except Exception as exc:
            QMessageBox.critical(self, "Capture Error", str(exc))

    # ── Layout I/O (from Settings panel) ─────────────────────────────────────

    def _on_save_layout(self) -> None:
        workspace = self.workspace_widget.workspace
        if not workspace.get_panels():
            return
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        default = f"layout_config_{ts}.json"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Layout Config",
            os.path.join(os.path.expanduser("~/Documents"), default),
            "JSON (*.json)"
        )
        if not path:
            return
        if not path.endswith(".json"):
            path += ".json"
        try:
            save_session_config(
                workspace.to_config(),
                path,
                device_uid=self._active_device_uid,
                device_name=self._active_device_label,
                transport=self._active_transport,
                analysis=self.workspace_widget.analysis_panel.to_config(),
            )
            QMessageBox.information(
                self, "Layout Saved",
                f"Workspace layout saved to:\n{path}"
            )
        except Exception as exc:
            QMessageBox.critical(self, "Save Error", str(exc))

    def _on_load_layout(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Template", "", "JSON (*.json);;All Files (*)"
        )
        if not path:
            return
        self._on_load_layout_from_path(path)

    def _on_load_layout_from_path(self, path: str) -> None:
        """Load a layout/template JSON from *path* without a file dialog."""
        try:
            doc = load_session_config(path)
        except Exception as exc:
            QMessageBox.critical(self, "Load Error", str(exc))
            return
        self._apply_layout_doc(doc)

    def _apply_layout_doc(self, doc: dict) -> None:
        """Apply a loaded layout document to the workspace (shared by all load paths)."""
        workspace = self.workspace_widget.workspace
        workspace.from_config(doc.get("plots", []))
        self.workspace_widget.settings_panel.attach_workspace(workspace)
        analysis_cfg = doc.get("analysis", {})
        if analysis_cfg:
            self.workspace_widget.analysis_panel.from_config(analysis_cfg)
        panels = workspace.get_panels()
        for p_cfg, panel in zip(doc.get("plots", []), panels):
            ov_path = p_cfg.get("overlay_path")
            if ov_path and os.path.exists(ov_path):
                try:
                    panel.load_overlay(ov_path)
                except Exception:
                    pass
        if panels:
            self.workspace_widget.settings_panel.set_active_panel(panels[0])
        saved_uid = doc.get("device_uid")
        if (saved_uid and self._active_device_uid
                and saved_uid != self._active_device_uid):
            QMessageBox.warning(
                self, "Different Device",
                "This template was saved for a different device.\n\n"
                f"Saved for : {doc.get('device_name', saved_uid)}\n"
                f"Connected: {self._active_device_label}"
            )

    # ── Post-analysis flow ───────────────────────────────────────────────────

    def _enter_post_analysis(self) -> None:
        """Primary post-analysis flow: pick a session folder (one click).

        If the folder contains session_config.json everything is restored
        automatically.  If not, falls back to the legacy file-by-file dialog.
        """
        folder = QFileDialog.getExistingDirectory(
            self, "Open Session Folder",
            SessionFolder.default_root(),
        )
        if not folder:
            return

        if SessionFolder.is_session_folder(folder):
            self._load_session_folder(folder)
        else:
            # Legacy fallback: folder has no session_config.json.
            self._enter_post_analysis_legacy(folder)

    def _load_session_folder(self, folder_path: str) -> None:
        """Restore plots + data from a unified session folder."""
        try:
            doc = SessionFolder.load_folder(folder_path)
        except Exception as exc:
            QMessageBox.critical(self, "Load Error", str(exc))
            return

        workspace = self.workspace_widget.workspace
        workspace.from_config(doc.get("plots", []))

        # Make the workspace visible BEFORE loading overlays. PyQtGraph's
        # autoRange is a silent no-op while the widget has no geometry, so
        # the view would otherwise stay at its default (0, 1) range while
        # the recorded data sits at far-larger PC_Time_s values.
        self.workspace_widget.btn_record.setEnabled(False)
        self.stack.setCurrentIndex(_PAGE_WORKSPACE)
        self.workspace_widget.body_tabs.setCurrentIndex(0)   # Data tab

        # Map device_path -> absolute file path so we can route each panel's
        # channels to the file that actually contains them.
        panels = workspace.get_panels()
        device_to_file: dict[str, str] = {}
        for dev_path, rel_name in doc.get("data_files", {}).items():
            abs_path = os.path.join(folder_path, rel_name)
            if os.path.exists(abs_path):
                device_to_file[dev_path] = abs_path
        # Fallback for older sessions without data_files — just glob for
        # any data_* file; routing-by-device won't work but at least one
        # panel will see something.
        fallback_paths: list[str] = []
        if not device_to_file:
            for fname in sorted(os.listdir(folder_path)):
                if fname.startswith("data_") and fname.endswith((".csv", ".parquet", ".pq")):
                    fallback_paths.append(os.path.join(folder_path, fname))

        # For each panel: group its channels by device_path, load only the
        # channels that panel owns from each device's CSV. Stack files onto
        # the panel (clear=False) so multi-device panels accumulate.
        load_errors: list[str] = []
        total_rows = 0
        for panel in panels:
            cfgs = list(panel._channels.values()) if hasattr(panel, "_channels") else []
            # Group channel names by device_path.
            per_device: dict[str, set] = {}
            for cfg in cfgs:
                per_device.setdefault(cfg.device_path, set()).add(cfg.channel_name)

            # restore_config may have already loaded a user-added overlay from
            # the real-time session. Remember its path, wipe so the recording
            # load starts from a clean slate, and re-apply on top so post-
            # analysis shows BOTH the recording and the user's overlay.
            user_overlay_path = getattr(panel, "_overlay_path", None)

            panel.clear_overlay()   # one wipe per panel before stacking files
            first = True
            for dev_path, ch_names in per_device.items():
                abs_path = device_to_file.get(dev_path)
                if abs_path is None:
                    # Fallback: try each unrouted file. Channel filter still
                    # protects against rendering the wrong channels.
                    candidates = fallback_paths or list(device_to_file.values())
                else:
                    candidates = [abs_path]
                for src in candidates:
                    try:
                        panel.load_overlay(
                            src,
                            channel_configs=cfgs,
                            channel_filter=ch_names,
                            clear=False,
                        )
                        total_rows += int(getattr(panel, "_overlay_last_rows", 0) or 0)
                        first = False
                    except Exception as exc:
                        load_errors.append(f"{os.path.basename(src)}: {exc}")
            # Re-apply any user-added overlay from the real-time session on
            # top of the recording data.
            if user_overlay_path and os.path.exists(user_overlay_path):
                try:
                    panel.load_overlay(user_overlay_path, clear=False)
                except Exception:
                    pass
            # Replay any custom-math formulas the user had during recording.
            # Must run AFTER load_overlay so _overlay_data is populated.
            try:
                panel.replay_formulas()
            except Exception:
                pass
            # Defer the auto-fit to the next event-loop tick so Qt has time
            # to lay the now-visible widget out.
            QTimer.singleShot(0, lambda p=panel: _autorange_all_vbs(p))

        self.workspace_widget.settings_panel.attach_workspace(workspace)
        # Analysis panel needs both wires too — without attach_workspace it
        # won't see the restored panels, and without attach_session its DSP /
        # custom-math handlers can't fall back to overlay data correctly.
        self.workspace_widget.analysis_panel.attach_workspace(workspace)
        self.workspace_widget.analysis_panel.attach_session(self.session)
        self.workspace_widget.analysis_panel.refresh()
        analysis_cfg = doc.get("analysis", {})
        if analysis_cfg:
            self.workspace_widget.analysis_panel.from_config(analysis_cfg)
        if panels:
            self.workspace_widget.settings_panel.set_active_panel(panels[0])
            self.workspace_widget.analysis_panel.set_active_panel(panels[0])

        folder_name = os.path.basename(folder_path)
        if load_errors:
            self.statusBar().showMessage(
                f"Session loaded ({total_rows:,} rows) with "
                f"{len(load_errors)} data-load error(s): "
                + "; ".join(load_errors[:2]),
                15_000,
            )
        elif total_rows:
            self.statusBar().showMessage(
                f"Session loaded: {folder_name}  ({total_rows:,} rows)", 8_000
            )
        else:
            self.statusBar().showMessage(
                f"Session loaded: {folder_name} — no data rows rendered. "
                "The recording file may be empty or in an unexpected format.",
                15_000,
            )

    def _enter_post_analysis_legacy(self, start_dir: str = "") -> None:
        """Legacy fallback: pick data file then optional layout JSON separately."""
        data_path, _ = QFileDialog.getOpenFileName(
            self, "Open Recorded Data File",
            start_dir or os.path.expanduser("~/Documents"),
            "Data Files (*.csv *.tsv *.parquet *.pq);;All Files (*)"
        )
        if not data_path:
            return

        cfg_path, _ = QFileDialog.getOpenFileName(
            self, "Open Layout Config (optional — Cancel to skip)",
            os.path.dirname(data_path),
            "JSON (*.json);;All Files (*)"
        )

        workspace = self.workspace_widget.workspace
        if cfg_path:
            try:
                doc = load_session_config(cfg_path)
                workspace.from_config(doc.get("plots", []))
            except Exception as exc:
                QMessageBox.warning(self, "Layout Error",
                                    f"{exc}\n\nUsing default layout.")
                workspace.from_config([])
        else:
            workspace.from_config([{
                "id": "plot_0", "title": "Historical Plot",
                "x_device": None, "x_channel": None,
                "rolling_window_s": 0.0,
                "y_axes": [{"label": "", "min": None, "max": None,
                            "locked": False, "color": "#cfcfcf"}],
                "channels": [], "thresholds": [], "overlay_path": None,
            }])

        panels = workspace.get_panels()
        if panels:
            try:
                panels[0].load_overlay(data_path)
            except Exception as exc:
                QMessageBox.warning(self, "Overlay Error", str(exc))

        self.workspace_widget.settings_panel.attach_workspace(workspace)
        if panels:
            self.workspace_widget.settings_panel.set_active_panel(panels[0])

        self.workspace_widget.btn_record.setEnabled(False)
        self.stack.setCurrentIndex(_PAGE_WORKSPACE)
        self.statusBar().showMessage(
            f"Loaded {os.path.basename(data_path)} for post-analysis."
        )

    # ── Alarm webhook ────────────────────────────────────────────────────────

    def _on_alarm_webhook(
        self,
        panel_title: str,
        alarm_name: str,
        channel_name: str,
        value: float,
        ts: float,
    ) -> None:
        url = self.workspace_widget.analysis_panel.webhook_url()
        if url:
            send_alarm_webhook(
                url=url,
                alarm_name=alarm_name,
                channel_name=channel_name,
                value=value,
                timestamp=ts,
                device_name=self._active_device_label or "",
            )

    # ── Registry helper ──────────────────────────────────────────────────────

    def _upsert_registry(self, device_path: str, metadata: DeviceMetadataV2) -> None:
        channels = [
            ChannelRecord(
                channel_id=ch.channel_id, name=ch.name, unit=ch.unit,
                type_label=ch.label, range_min=ch.range_min, range_max=ch.range_max,
            )
            for ch in metadata.channels
        ]
        now = _now_iso()
        record = DeviceRecord(
            uid=metadata.device_uid,
            device_name=metadata.device_name,
            display_name=self._active_device_label or metadata.device_name,
            firmware_version=metadata.firmware_version,
            channels=channels,
            first_seen=now, last_seen=now,
            last_baud=DEFAULT_BAUD,
        )
        self._registry.upsert(record)

    # ── 60 Hz refresh ────────────────────────────────────────────────────────

    def _refresh_plots(self) -> None:
        # Skip rendering entirely while the user is on Settings or Analysis tabs.
        # This is the single biggest performance win: zero pyqtgraph work when
        # the Data tab is not visible.
        if self.workspace_widget.body_tabs.currentIndex() != 0:
            return
        self.workspace_widget.workspace.refresh(self.session)
        self._check_rate_warning()

    def _check_rate_warning(self) -> None:
        """Warn once via the status bar if combined data rate may cause lag."""
        if getattr(self, "_rate_warning_shown", False):
            return
        for ds in self.session.ready_devices():
            if ds.metadata is None or ds.buffer is None:
                continue
            data = ds.buffer.read_latest(200)
            if data.shape[1] < 10:
                continue
            dt = float(data[0, -1] - data[0, 0])
            if dt <= 0:
                continue
            rate = data.shape[1] / dt
            n_ch = ds.metadata.num_channels
            if rate * n_ch > 800:   # e.g. 4 ch × 200 Hz
                self._rate_warning_shown = True
                self.statusBar().showMessage(
                    f"⚠  High data rate (~{rate:.0f} Hz × {n_ch} ch). "
                    "Enable a rolling window in Settings → Time Window to reduce plot load.",
                    10_000,
                )
                break

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def closeEvent(self, event: QCloseEvent) -> None:
        self._plot_timer.stop()
        # Tear down active devices cleanly.
        for s in self.session.list_active():
            self.session.remove_device(s.device_path)
        self.scanner.stop()
        self.scanner.wait(2000)
        try:
            self._registry.save()
        except Exception:
            pass
        super().closeEvent(event)


# ── Convenience: app-level stylesheet install ────────────────────────────────

def apply_enterprise_theme(app) -> None:
    """Apply the dark theme to the whole QApplication."""
    app.setStyleSheet(ENTERPRISE_QSS)
