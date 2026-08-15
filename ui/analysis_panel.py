"""Analysis Panel — unified analytical workspace.

Top-to-bottom logical flow:
  1. Active Plot selector  — which panel the annotation tools edit
  2. Historical Overlay    — load CSV/Parquet comparison data
  3. Visual Thresholds     — unified X and Y threshold lines
  4. Alarms & Webhooks     — logic alarms with Slack/Teams notification
  5. Analysis Tags         — draggable time-region annotations
  6. DSP Functions         — with inline channel + data-range targeting
  7. Custom Math Channel   — numexpr formula engine, strict validation
  8. Special Views         — FFT/Spectrogram, 3-D scatter, Oscilloscope
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from engine.analysis import (
    compute_correlation,
    compute_fft,
    compute_histogram,
    compute_max,
    compute_mean,
    compute_median,
    compute_min,
    compute_moving_average,
    compute_peaks,
    compute_rms,
    compute_std,
    evaluate_formula,
    sanitize_name,
    slice_by_range,
)
from ui.advanced_views import FFTWindow, OscilloscopeWindow, ThreeDWindow


# ── Toggleable QListWidget ────────────────────────────────────────────────────

class _ToggleListWidget(QListWidget):
    """QListWidget where clicking an already-selected item clears selection.

    Used by Visual Thresholds / Alarms / Analysis Tags so the user can back
    out of a selection without picking a different row.
    """

    def mousePressEvent(self, event) -> None:
        item = self.itemAt(event.pos())
        if item is not None and item.isSelected():
            self.clearSelection()
            self.setCurrentItem(None)
            event.accept()
            return
        super().mousePressEvent(event)


# ── Colour swatch button ──────────────────────────────────────────────────────

class _ColorChip(QPushButton):
    color_changed = Signal(str)

    def __init__(self, color: str = "#4FC3F7", parent=None) -> None:
        super().__init__(parent)
        self._c = color
        self.setFixedSize(52, 24)
        self.clicked.connect(self._pick)
        self._repaint()

    def color(self) -> str:
        return self._c

    def set_color(self, c: str) -> None:
        if c and c != self._c:
            self._c = c
            self._repaint()
            self.color_changed.emit(c)

    def _repaint(self) -> None:
        self.setStyleSheet(
            f"background:{self._c}; border:1px solid #3a3a3a; border-radius:3px;"
        )

    def _pick(self) -> None:
        c = QColorDialog.getColor(QColor(self._c), self)
        if c.isValid():
            self._c = c.name()
            self._repaint()
            self.color_changed.emit(self._c)


# ═══════════════════════════════════════════════════════════════════════════════
# AnalysisPanel
# ═══════════════════════════════════════════════════════════════════════════════

class AnalysisPanel(QWidget):
    """The Analysis tab — all operational and analytical tools."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("sidebar")

        self._workspace   = None
        self._session     = None
        self._active_panel = None   # PlotPanel currently targeted by annotations


        self._fft_window:   Optional[FFTWindow]          = None
        self._3d_window:    Optional[ThreeDWindow]       = None
        self._scope_window: Optional[OscilloscopeWindow] = None
        self._result_windows: List = []

        self._prefs = QSettings("DAQPlatform", "UserPrefs")
        self._build_ui()

    # ── Attachment ─────────────────────────────────────────────────────────────

    def attach_workspace(self, workspace) -> None:
        self._workspace = workspace
        # Route aggregated alarm events to the alarm log in this panel.
        try:
            workspace.alarm_triggered.connect(self._on_alarm_log_entry)
        except Exception:
            pass
        self.refresh()

    def attach_session(self, session) -> None:
        self._session = session

    def set_active_panel(self, panel) -> None:
        """Point the annotation tools (overlay, thresholds, alarms, tags) at *panel*."""
        self._active_panel = panel
        self._refresh_annotation_panel_state()

    def refresh(self) -> None:
        self._rebuild_panel_combo()
        self._rebuild_channel_combos()
        self._rebuild_tag_combo()

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body        = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(10, 10, 10, 10)
        body_layout.setSpacing(10)

        # Active plot selector — boxed so it visually matches the other sections.
        active_plot_box = QGroupBox("Active plot")
        ap_layout = QHBoxLayout(active_plot_box)
        self._panel_combo = QComboBox()
        self._panel_combo.setToolTip(
            "All annotation controls below (overlay, thresholds, alarms, tags) "
            "operate on this plot panel."
        )
        self._panel_combo.currentIndexChanged.connect(self._on_panel_combo_changed)
        ap_layout.addWidget(self._panel_combo, stretch=1)
        body_layout.addWidget(active_plot_box)

        body_layout.addWidget(self._build_overlay_group())
        body_layout.addWidget(self._build_thresholds_group())
        body_layout.addWidget(self._build_alarms_group())
        body_layout.addWidget(self._build_tags_group())
        body_layout.addWidget(self._build_dsp_group())
        body_layout.addWidget(self._build_formula_group())
        body_layout.addWidget(self._build_special_views_group())
        body_layout.addStretch()

        scroll.setWidget(body)
        outer.addWidget(scroll, stretch=1)

    # ── Section 1: Historical Overlay ─────────────────────────────────────────

    def _build_overlay_group(self) -> QGroupBox:
        box = QGroupBox("Historical Overlay")
        v   = QVBoxLayout(box)
        v.setSpacing(8)
        v.addWidget(QLabel(
            "Load a previously recorded CSV or Parquet file as a "
            "semi-transparent comparison overlay on the active plot."
        ))
        self._overlay_label = QLabel("No overlay loaded")
        self._overlay_label.setObjectName("subtle")
        v.addWidget(self._overlay_label)

        row = QHBoxLayout()
        btn_load = QPushButton("Load Overlay")
        btn_load.setObjectName("secondary")
        btn_load.clicked.connect(self._on_load_overlay)
        row.addWidget(btn_load)

        self._btn_clear_overlay = QPushButton("Clear")
        self._btn_clear_overlay.setObjectName("danger")
        self._btn_clear_overlay.setEnabled(False)
        self._btn_clear_overlay.clicked.connect(self._on_clear_overlay)
        row.addWidget(self._btn_clear_overlay)
        v.addLayout(row)
        return box

    def _on_load_overlay(self) -> None:
        if self._active_panel is None:
            QMessageBox.warning(self, "Overlay", "Select an active plot first.")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Overlay Data", "",
            "Data Files (*.csv *.tsv *.parquet *.pq);;All Files (*)",
        )
        if not path:
            return
        try:
            self._active_panel.load_overlay(path)
            self._refresh_overlay_state()
        except Exception as exc:
            QMessageBox.warning(self, "Overlay Error", str(exc))

    def _on_clear_overlay(self) -> None:
        if self._active_panel is not None:
            self._active_panel.clear_overlay()
            self._refresh_overlay_state()

    def _refresh_overlay_state(self) -> None:
        if self._active_panel is None or not self._active_panel._overlay_curves:
            self._overlay_label.setText("No overlay loaded")
            self._btn_clear_overlay.setEnabled(False)
        else:
            n = len(self._active_panel._overlay_curves)
            self._overlay_label.setText(f"Overlay active — {n} channel(s)")
            self._btn_clear_overlay.setEnabled(True)

    # ── Section 2: Visual Thresholds (unified X + Y) ──────────────────────────

    def _build_thresholds_group(self) -> QGroupBox:
        box = QGroupBox("Visual Thresholds")
        v   = QVBoxLayout(box)
        v.setSpacing(8)
        v.addWidget(QLabel(
            "Add horizontal (Y) or vertical (X) reference lines to the plot."
        ))

        self._thr_list = _ToggleListWidget()
        self._thr_list.setMaximumHeight(90)
        v.addWidget(self._thr_list)

        form = QFormLayout()
        form.setSpacing(6)

        self._thr_axis_combo = QComboBox()
        self._thr_axis_combo.addItem("Y-axis (horizontal line)", userData="y")
        self._thr_axis_combo.addItem("X-axis (vertical line)",   userData="x")
        self._thr_axis_combo.currentIndexChanged.connect(self._on_thr_axis_changed)
        form.addRow("Axis type:", self._thr_axis_combo)

        self._thr_yaxis_combo = QComboBox()
        form.addRow("Y-axis target:", self._thr_yaxis_combo)
        # Capture the label so it can be hidden in lockstep with the combo
        # when the user picks "X-axis (vertical line)".
        self._thr_yaxis_label = form.labelForField(self._thr_yaxis_combo)

        self._thr_value_spin = QDoubleSpinBox()
        self._thr_value_spin.setRange(-1e9, 1e9)
        self._thr_value_spin.setDecimals(4)
        self._thr_value_spin.setButtonSymbols(QAbstractSpinBox.NoButtons)
        form.addRow("Value:", self._thr_value_spin)

        self._thr_label_edit = QLineEdit()
        self._thr_label_edit.setPlaceholderText("e.g. Max Voltage")
        form.addRow("Label:", self._thr_label_edit)

        self._thr_color_chip = _ColorChip("#E05050")
        form.addRow("Color:", self._thr_color_chip)
        v.addLayout(form)

        row = QHBoxLayout()
        btn_add = QPushButton("Add Threshold")
        btn_add.setObjectName("secondary")
        btn_add.clicked.connect(self._on_add_threshold)
        row.addWidget(btn_add)
        btn_rm = QPushButton("Remove")
        btn_rm.setObjectName("danger")
        btn_rm.clicked.connect(self._on_remove_threshold)
        row.addWidget(btn_rm)
        v.addLayout(row)
        return box

    def _on_thr_axis_changed(self, _idx: int) -> None:
        show_yaxis = (self._thr_axis_combo.currentData() == "y")
        self._thr_yaxis_combo.setVisible(show_yaxis)
        if self._thr_yaxis_label is not None:
            self._thr_yaxis_label.setVisible(show_yaxis)

    def _refresh_thresholds(self) -> None:
        self._thr_list.clear()
        self._thr_yaxis_combo.clear()

        if self._active_panel is None:
            return

        y_axis_cfgs = getattr(self._active_panel, "_y_axis_cfgs", [])
        axis_sides  = getattr(self._active_panel, "_axis_sides", [])
        n_axes = max(len(y_axis_cfgs), 1)
        for i in range(n_axes):
            side = (axis_sides[i].capitalize() if i < len(axis_sides)
                    else ("Left" if i == 0 else "Right"))
            label = y_axis_cfgs[i].label if i < len(y_axis_cfgs) else ""
            text  = label.strip() if label and label.strip() else f"Axis {i} ({side})"
            # Always include the index so duplicates are still distinguishable.
            if label and label.strip():
                text = f"{label.strip()}  —  Axis {i} ({side})"
            self._thr_yaxis_combo.addItem(text, userData=i)

        for tid, cfg in self._active_panel._threshold_cfgs.items():
            item = QListWidgetItem(
                f"{cfg.label or '—'}  =  {cfg.value:.4g}  [Y{cfg.y_axis_idx}]"
            )
            item.setForeground(QColor(cfg.color))
            item.setData(Qt.UserRole, tid)
            self._thr_list.addItem(item)

        # X markers shown in same list with X prefix
        for mid, cfg in self._active_panel._x_marker_cfgs.items():
            item = QListWidgetItem(f"X  {cfg.label or '—'}  @  {cfg.x_value:.4g} s")
            item.setForeground(QColor(cfg.color))
            item.setData(Qt.UserRole, f"xm::{mid}")
            self._thr_list.addItem(item)

    def _on_add_threshold(self) -> None:
        if self._active_panel is None:
            return
        axis_type = self._thr_axis_combo.currentData()
        if axis_type == "y":
            from ui.plot_panel import ThresholdConfig
            cfg = ThresholdConfig(
                value=self._thr_value_spin.value(),
                color=self._thr_color_chip.color(),
                label=self._thr_label_edit.text().strip(),
                y_axis_idx=self._thr_yaxis_combo.currentData() or 0,
            )
            self._active_panel.add_threshold(cfg)
        else:
            from ui.plot_panel import XMarkerConfig
            cfg = XMarkerConfig(
                x_value=self._thr_value_spin.value(),
                color=self._thr_color_chip.color(),
                label=self._thr_label_edit.text().strip(),
            )
            self._active_panel.add_x_marker(cfg)
        self._refresh_thresholds()

    def _on_remove_threshold(self) -> None:
        if self._active_panel is None:
            return
        item = self._thr_list.currentItem()
        if item is None:
            return
        uid = item.data(Qt.UserRole)
        if isinstance(uid, str) and uid.startswith("xm::"):
            self._active_panel.remove_x_marker(uid[4:])
        else:
            self._active_panel.remove_threshold(uid)
        self._refresh_thresholds()

    # ── Section 3: Alarms & Notifications ────────────────────────────────────

    def _build_alarms_group(self) -> QGroupBox:
        box = QGroupBox("Alarms & Notifications")
        v   = QVBoxLayout(box)
        v.setSpacing(8)
        v.addWidget(QLabel(
            "Logic alarms fire when a channel crosses a threshold.\n"
            "A marker is dropped on the plot and an entry is logged below."
        ))

        self._alarm_list = _ToggleListWidget()
        self._alarm_list.setMaximumHeight(90)
        v.addWidget(self._alarm_list)

        form = QFormLayout()
        form.setSpacing(6)

        self._alarm_name_edit = QLineEdit()
        self._alarm_name_edit.setPlaceholderText("e.g. Overheat")
        form.addRow("Name:", self._alarm_name_edit)

        self._alarm_ch_combo = QComboBox()
        form.addRow("Channel:", self._alarm_ch_combo)

        self._alarm_op_combo = QComboBox()
        for op in (">", "<", ">=", "<=", "=="):
            self._alarm_op_combo.addItem(op)
        form.addRow("Operator:", self._alarm_op_combo)

        self._alarm_val_spin = QDoubleSpinBox()
        self._alarm_val_spin.setRange(-1e9, 1e9)
        self._alarm_val_spin.setDecimals(4)
        self._alarm_val_spin.setButtonSymbols(QAbstractSpinBox.NoButtons)
        form.addRow("Threshold:", self._alarm_val_spin)

        self._alarm_color_chip = _ColorChip("#E05050")
        form.addRow("Marker color:", self._alarm_color_chip)
        v.addLayout(form)

        row = QHBoxLayout()
        btn_add = QPushButton("Add Alarm")
        btn_add.setObjectName("secondary")
        btn_add.clicked.connect(self._on_add_alarm)
        row.addWidget(btn_add)
        btn_rm = QPushButton("Remove")
        btn_rm.setObjectName("danger")
        btn_rm.clicked.connect(self._on_remove_alarm)
        row.addWidget(btn_rm)
        v.addLayout(row)

        # Alarm log
        log_hdr = QLabel("Alarm Log")
        log_hdr.setObjectName("h3")
        v.addWidget(log_hdr)

        self._alarm_log = QTextEdit()
        self._alarm_log.setReadOnly(True)
        self._alarm_log.setMaximumHeight(120)
        self._alarm_log.setPlaceholderText("Alarm events appear here during streaming.")
        self._alarm_log.setStyleSheet(
            "background:#0D1A0D; color:#4CAF80; "
            "font-family: monospace; font-size: 11px; border-radius:4px;"
        )
        v.addWidget(self._alarm_log)

        btn_clear = QPushButton("Clear Log")
        btn_clear.setObjectName("quiet")
        btn_clear.clicked.connect(self._alarm_log.clear)
        v.addWidget(btn_clear)

        # Webhook notifications
        notif_hdr = QLabel("Push Notifications (Slack / Teams)")
        notif_hdr.setObjectName("h3")
        v.addWidget(notif_hdr)

        v.addWidget(QLabel(
            "Paste an incoming webhook URL.\n"
            "A message is sent automatically whenever an alarm fires."
        ))

        self._webhook_url_edit = QLineEdit()
        self._webhook_url_edit.setPlaceholderText(
            "https://hooks.slack.com/services/…  or  https://…webhook.office.com/…"
        )
        saved_url = self._prefs.value("webhook_url", "") or ""
        self._webhook_url_edit.setText(saved_url)
        self._webhook_url_edit.textChanged.connect(
            lambda t: self._prefs.setValue("webhook_url", t.strip())
        )
        v.addWidget(self._webhook_url_edit)

        btn_test = QPushButton("Send Test Message")
        btn_test.setObjectName("secondary")
        btn_test.clicked.connect(self._on_test_webhook)
        v.addWidget(btn_test)
        return box

    def webhook_url(self) -> str:
        return self._webhook_url_edit.text().strip()

    def _on_alarm_log_entry(
        self, panel_title: str, alarm_name: str,
        channel: str, value: float, ts: float,
    ) -> None:
        from datetime import datetime
        hms = datetime.fromtimestamp(ts % 86400).strftime("%H:%M:%S") if ts else "00:00:00"
        ms  = int((ts % 1) * 1000)
        entry = (
            f"[{hms}.{ms:03d}]  <b>{alarm_name}</b>  "
            f"({channel})  =  <b>{value:.4g}</b>  "
            f"<span style='color:#505050'>— {panel_title}</span>"
        )
        self._alarm_log.append(entry)

    def _on_test_webhook(self) -> None:
        url = self.webhook_url()
        if not url:
            QMessageBox.warning(self, "Webhook", "Paste a webhook URL first.")
            return
        from engine.notifications import send_test_webhook
        send_test_webhook(url)
        QMessageBox.information(
            self, "Webhook",
            "Test message sent. Check your Slack/Teams channel — "
            "it should arrive within a few seconds."
        )

    def _refresh_alarms(self) -> None:
        self._alarm_list.clear()
        self._alarm_ch_combo.clear()
        if self._active_panel is None:
            return
        for cfg in self._active_panel._channels.values():
            self._alarm_ch_combo.addItem(cfg.channel_name, userData=cfg.channel_name)
        for aid, alarm in self._active_panel._alarm_cfgs.items():
            item = QListWidgetItem(
                f"{alarm.name}  —  {alarm.channel_name} {alarm.operator} {alarm.threshold:.4g}"
            )
            item.setData(Qt.UserRole, aid)
            self._alarm_list.addItem(item)

    def _on_add_alarm(self) -> None:
        if self._active_panel is None:
            return
        from ui.plot_panel import AlarmConfig
        ch = self._alarm_ch_combo.currentData() or self._alarm_ch_combo.currentText()
        if not ch:
            QMessageBox.warning(self, "Alarm", "Select a channel first.")
            return
        cfg = AlarmConfig(
            name=self._alarm_name_edit.text().strip() or "Alarm",
            channel_name=ch,
            operator=self._alarm_op_combo.currentText(),
            threshold=self._alarm_val_spin.value(),
            color=self._alarm_color_chip.color(),
        )
        self._active_panel.add_alarm(cfg)
        self._refresh_alarms()

    def _on_remove_alarm(self) -> None:
        if self._active_panel is None:
            return
        item = self._alarm_list.currentItem()
        if item is None:
            return
        self._active_panel.remove_alarm(item.data(Qt.UserRole))
        self._refresh_alarms()

    # ── Section 4: Analysis Tags ───────────────────────────────────────────────

    def _build_tags_group(self) -> QGroupBox:
        box = QGroupBox("Analysis Tags")
        v   = QVBoxLayout(box)
        v.setSpacing(8)
        v.addWidget(QLabel(
            "Draggable shaded regions that mark anomalies or points of interest."
        ))

        self._tag_list = _ToggleListWidget()
        self._tag_list.setMaximumHeight(90)
        self._tag_list.currentRowChanged.connect(self._on_tag_selected)
        v.addWidget(self._tag_list)

        form = QFormLayout()
        form.setSpacing(6)

        self._tag_name_edit = QLineEdit()
        self._tag_name_edit.setPlaceholderText("e.g. Thermal Spike")
        form.addRow("Tag name:", self._tag_name_edit)

        self._tag_start_spin = QDoubleSpinBox()
        self._tag_start_spin.setRange(-1e12, 1e12)
        self._tag_start_spin.setDecimals(3)
        self._tag_start_spin.setButtonSymbols(QAbstractSpinBox.NoButtons)
        form.addRow("Start (s):", self._tag_start_spin)

        self._tag_end_spin = QDoubleSpinBox()
        self._tag_end_spin.setRange(-1e12, 1e12)
        self._tag_end_spin.setDecimals(3)
        self._tag_end_spin.setValue(1.0)
        self._tag_end_spin.setButtonSymbols(QAbstractSpinBox.NoButtons)
        form.addRow("End (s):", self._tag_end_spin)

        self._tag_color_chip = _ColorChip("#E0A030")
        form.addRow("Color:", self._tag_color_chip)
        v.addLayout(form)

        row = QHBoxLayout()
        btn_add = QPushButton("Add Tag Region")
        btn_add.setObjectName("secondary")
        btn_add.clicked.connect(self._on_add_tag)
        row.addWidget(btn_add)
        btn_rm = QPushButton("Remove")
        btn_rm.setObjectName("danger")
        btn_rm.clicked.connect(self._on_remove_tag)
        row.addWidget(btn_rm)
        v.addLayout(row)
        return box

    def _refresh_tags(self) -> None:
        self._tag_list.clear()
        if self._active_panel is None:
            return
        for tid, cfg in self._active_panel._tag_cfgs.items():
            item = QListWidgetItem(
                f"{cfg.name or '—'}  [{cfg.start:.3g} -> {cfg.end:.3g} s]"
            )
            item.setForeground(QColor(cfg.color))
            item.setData(Qt.UserRole, tid)
            self._tag_list.addItem(item)

    def _on_add_tag(self) -> None:
        if self._active_panel is None:
            return
        from ui.plot_panel import AnalysisTagConfig
        start = self._tag_start_spin.value()
        end   = self._tag_end_spin.value()
        if end <= start:
            end = start + 1.0
        cfg = AnalysisTagConfig(
            start=start, end=end,
            name=self._tag_name_edit.text().strip(),
            color=self._tag_color_chip.color(),
        )
        self._active_panel.add_analysis_tag(cfg)
        self._refresh_tags()

    def _on_remove_tag(self) -> None:
        if self._active_panel is None:
            return
        item = self._tag_list.currentItem()
        if item is None:
            return
        self._active_panel.remove_analysis_tag(item.data(Qt.UserRole))
        self._refresh_tags()

    def _on_tag_selected(self, _row: int) -> None:
        if self._active_panel is None:
            return
        item = self._tag_list.currentItem()
        if item is None:
            return
        cfg = self._active_panel._tag_cfgs.get(item.data(Qt.UserRole))
        if cfg is None:
            return
        self._tag_start_spin.blockSignals(True)
        self._tag_end_spin.blockSignals(True)
        self._tag_start_spin.setValue(cfg.start)
        self._tag_end_spin.setValue(cfg.end)
        self._tag_start_spin.blockSignals(False)
        self._tag_end_spin.blockSignals(False)

    # ── Section 5: DSP Functions (inline channel + range targeting) ────────────

    def _build_dsp_group(self) -> QGroupBox:
        box = QGroupBox("DSP Functions")
        v   = QVBoxLayout(box)
        v.setSpacing(8)

        # Inline targeting — channel + data range directly inside this group
        target_form = QFormLayout()
        target_form.setSpacing(6)

        self._channel_combo = QComboBox()
        self._channel_combo.setToolTip("Channel whose data is fed into the function.")
        target_form.addRow("Channel:", self._channel_combo)

        self._range_combo = QComboBox()
        self._range_combo.addItem("Entire Stream",   "all")
        self._range_combo.addItem("Current ViewBox", "viewbox")
        self._range_combo.addItem("Tag Region",      "tag")
        self._range_combo.currentIndexChanged.connect(self._on_range_changed)
        target_form.addRow("Data range:", self._range_combo)

        self._tag_combo = QComboBox()
        self._tag_combo.setVisible(False)
        target_form.addRow("Tag:", self._tag_combo)
        self._tag_label_widget = target_form.labelForField(self._tag_combo)
        if self._tag_label_widget:
            self._tag_label_widget.setVisible(False)

        v.addLayout(target_form)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color:#333;")
        v.addWidget(sep)

        top = QHBoxLayout()
        self._func_combo = QComboBox()
        for label, key in (
            ("Maximum",        "max"),
            ("Minimum",        "min"),
            ("Mean",           "mean"),
            ("Median",         "median"),
            ("Std Dev",        "std"),
            ("RMS",            "rms"),
            ("Peak Detection", "peaks"),
            ("Moving Average", "moving_avg"),
            ("Correlation",    "correlation"),
            ("Histogram",      "histogram"),
        ):
            self._func_combo.addItem(label, userData=key)
        self._func_combo.currentIndexChanged.connect(self._on_func_changed)
        top.addWidget(self._func_combo, stretch=1)

        btn_run = QPushButton("Run")
        btn_run.setObjectName("primary")
        btn_run.clicked.connect(self._on_run_dsp)
        top.addWidget(btn_run)
        v.addLayout(top)

        # Parameter sub-form
        self._param_form = QFormLayout()
        self._param_form.setSpacing(6)

        self._window_spin = QSpinBox()
        self._window_spin.setRange(2, 10_000)
        self._window_spin.setValue(10)
        self._param_form.addRow("Window size:", self._window_spin)

        self._hist_bins_spin = QSpinBox()
        self._hist_bins_spin.setRange(2, 500)
        self._hist_bins_spin.setValue(20)
        self._param_form.addRow("Histogram bins:", self._hist_bins_spin)

        self._corr_channel_combo = QComboBox()
        self._param_form.addRow("Channel B:", self._corr_channel_combo)

        v.addLayout(self._param_form)
        self._on_func_changed()

        self._results_table = QTableWidget(0, 2)
        self._results_table.setHorizontalHeaderLabels(["Metric", "Value"])
        self._results_table.horizontalHeader().setStretchLastSection(True)
        self._results_table.setMinimumHeight(320)
        self._results_table.setEditTriggers(QTableWidget.NoEditTriggers)
        v.addWidget(self._results_table)
        return box

    # ── Section 6: Custom Math Channel ────────────────────────────────────────

    def _build_formula_group(self) -> QGroupBox:
        box = QGroupBox("Custom Math Channel")
        v   = QVBoxLayout(box)
        v.setSpacing(8)
        v.addWidget(QLabel(
            "Write a formula using your channel names as variables.\n"
            "Channel names are sanitised (spaces -> underscores).\n"
            "Uses numexpr syntax: +, -, *, /, **, sqrt(), log(), etc."
        ))

        form = QFormLayout()
        form.setSpacing(6)

        self._formula_name_edit = QLineEdit()
        self._formula_name_edit.setPlaceholderText(
            "e.g.  Power_W  (leave blank to use the formula as name)"
        )
        form.addRow("Channel name:", self._formula_name_edit)

        self._formula_edit = QLineEdit()
        self._formula_edit.setPlaceholderText(
            "e.g.  voltage * current / 1000   or   sqrt(rpm**2 + torque**2)"
        )
        form.addRow("Formula:", self._formula_edit)

        self._formula_color = _ColorChip("#E0A030")
        form.addRow("Color:", self._formula_color)

        self._formula_axis_spin = QSpinBox()
        self._formula_axis_spin.setRange(0, 3)
        self._formula_axis_spin.setValue(0)
        self._formula_axis_spin.setToolTip(
            "Which Y-axis to plot on (0 = primary left, 1-3 = extra axes)."
        )
        form.addRow("Y-axis index:", self._formula_axis_spin)

        v.addLayout(form)

        btn_run_plot = QPushButton("Run & Plot")
        btn_run_plot.setObjectName("primary")
        btn_run_plot.setToolTip(
            "Evaluate the formula and draw it on the active plot."
        )
        btn_run_plot.clicked.connect(self._on_run_and_plot)
        v.addWidget(btn_run_plot)

        self._formula_result_label = QLabel("")
        self._formula_result_label.setObjectName("caption")
        self._formula_result_label.setWordWrap(True)
        v.addWidget(self._formula_result_label)
        return box

    # ── Section 7: Special Views ───────────────────────────────────────────────

    def _build_special_views_group(self) -> QGroupBox:
        box = QGroupBox("Special Views")
        v   = QVBoxLayout(box)
        v.setSpacing(8)

        fft_box   = QGroupBox("FFT and Spectrogram")
        fft_form  = QFormLayout(fft_box)
        fft_form.setSpacing(6)
        self._fft_channel_combo = QComboBox()
        self._fft_channel_combo.setToolTip(
            "Channel whose data is fed into the FFT and spectrogram."
        )
        fft_form.addRow("Channel:", self._fft_channel_combo)
        btn_fft = QPushButton("Open FFT Window")
        btn_fft.setObjectName("secondary")
        btn_fft.clicked.connect(self._on_open_fft)
        fft_form.addRow(btn_fft)
        v.addWidget(fft_box)

        d3_box  = QGroupBox("3-D Plot")
        d3_form = QFormLayout(d3_box)
        d3_form.setSpacing(6)
        self._3d_x_combo = QComboBox()
        self._3d_y_combo = QComboBox()
        self._3d_z_combo = QComboBox()
        d3_form.addRow("X:", self._3d_x_combo)
        d3_form.addRow("Y:", self._3d_y_combo)
        d3_form.addRow("Z:", self._3d_z_combo)
        btn_3d = QPushButton("Open 3-D Plot")
        btn_3d.setObjectName("secondary")
        btn_3d.clicked.connect(self._on_open_3d)
        d3_form.addRow(btn_3d)
        v.addWidget(d3_box)

        scope_box  = QGroupBox("Oscilloscope")
        scope_form = QFormLayout(scope_box)
        scope_form.setSpacing(6)
        self._scope_channel_combo = QComboBox()
        scope_form.addRow("Channel:", self._scope_channel_combo)
        btn_scope = QPushButton("Open Oscilloscope")
        btn_scope.setObjectName("secondary")
        btn_scope.clicked.connect(self._on_open_oscilloscope)
        scope_form.addRow(btn_scope)
        v.addWidget(scope_box)
        return box

    # ── Panel combo helpers ────────────────────────────────────────────────────

    def _rebuild_panel_combo(self) -> None:
        self._panel_combo.blockSignals(True)
        self._panel_combo.clear()
        if self._workspace:
            for panel in self._workspace.get_panels():
                self._panel_combo.addItem(panel.title, userData=panel)
            if self._active_panel in self._workspace.get_panels():
                idx = self._workspace.get_panels().index(self._active_panel)
                self._panel_combo.setCurrentIndex(idx)
            elif self._workspace.get_panels():
                self._active_panel = self._workspace.get_panels()[0]
                self._panel_combo.setCurrentIndex(0)
        self._panel_combo.blockSignals(False)
        self._refresh_annotation_panel_state()

    def _on_panel_combo_changed(self, idx: int) -> None:
        panel = self._panel_combo.itemData(idx)
        if panel is not None:
            self._active_panel = panel
            self._refresh_annotation_panel_state()

    def _refresh_annotation_panel_state(self) -> None:
        """Refresh all annotation UI elements for the newly active panel."""
        self._refresh_overlay_state()
        self._refresh_thresholds()
        self._refresh_alarms()
        self._refresh_tags()

    def refresh_annotation_state(self) -> None:
        """Public alias for _refresh_annotation_panel_state — called by AI handlers."""
        self._refresh_annotation_panel_state()

    # ── Channel combo helpers ──────────────────────────────────────────────────

    def _rebuild_channel_combos(self) -> None:
        combos = [
            self._channel_combo,
            self._corr_channel_combo,
            self._fft_channel_combo,
            self._3d_x_combo,
            self._3d_y_combo,
            self._3d_z_combo,
            self._scope_channel_combo,
        ]
        for cb in combos:
            cb.blockSignals(True)
            prev = cb.currentData()
            cb.clear()

        channels = self._collect_channels()
        for dev_path, ch_name in channels:
            ud = (dev_path, ch_name)
            for cb in combos:
                cb.addItem(ch_name, userData=ud)

        for cb in combos:
            for i in range(cb.count()):
                if cb.itemData(i) == prev:
                    cb.setCurrentIndex(i)
                    break
            cb.blockSignals(False)

    def _rebuild_tag_combo(self) -> None:
        self._tag_combo.clear()
        if self._workspace is None:
            return
        for panel in self._workspace.get_panels():
            for tid, cfg in panel._tag_cfgs.items():
                label = f"{cfg.name or '—'}  [{cfg.start:.2f}->{cfg.end:.2f} s]"
                self._tag_combo.addItem(label, userData=(panel, tid))

    def _collect_channels(self) -> List[Tuple[str, str]]:
        if self._workspace is None:
            return []
        return self._workspace.get_all_channels()

    # ── DSP slot handlers ──────────────────────────────────────────────────────

    def _on_range_changed(self, _idx: int) -> None:
        mode     = self._range_combo.currentData()
        show_tag = (mode == "tag")
        self._tag_combo.setVisible(show_tag)
        if self._tag_label_widget:
            self._tag_label_widget.setVisible(show_tag)
        if show_tag:
            self._rebuild_tag_combo()

    def _on_func_changed(self) -> None:
        key = self._func_combo.currentData()
        self._window_spin.setVisible(key == "moving_avg")
        self._hist_bins_spin.setVisible(key == "histogram")
        self._corr_channel_combo.setVisible(key == "correlation")
        for row in range(self._param_form.rowCount()):
            lbl = self._param_form.itemAt(row, QFormLayout.LabelRole)
            fld = self._param_form.itemAt(row, QFormLayout.FieldRole)
            if lbl and fld and fld.widget():
                vis = fld.widget().isVisible()
                if lbl.widget():
                    lbl.widget().setVisible(vis)

    def _get_xy(self, combo: QComboBox) -> Tuple[Optional[np.ndarray], Optional[np.ndarray]]:
        ud = combo.currentData()
        if not ud:
            return None, None
        dev_path, ch_name = ud

        # ── Live path: read from the device's ring buffer ──
        if self._session is not None:
            dev = self._session.get_device(dev_path)
            if dev is not None and dev.buffer is not None and dev.metadata is not None:
                data = dev.buffer.read_latest(500_000)
                if data.shape[1] > 0:
                    y_row = next(
                        (i + 1 for i, ch in enumerate(dev.metadata.channels)
                         if ch.name == ch_name),
                        None,
                    )
                    if y_row is not None:
                        return self._apply_range(data[0], data[y_row])

        # ── Post-analysis fallback: pull from any panel's overlay_data ──
        # overlay_data is keyed by the raw CSV column ("Voltage[V]"); strip
        # the [unit] suffix when comparing against the bare channel name.
        if self._workspace is not None:
            for panel in self._workspace.get_panels():
                overlay = getattr(panel, "_overlay_data", None) or {}
                for col, (x_arr, y_arr) in overlay.items():
                    bare = col.split("[", 1)[0].strip() if "[" in col else col
                    if bare == ch_name:
                        return self._apply_range(x_arr, y_arr)

        return None, None

    def _apply_range(
        self, x: np.ndarray, y: np.ndarray
    ) -> Tuple[np.ndarray, np.ndarray]:
        mode = self._range_combo.currentData()
        if mode == "viewbox" and self._workspace:
            panels = self._workspace.get_panels()
            if panels:
                try:
                    vb_range = panels[0]._plot_item.vb.viewRange()[0]
                    return slice_by_range(x, y, "viewbox", vb_xrange=tuple(vb_range))
                except Exception:
                    pass
        elif mode == "tag":
            ud = self._tag_combo.currentData()
            if ud:
                panel, tid = ud
                cfg = panel._tag_cfgs.get(tid)
                if cfg:
                    return slice_by_range(x, y, "tag", tag_start=cfg.start, tag_end=cfg.end)
        return x, y

    def run_dsp_for_channel(
        self, fn_key: str, ch_name: str
    ) -> Optional[Dict[str, float]]:
        """Run a DSP function on a named channel and return scalar results.

        Returns None when the channel has no data.
        """
        # Point the channel combo at the requested channel.
        for i in range(self._channel_combo.count()):
            if self._channel_combo.itemText(i) == ch_name:
                self._channel_combo.setCurrentIndex(i)
                break

        x, y = self._get_xy(self._channel_combo)
        if y is None or len(y) == 0:
            return None

        results: Dict[str, float] = {}
        fn = fn_key.lower()
        if fn == "mean":
            results["Mean"] = compute_mean(y)
        elif fn == "median":
            results["Median"] = compute_median(y)
        elif fn in ("std", "stddev"):
            results["Std Dev"] = compute_std(y)
        elif fn == "rms":
            results["RMS"] = compute_rms(y)
        elif fn in ("max", "maximum"):
            results["Maximum"] = compute_max(y)
        elif fn in ("min", "minimum"):
            results["Minimum"] = compute_min(y)
        elif fn in ("peak", "peaks"):
            results.update(compute_peaks(y, x))
        elif fn == "moving_avg":
            w   = int(self._window_spin.value()) if hasattr(self, "_window_spin") else 10
            out = compute_moving_average(y, window=w)
            results["MA Mean"] = float(np.mean(out))
            results["window"]  = float(w)
        elif fn == "histogram":
            bins = int(self._hist_bins_spin.value()) if hasattr(self, "_hist_bins_spin") else 10
            results["Mean"]    = compute_mean(y)
            results["Std Dev"] = compute_std(y)
            results["bins"]    = float(bins)
        elif fn == "correlation":
            xb, yb = self._get_xy(self._corr_channel_combo)
            if yb is not None and len(yb) >= 2:
                results.update(compute_correlation(y, yb))
        else:
            return None

        results["n_samples"] = float(len(y))
        if len(x) > 1:
            results["duration_s"] = float(x[-1] - x[0])
        return results

    def _on_run_dsp(self) -> None:
        x, y = self._get_xy(self._channel_combo)
        if y is None or len(y) == 0:
            QMessageBox.warning(self, "Analysis",
                                "No data. Connect a device and stream first.")
            return
        key     = self._func_combo.currentData()
        results: Dict[str, float] = {}

        if key == "max":
            results["Maximum"] = compute_max(y)
        elif key == "min":
            results["Minimum"] = compute_min(y)
        elif key == "mean":
            results["Mean"] = compute_mean(y)
        elif key == "median":
            results["Median"] = compute_median(y)
        elif key == "std":
            results["Std Dev"] = compute_std(y)
        elif key == "rms":
            results["RMS"] = compute_rms(y)
        elif key == "peaks":
            results.update(compute_peaks(y, x))
        elif key == "moving_avg":
            w   = self._window_spin.value()
            out = compute_moving_average(y, window=w)
            self._show_array_result(x, out, title=f"Moving Average (window={w})", color="#81C784")
            results["window"]      = float(w)
            results["output_mean"] = float(np.mean(out))
        elif key == "correlation":
            xb, yb = self._get_xy(self._corr_channel_combo)
            if yb is None or len(yb) < 2:
                QMessageBox.warning(self, "Correlation", "Select Channel B with data.")
                return
            results.update(compute_correlation(y, yb))
        elif key == "histogram":
            bins            = self._hist_bins_spin.value()
            centers, counts = compute_histogram(y, bins=bins)
            self._show_bar_result(centers, counts, title=f"Histogram ({bins} bins)")
            results["bins"] = float(bins)
            results["mean"] = compute_mean(y)
            results["std"]  = compute_std(y)

        results["n_samples"]  = float(len(y))
        if len(x) > 1:
            results["duration_s"] = float(x[-1] - x[0])
        self._populate_table(results)

    # ── Formula handlers ───────────────────────────────────────────────────────

    def _build_channel_dict(self) -> Dict[str, np.ndarray]:
        """Build {sanitised_channel_name: y_array} for the formula engine.

        Pulls from live device buffers when a session is connected; falls
        back to each panel's overlay data so custom-math formulas still work
        in post-analysis.
        """
        result: Dict[str, np.ndarray] = {}
        if not self._workspace:
            return result

        # ── Live source ──
        if self._session is not None:
            for dev_path, ch_name in self._workspace.get_all_channels():
                dev = self._session.get_device(dev_path)
                if dev is None or dev.buffer is None or dev.metadata is None:
                    continue
                data = dev.buffer.read_latest(500_000)
                if data.shape[1] == 0:
                    continue
                x = data[0]
                for i, ch in enumerate(dev.metadata.channels):
                    if ch.name == ch_name:
                        _, ys = self._apply_range(x, data[i + 1])
                        result[sanitize_name(ch_name)] = ys
                        break

        # ── Overlay fallback (post-analysis) ──
        for panel in self._workspace.get_panels():
            overlay = getattr(panel, "_overlay_data", None) or {}
            for col, (x_arr, y_arr) in overlay.items():
                bare = col.split("[", 1)[0].strip() if "[" in col else col
                key  = sanitize_name(bare)
                # Don't clobber live data with overlay data.
                if key in result:
                    continue
                _, ys = self._apply_range(x_arr, y_arr)
                result[key] = ys
        return result

    def _on_run_and_plot(self) -> None:
        formula = self._formula_edit.text().strip()
        color   = self._formula_color.color()
        if not formula:
            return

        # Target the active plot the user picked in the "Active plot" combo;
        # fall back to the first panel only if nothing is active.
        if not self._workspace:
            return
        panels = self._workspace.get_panels()
        if not panels:
            return
        panel = self._active_panel if self._active_panel in panels else panels[0]

        # Use the user-specified channel name if provided; fall back to formula.
        name = self._formula_name_edit.text().strip() or formula

        ch_data = self._build_channel_dict()
        if not ch_data:
            QMessageBox.warning(self, "Formula Error",
                "No channel data available. Connect a device and stream data first.")
            return

        try:
            result = evaluate_formula(formula, ch_data)
        except KeyError as exc:
            missing = str(exc).strip("'\"")
            available = ", ".join(f"'{k}'" for k in ch_data)
            QMessageBox.critical(
                self, "Formula Error — Unknown Channel",
                f"Channel '{missing}' was not found.\n\n"
                f"Available channels: {available or '(none)'}\n\n"
                "Check spelling. Channel names are sanitised: spaces become underscores."
            )
            return
        except SyntaxError as exc:
            QMessageBox.critical(
                self, "Formula Error — Syntax",
                f"The formula contains a syntax error:\n\n{exc}\n\n"
                "Use numexpr syntax, e.g.  voltage * current / 1000"
            )
            return
        except Exception as exc:
            QMessageBox.critical(self, "Formula Error", str(exc))
            return

        mn  = float(np.min(result))
        mx  = float(np.max(result))
        rms = float(np.sqrt(np.mean(result ** 2)))
        self._formula_result_label.setText(
            f"{len(result):,} samples  |  min {mn:.4g}  max {mx:.4g}  rms {rms:.4g}"
        )
        self._formula_result_label.setStyleSheet("color:#4CAF80;")

        x = None
        # Try live first
        if self._session is not None:
            for dev_path, ch_name in self._workspace.get_all_channels():
                dev = self._session.get_device(dev_path)
                if dev and dev.buffer:
                    data = dev.buffer.read_latest(500_000)
                    if data.shape[1] > 0:
                        xs, _ = self._apply_range(data[0], data[1])
                        x = xs[:len(result)]
                        break
        # Post-analysis fallback: borrow the target panel's overlay X first,
        # then any panel's overlay if that one has none.
        if x is None:
            overlay = getattr(panel, "_overlay_data", None) or {}
            if overlay:
                x_arr, _ = next(iter(overlay.values()))
                x = x_arr[:len(result)]
        if x is None:
            for p in self._workspace.get_panels():
                overlay = getattr(p, "_overlay_data", None) or {}
                if overlay:
                    x_arr, _ = next(iter(overlay.values()))
                    x = x_arr[:len(result)]
                    break
        if x is None:
            x = np.arange(len(result), dtype=float)

        # Use the PlotPanel helper so legend dedup, curve ownership, and
        # the panel-side formula registry all stay consistent. This is also
        # what restore_config calls to replay formulas in post-analysis.
        y_axis_idx = self._formula_axis_spin.value()
        panel.add_formula_curve(name, formula, color, x[:len(result)], result, y_axis_idx=y_axis_idx)
        self._formula_result_label.setText(
            self._formula_result_label.text() + f"  |  plotted on '{panel.title}'"
        )

    # ── Result display helpers ─────────────────────────────────────────────────

    def _populate_table(self, data: Dict[str, float]) -> None:
        self._results_table.setRowCount(0)
        for metric, val in data.items():
            r = self._results_table.rowCount()
            self._results_table.insertRow(r)
            self._results_table.setItem(r, 0, QTableWidgetItem(metric))
            self._results_table.setItem(
                r, 1, QTableWidgetItem(f"{val:.6g}" if val == val else "NaN")
            )

    def _show_array_result(
        self, x: np.ndarray, y_out: np.ndarray, title: str, color: str = "#4FC3F7"
    ) -> None:
        win = pg.PlotWidget(title=title)
        win.setWindowTitle(title)
        win.setWindowFlags(Qt.Window)
        win.setBackground("#181818")
        win.plot(x[:len(y_out)], y_out, pen=pg.mkPen(color, width=1.5))
        win.resize(640, 320)
        win.show()
        win.setAttribute(Qt.WA_DeleteOnClose, True)
        self._result_windows.append(win)

    def _show_bar_result(self, centers: np.ndarray, counts: np.ndarray, title: str) -> None:
        win = pg.plot(title=title)
        win.setWindowTitle(title)
        win.setBackground("#181818")
        width = (centers[1] - centers[0]) * 0.8 if len(centers) > 1 else 1.0
        win.addItem(pg.BarGraphItem(x=centers, height=counts, width=width, brush="#C8C8C8"))
        win.resize(640, 320)
        self._result_windows.append(win)

    # ── Special views launchers ────────────────────────────────────────────────

    def _on_open_fft(self) -> None:
        x, y = self._get_xy(self._fft_channel_combo)
        if y is None or len(y) < 4:
            QMessageBox.warning(self, "FFT", "No channel data available.")
            return
        ud = self._fft_channel_combo.currentData()
        ch_name = ud[1] if ud else ""
        if self._fft_window is None:
            self._fft_window = FFTWindow()
        self._fft_window.update_data(x, y, ch_name)
        self._fft_window.show()
        self._fft_window.raise_()

    def _on_open_3d(self) -> None:
        ya = self._get_xy(self._3d_x_combo)[1]
        yb = self._get_xy(self._3d_y_combo)[1]
        yc = self._get_xy(self._3d_z_combo)[1]
        if any(v is None for v in (ya, yb, yc)):
            QMessageBox.warning(self, "3-D Plot",
                "Assign three channels and stream data first.")
            return
        if self._3d_window is None:
            self._3d_window = ThreeDWindow()
        self._3d_window.update_data(ya, yb, yc,
            self._3d_x_combo.currentText(),
            self._3d_y_combo.currentText(),
            self._3d_z_combo.currentText())
        self._3d_window.show()
        self._3d_window.raise_()

    def _on_open_oscilloscope(self) -> None:
        if self._scope_window is None:
            self._scope_window = OscilloscopeWindow()
        ud = self._scope_channel_combo.currentData()
        if ud and self._session:
            dev_path, ch_name = ud
            self._scope_window.attach_session(self._session, dev_path, ch_name)
        self._scope_window.show()
        self._scope_window.raise_()

    # ── Serialization ──────────────────────────────────────────────────────────

    def to_config(self) -> dict:
        scope_cfg = {}
        if self._scope_window is not None:
            scope_cfg = self._scope_window.to_config()
        return {
            "oscilloscope":      scope_cfg,
            "last_dsp_function": self._func_combo.currentData() or "mean",
        }

    def from_config(self, d: dict) -> None:
        last_fn = d.get("last_dsp_function", "mean")
        for i in range(self._func_combo.count()):
            if self._func_combo.itemData(i) == last_fn:
                self._func_combo.setCurrentIndex(i)
                break
        scope_cfg = d.get("oscilloscope", {})
        if scope_cfg and self._scope_window is not None:
            self._scope_window.from_config(scope_cfg)
