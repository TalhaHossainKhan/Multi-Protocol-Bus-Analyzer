"""Signal Mapping modal.

Spawned automatically after a device's metadata handshake completes,
*before* the user is dropped into the workspace.  Lets the user decide
how the device's channels should be visualised:

  • Number of plots (1 – 4)
  • X-axis source (Timestamp by default; any channel may be picked)
  • For every channel:
       – whether to include it in the plot (Plot checkbox)
       – whether to surface it as a digital readout (DRO checkbox)
       – which plot it belongs to
       – which Y-axis within that plot
       – auto-scale (using the MCU-reported range) or manual min/max override

The dialog reads ``range_min`` and ``range_max`` from every
``ChannelSpecV2`` in the supplied ``DeviceMetadataV2`` and pre-fills the
manual override boxes with those values.

Output
------
``result_layout()`` returns a list of plot-config dicts (only channels
where "Plot" is ticked).
``result_digital_readouts()`` returns channel names where "DRO" is ticked.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from engine.protocol import DeviceMetadataV2
from ui.settings_panel import _AxisSideDialog, pick_axis_via_menu

_PALETTE = (
    "#06b6d4", "#f59e0b", "#10b981", "#ec4899",
    "#a78bfa", "#22d3ee", "#fb923c", "#84cc16",
    "#f43f5e", "#8b5cf6", "#14b8a6", "#eab308",
)


# ── A single per-channel mapping row ──────────────────────────────────────────

class _ChannelRow(QFrame):
    """One row in the channel-mapping table."""

    def __init__(self, ch_idx: int, name: str, unit: str,
                 range_min: float, range_max: float,
                 n_plots: int, default_color: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self._ch_idx = ch_idx
        self._name   = name
        self._unit   = unit

        self._mcu_min = range_min if not math.isnan(range_min) else None
        self._mcu_max = range_max if not math.isnan(range_max) else None

        layout = QGridLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setHorizontalSpacing(14)
        layout.setVerticalSpacing(6)

        # ── Row 0: channel name + MCU range + mode checkboxes ──
        title = QLabel(f"<b>{name}</b>" + (f" <span style='color:#8b8b8b'>({unit})</span>"
                                            if unit else ""))
        title.setTextFormat(Qt.RichText)
        layout.addWidget(title, 0, 0, 1, 2)

        mcu_str = (
            f"MCU range: {self._mcu_min:.3g} → {self._mcu_max:.3g}"
            if self._mcu_min is not None and self._mcu_max is not None
            else "MCU range: (not specified)"
        )
        mcu_lbl = QLabel(mcu_str)
        mcu_lbl.setObjectName("caption")
        layout.addWidget(mcu_lbl, 0, 2, 1, 1)

        # Mode checkboxes — right-aligned in the header row.
        mode_widget = QWidget()
        mode_layout = QHBoxLayout(mode_widget)
        mode_layout.setContentsMargins(0, 0, 0, 0)
        mode_layout.setSpacing(12)

        self._plot_cb = QCheckBox("Plot")
        self._plot_cb.setChecked(True)
        self._plot_cb.setToolTip(
            "Include this channel in the plot panel.\n"
            "Uncheck to hide from the chart (DRO only)."
        )
        mode_layout.addWidget(self._plot_cb)

        self._dro_cb = QCheckBox("Live Value")
        self._dro_cb.setChecked(False)
        self._dro_cb.setToolTip(
            "Show this channel as a large numeric readout\n"
            "in the Live Values panel below the plots."
        )
        mode_layout.addWidget(self._dro_cb)
        layout.addWidget(mode_widget, 0, 3, 1, 1)

        # ── Row 1: Plot + Y-axis ──
        self._r1_plot_lbl = QLabel("Plot:")
        layout.addWidget(self._r1_plot_lbl, 1, 0)
        self._plot_combo = QComboBox()
        for p in range(n_plots):
            self._plot_combo.addItem(f"Plot {p + 1}", userData=p)
        layout.addWidget(self._plot_combo, 1, 1)

        self._r1_axis_lbl = QLabel("Y-Axis:")
        layout.addWidget(self._r1_axis_lbl, 1, 2)
        self._yaxis_combo = QComboBox()
        self._yaxis_combo.setToolTip(
            "Which Y-axis to plot this channel against.\n"
            "Multiple channels can share the same axis.\n"
            "Add more Y-axes with the '+ Add Y-Axis' button below."
        )
        layout.addWidget(self._yaxis_combo, 1, 3)

        # ── Row 2: colour + autoscale toggle ──
        self._r2_color_lbl = QLabel("Colour:")
        layout.addWidget(self._r2_color_lbl, 2, 0)
        self._color_btn = QPushButton()
        self._color = default_color
        self._refresh_color_button()
        self._color_btn.clicked.connect(self._pick_color)
        self._color_btn.setFixedWidth(80)
        layout.addWidget(self._color_btn, 2, 1)

        self._autoscale_cb = QCheckBox("Auto-scale (use MCU range)")
        self._autoscale_cb.setChecked(True)
        self._autoscale_cb.toggled.connect(self._on_autoscale_toggled)
        layout.addWidget(self._autoscale_cb, 2, 2, 1, 2)

        # ── Row 3: manual min / max (hidden when auto-scale is on) ──
        self._min_label = QLabel("Min:")
        layout.addWidget(self._min_label, 3, 0)
        self._min_spin = QDoubleSpinBox()
        self._min_spin.setRange(-1e9, 1e9)
        self._min_spin.setDecimals(4)
        self._min_spin.setValue(self._mcu_min if self._mcu_min is not None else 0.0)
        layout.addWidget(self._min_spin, 3, 1)

        self._max_label = QLabel("Max:")
        layout.addWidget(self._max_label, 3, 2)
        self._max_spin = QDoubleSpinBox()
        self._max_spin.setRange(-1e9, 1e9)
        self._max_spin.setDecimals(4)
        self._max_spin.setValue(self._mcu_max if self._mcu_max is not None else 100.0)
        layout.addWidget(self._max_spin, 3, 3)

        self._on_autoscale_toggled(True)   # apply initial visibility

        # Collect all the plot-specific widgets so we can hide them in bulk.
        self._plot_widgets = [
            self._r1_plot_lbl, self._plot_combo,
            self._r1_axis_lbl, self._yaxis_combo,
            self._r2_color_lbl, self._color_btn,
            self._autoscale_cb,
        ]
        self._plot_cb.toggled.connect(self._on_plot_toggled)

    # ── Visibility management ─────────────────────────────────────────────────

    def _on_plot_toggled(self, on: bool) -> None:
        for w in self._plot_widgets:
            w.setVisible(on)
        # Min/Max rows are a sub-toggle; only show when both conditions hold.
        if on:
            self._on_autoscale_toggled(self._autoscale_cb.isChecked())
        else:
            for w in (self._min_label, self._min_spin,
                      self._max_label, self._max_spin):
                w.setVisible(False)

    # ── Color picker ─────────────────────────────────────────────────────────

    def _refresh_color_button(self) -> None:
        self._color_btn.setStyleSheet(
            f"background-color: {self._color}; border: 1px solid #3a3a3a; "
            f"min-height: 22px; border-radius: 4px;"
        )
        self._color_btn.setText("")

    def _pick_color(self) -> None:
        from PySide6.QtGui import QColor
        from PySide6.QtWidgets import QColorDialog
        c = QColorDialog.getColor(QColor(self._color), self)
        if c.isValid():
            self._color = c.name()
            self._refresh_color_button()

    def _on_autoscale_toggled(self, on: bool) -> None:
        for w in (self._min_label, self._min_spin,
                  self._max_label, self._max_spin):
            w.setVisible(not on)

    # ── Public API ───────────────────────────────────────────────────────────

    def update_plot_count(self, n_plots: int) -> None:
        current = self._plot_combo.currentData() or 0
        self._plot_combo.clear()
        for p in range(n_plots):
            self._plot_combo.addItem(f"Plot {p + 1}", userData=p)
        self._plot_combo.setCurrentIndex(min(current, n_plots - 1))

    def update_yaxis_count(self, n_axes: int) -> None:
        current = self._yaxis_combo.currentData() or 0
        self._yaxis_combo.clear()
        for a in range(n_axes):
            label = "Axis 0 (Left)" if a == 0 else f"Axis {a} (Right-{a})"
            self._yaxis_combo.addItem(label, userData=a)
        self._yaxis_combo.setCurrentIndex(min(current, n_axes - 1))

    def channel_name(self) -> str:
        return self._name

    def include_in_plot(self) -> bool:
        return self._plot_cb.isChecked()

    def show_as_dro(self) -> bool:
        return self._dro_cb.isChecked()

    def plot_idx(self) -> int:
        return self._plot_combo.currentData() or 0

    def y_axis_idx(self) -> int:
        return self._yaxis_combo.currentData() or 0

    def color(self) -> str:
        return self._color

    def autoscale(self) -> bool:
        return self._autoscale_cb.isChecked()

    def y_min(self) -> Optional[float]:
        if self.autoscale():
            return None
        return self._min_spin.value()

    def y_max(self) -> Optional[float]:
        if self.autoscale():
            return None
        return self._max_spin.value()


# ── The dialog ────────────────────────────────────────────────────────────────

class SignalMappingDialog(QDialog):
    """Modal shown immediately after the MCU handshake completes."""

    def __init__(self, device_path: str, device_label: str,
                 metadata: DeviceMetadataV2, parent=None) -> None:
        super().__init__(parent)
        self._device_path = device_path
        self._device_label = device_label
        self._metadata = metadata
        self._channel_rows: List[_ChannelRow] = []
        _default = max(1, metadata.num_channels)
        self._axis_sides_per_plot: Dict[int, List[str]] = {
            0: ["left"] + ["right"] * (_default - 1)
        }
        self._plot_titles: Dict[int, str] = {}
        self._plot_title_edits: Dict[int, "QLineEdit"] = {}

        self.setWindowTitle("Signal Mapping")
        self.setModal(True)

        # Size the dialog to fit the screen, leaving a comfortable margin.
        screen = QGuiApplication.primaryScreen().availableGeometry()
        self.resize(
            min(960, screen.width() - 80),
            min(860, screen.height() - 60),
        )
        self.setMinimumSize(820, 600)

        self._build_ui()

    def _n_axes(self, p_idx: int) -> int:
        return len(self._axis_sides_per_plot.get(p_idx, ["left"]))

    # ── UI construction ──────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # Outer layout: scroll area fills the dialog, button row pins to bottom.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Scrollable body ──
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        body_widget = QWidget()
        body = QVBoxLayout(body_widget)
        body.setContentsMargins(22, 20, 22, 16)
        body.setSpacing(14)

        # Header
        header = QLabel("Map Your Signals")
        header.setObjectName("h1")
        body.addWidget(header)

        sub = QLabel(
            f"<b>{self._device_label}</b> reported "
            f"<b>{self._metadata.num_channels}</b> channel(s).  "
            f"Use the checkboxes on each channel to choose whether it appears "
            f"in the <b>plot</b>, as a <b>live readout</b>, or both."
        )
        sub.setObjectName("subtle")
        sub.setWordWrap(True)
        sub.setTextFormat(Qt.RichText)
        body.addWidget(sub)

        # ── Global controls ──
        global_box = QGroupBox("Workspace layout")
        gb_layout = QGridLayout(global_box)
        gb_layout.setColumnStretch(3, 1)

        gb_layout.addWidget(QLabel("Number of plots:"), 0, 0)
        self._plot_count_spin = QSpinBox()
        self._plot_count_spin.setRange(1, 4)
        self._plot_count_spin.setValue(1)
        self._plot_count_spin.valueChanged.connect(self._on_plot_count_changed)
        gb_layout.addWidget(self._plot_count_spin, 0, 1)

        gb_layout.addWidget(QLabel("X-axis source:"), 1, 0)
        self._x_combo = QComboBox()
        self._x_combo.addItem("⏱  Timestamp (PC_Time_s)", userData=(None, None))
        for ch in self._metadata.channels:
            disp = ch.name + (f" ({ch.unit})" if ch.unit else "")
            self._x_combo.addItem(disp, userData=(self._device_path, ch.name))
        gb_layout.addWidget(self._x_combo, 1, 1, 1, 3)

        self._axes_container = QWidget()
        self._axes_container_layout = QVBoxLayout(self._axes_container)
        self._axes_container_layout.setContentsMargins(0, 0, 0, 0)
        self._axes_container_layout.setSpacing(4)
        gb_layout.addWidget(self._axes_container, 2, 0, 1, 4)

        body.addWidget(global_box)

        # ── Channel rows (no nested scroll — outer scroll handles it) ──
        channels_box = QGroupBox("Channels")
        ch_layout = QVBoxLayout(channels_box)
        ch_layout.setSpacing(8)
        ch_layout.setContentsMargins(8, 8, 8, 8)

        for i, ch in enumerate(self._metadata.channels):
            color = _PALETTE[i % len(_PALETTE)]
            row = _ChannelRow(
                ch_idx=i,
                name=ch.name,
                unit=ch.unit,
                range_min=ch.range_min,
                range_max=ch.range_max,
                n_plots=self._plot_count_spin.value(),
                default_color=color,
            )
            n0 = self._n_axes(0)
            row.update_yaxis_count(n0)
            row._yaxis_combo.setCurrentIndex(min(i, n0 - 1))
            row._plot_combo.currentIndexChanged.connect(
                lambda p_idx, r=row: r.update_yaxis_count(self._n_axes(p_idx))
            )
            ch_layout.addWidget(row)
            self._channel_rows.append(row)

        body.addWidget(channels_box)

        # Seed defaults and build axis rows.
        self._plot_titles.setdefault(0, self._default_plot_title(0))
        self._rebuild_axis_rows(self._plot_count_spin.value())

        body.addStretch(1)
        scroll.setWidget(body_widget)
        outer.addWidget(scroll, stretch=1)

        # ── Button row (pinned outside the scroll) ──
        btn_row = QDialogButtonBox(QDialogButtonBox.Ok)
        btn_row.button(QDialogButtonBox.Ok).setText("Continue →")
        btn_row.button(QDialogButtonBox.Ok).setObjectName("primary")
        btn_row.accepted.connect(self.accept)

        btn_container = QWidget()
        btn_container.setObjectName("")
        btn_cl = QHBoxLayout(btn_container)
        btn_cl.setContentsMargins(22, 10, 22, 14)
        btn_cl.addStretch()
        btn_cl.addWidget(btn_row)

        # Thin separator line above the button row.
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #2C2C2C; background: #2C2C2C; max-height: 1px;")
        outer.addWidget(sep)
        outer.addWidget(btn_container)

    def _on_plot_count_changed(self, n: int) -> None:
        for row in self._channel_rows:
            row.update_plot_count(n)
        for p_idx in range(n):
            self._axis_sides_per_plot.setdefault(
                p_idx,
                ["left"] + ["right"] * max(0, self._metadata.num_channels - 1),
            )
        self._rebuild_axis_rows(n)

    def _rebuild_axis_rows(self, n_plots: int) -> None:
        layout = self._axes_container_layout
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._plot_title_edits.clear()

        for p_idx in range(n_plots):
            sides = self._axis_sides_per_plot.get(p_idx, ["left"])

            title_row = QWidget()
            title_layout = QHBoxLayout(title_row)
            title_layout.setContentsMargins(0, 0, 0, 0)
            title_layout.setSpacing(8)
            title_layout.addWidget(QLabel(f"Plot {p_idx + 1} title:"))
            title_edit = QLineEdit()
            title_edit.setPlaceholderText(self._default_plot_title(p_idx))
            title_edit.setText(self._plot_titles.get(p_idx, ""))
            title_edit.textChanged.connect(
                lambda txt, pi=p_idx: self._plot_titles.__setitem__(pi, txt)
            )
            title_layout.addWidget(title_edit, 1)
            self._plot_title_edits[p_idx] = title_edit
            layout.addWidget(title_row)

            row_widget = QWidget()
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(8)

            prefix = f"Plot {p_idx + 1} Y-axes:" if n_plots > 1 else "Y-axes per plot:"
            row_layout.addWidget(QLabel(prefix))

            lbl = QLabel(self._axis_label_text(sides))
            lbl.setObjectName("subtle")
            row_layout.addWidget(lbl, 1)

            btn_add = QPushButton("+ Add Y-Axis")
            btn_add.setObjectName("secondary")
            btn_add.setToolTip("Add a new Y-axis on the left or right of this plot.")
            btn_add.clicked.connect(lambda _, pi=p_idx, lb=lbl: self._on_add_yaxis(pi, lb))
            row_layout.addWidget(btn_add)

            btn_rem = QPushButton("− Remove")
            btn_rem.setObjectName("danger")
            btn_rem.setToolTip("Pick which Y-axis to remove from this plot.")
            btn_rem.clicked.connect(
                lambda _, pi=p_idx, lb=lbl, bt=btn_rem: self._on_remove_yaxis(pi, lb, bt)
            )
            row_layout.addWidget(btn_rem)

            layout.addWidget(row_widget)

    def _on_add_yaxis(self, p_idx: int, label_widget: QLabel) -> None:
        sides = self._axis_sides_per_plot.setdefault(p_idx, ["left"])
        if len(sides) >= 8:
            return
        side = _AxisSideDialog.ask(self)
        if side is None:
            return
        sides.append(side)
        label_widget.setText(self._axis_label_text(sides))
        for row in self._channel_rows:
            if row.plot_idx() == p_idx:
                row.update_yaxis_count(len(sides))

    def _on_remove_yaxis(
        self, p_idx: int, label_widget: QLabel, button: QPushButton
    ) -> None:
        sides = self._axis_sides_per_plot.get(p_idx, ["left"])
        if len(sides) <= 1:
            return
        choices: List[Tuple[int, str]] = [
            (i, f"Axis {i} ({sides[i].capitalize()})") for i in range(1, len(sides))
        ]
        idx = pick_axis_via_menu(button, choices)
        if idx is None:
            return
        for row in self._channel_rows:
            if row.plot_idx() != p_idx:
                continue
            cur = row.y_axis_idx()
            if cur == idx:
                row._yaxis_combo.setCurrentIndex(0)
            elif cur > idx:
                row._yaxis_combo.setCurrentIndex(cur - 1)
        sides.pop(idx)
        label_widget.setText(self._axis_label_text(sides))
        for row in self._channel_rows:
            if row.plot_idx() == p_idx:
                row.update_yaxis_count(len(sides))

    def _default_plot_title(self, p_idx: int) -> str:
        label = self._device_label or "Device"
        return f"Plot {p_idx + 1} · {label}"

    def _resolved_plot_title(self, p_idx: int) -> str:
        typed = (self._plot_titles.get(p_idx) or "").strip()
        return typed or self._default_plot_title(p_idx)

    @staticmethod
    def _axis_label_text(sides: List[str]) -> str:
        n = len(sides)
        if n == 1:
            return "1  (Left only)"
        n_left  = sum(1 for s in sides if s == "left")
        n_right = sum(1 for s in sides if s == "right")
        parts = []
        if n_left:
            parts.append(f"{n_left} Left" if n_left > 1 else "Left")
        if n_right:
            parts.append(f"{n_right} Right" if n_right > 1 else "Right")
        return f"{n}  ({' + '.join(parts)})"

    # ── Result extraction ────────────────────────────────────────────────────

    def result_digital_readouts(self) -> List[str]:
        """Channel names the user wants surfaced in the Live Values dock."""
        return [row.channel_name() for row in self._channel_rows if row.show_as_dro()]

    def result_layout(self) -> List[dict]:
        """Return one plot-config dict per plot (only plot-checked channels)."""
        n_plots = self._plot_count_spin.value()
        x_data  = self._x_combo.currentData() or (None, None)
        x_dev, x_ch = x_data

        plots: List[dict] = []
        for p_idx in range(n_plots):
            plots.append({
                "id": f"plot_{p_idx}",
                "title": self._resolved_plot_title(p_idx),
                "x_device":  x_dev,
                "x_channel": x_ch,
                "rolling_window_s": 0.0,
                "y_axes":     [],
                "channels":   [],
                "thresholds": [],
                "overlay_path": None,
            })

        axis_bounds: Dict[Tuple[int, int], Tuple[List[float], List[float]]] = {}
        axis_locked: Dict[Tuple[int, int], bool] = {}

        for row in self._channel_rows:
            if not row.include_in_plot():
                continue
            p_idx  = min(row.plot_idx(), n_plots - 1)
            a_idx  = row.y_axis_idx()
            key    = (p_idx, a_idx)

            mins, maxs = axis_bounds.setdefault(key, ([], []))
            if row.y_min() is not None:
                mins.append(row.y_min())
                axis_locked[key] = True
            if row.y_max() is not None:
                maxs.append(row.y_max())
                axis_locked[key] = True
            axis_locked.setdefault(key, False)

            plots[p_idx]["channels"].append({
                "device":     self._device_path,
                "channel":    row.channel_name(),
                "y_axis":     a_idx,
                "color":      row.color(),
                "line_style": "solid",
                "line_width": 1.4,
            })

        for p_idx, plot in enumerate(plots):
            used_axes = sorted({c["y_axis"] for c in plot["channels"]})
            if not used_axes:
                used_axes = [0]
            sides = self._axis_sides_per_plot.get(p_idx, ["left"])
            n_declared = len(sides)
            for a_idx in range(max(max(used_axes) + 1, n_declared)):
                bounds = axis_bounds.get((p_idx, a_idx))
                locked = axis_locked.get((p_idx, a_idx), False)
                if bounds and locked:
                    y_min = min(bounds[0]) if bounds[0] else None
                    y_max = max(bounds[1]) if bounds[1] else None
                else:
                    y_min = None
                    y_max = None
                axis_channels = [c for c in plot["channels"] if c["y_axis"] == a_idx]
                label = axis_channels[0]["channel"] if axis_channels else ""
                color = axis_channels[0]["color"] if axis_channels else "#cfcfcf"
                side  = sides[a_idx] if a_idx < len(sides) else (
                    "left" if a_idx == 0 else "right"
                )
                plot["y_axes"].append({
                    "label":  label,
                    "min":    y_min,
                    "max":    y_max,
                    "locked": locked,
                    "color":  color,
                    "side":   side,
                })

        return plots
