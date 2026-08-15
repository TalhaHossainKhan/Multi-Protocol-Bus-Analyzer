"""Unified Settings Panel.

A collapsible right-hand sidebar that owns *every* visual control in the
workspace.  No buttons live on individual plots anymore — they all live
here, grouped logically:

  • Plots         — add / remove panels; pick the panel to edit
  • Channels      — colour, line style, line width, Y-axis assignment
  • Axes          — X-axis source dropdown; per-Y-axis label/range/locked
  • Scaling       — quick auto-scale toggle for the active axis
  • Time Window   — "All data" or "Last N <unit>" with unit dropdown
  • Overlay       — load / clear historical CSV/Parquet overlay
  • Layout I/O    — save / load complete layout JSON

The panel is a "view" — it reads the current state from the
``WorkspaceWidget`` and pushes changes back through the existing
``PlotPanel`` public API (``add_channel``, ``reconfigure_channel``, etc.).
This keeps thread-safety guarantees intact: the panel only touches the
UI thread and the underlying ring buffers are never modified.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ui.plot_panel import (
    _LINE_STYLES,
    AlarmConfig,
    AnalysisTagConfig,
    ChannelStyleConfig,
    PlotPanel,
    XMarkerConfig,
    YAxisConfig,
)

# Convert unit-keys to seconds.
_UNIT_TO_SECONDS = {
    "ms":  0.001,
    "s":   1.0,
    "min": 60.0,
    "h":   3600.0,
}


class _ColorChip(QPushButton):
    """A flat colored swatch button that opens a colour picker on click."""

    color_changed = Signal(str)

    def __init__(self, color: str = "#06b6d4", parent=None) -> None:
        super().__init__(parent)
        self._color = color
        self.setFixedSize(56, 24)
        self.clicked.connect(self._pick)
        self._refresh()

    def color(self) -> str:
        return self._color

    def set_color(self, c: str) -> None:
        if c and c != self._color:
            self._color = c
            self._refresh()
            self.color_changed.emit(c)

    def _refresh(self) -> None:
        self.setStyleSheet(
            f"background-color: {self._color}; border: 1px solid #3a3a3a; "
            f"border-radius: 4px;"
        )

    def _pick(self) -> None:
        c = QColorDialog.getColor(QColor(self._color), self)
        if c.isValid():
            self.set_color(c.name())


# ── New Plot dialog ───────────────────────────────────────────────────────────

_PLOT_COLORS = (
    "#4FC3F7", "#FFB74D", "#81C784", "#F06292",
    "#BA68C8", "#FFD54F", "#90CAF9", "#A1887F",
    "#80CBC4", "#EF9A9A", "#CE93D8", "#80DEEA",
)


class _NewPlotDialog(QDialog):
    """Signal-Mapping-style dialog for adding a new plot mid-session.

    Shows all channels available in the workspace so the user can pick which
    to include, set per-channel Y-axis and colour, choose the rendering backend,
    and optionally configure the X-axis source.
    """

    def __init__(self, workspace, plot_title: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Configure New Plot")
        self.setMinimumSize(720, 520)
        self.setModal(True)
        self._workspace = workspace
        # Sides of all declared Y-axes; index 0 is always "left" (primary).
        self._axis_sides: List[str] = ["left"]
        self._rows: list = []   # list of dicts with widget refs

        self._build_ui(plot_title)

    @property
    def _n_axes(self) -> int:
        return len(self._axis_sides)

    # ── UI construction ──────────────────────────────────────────────────────

    def _build_ui(self, title: str) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 16, 20, 16)
        outer.setSpacing(14)

        hdr = QLabel(f"<b>Configure: {title}</b>")
        hdr.setObjectName("h2")
        outer.addWidget(hdr)

        # ── Renderer ──
        render_box = QGroupBox("Renderer")
        rb_layout  = QVBoxLayout(render_box)
        self._rb_std = QRadioButton(
            "Standard (pyqtgraph)  — full features: multi-axis, alarms, overlays."
        )
        self._rb_std.setChecked(True)
        self._rb_gpu = QRadioButton(
            "High Performance (Vispy — GPU)  — for >10 kHz or >64 channels.\n"
            "Single Y-axis only. No thresholds, alarms, overlays, or analysis tags."
        )
        rb_layout.addWidget(self._rb_std)
        rb_layout.addWidget(self._rb_gpu)
        # Hide Y-axis controls when Vispy is selected (Vispy has no multi-axis support).
        self._rb_gpu.toggled.connect(self._on_renderer_toggled)
        outer.addWidget(render_box)

        # ── Plot configuration (X-axis on one row, Y-axes on the next) ──
        cfg_box  = QGroupBox("Plot configuration")
        cfg_vbox = QVBoxLayout(cfg_box)
        cfg_vbox.setSpacing(10)

        x_row = QHBoxLayout()
        x_row.addWidget(QLabel("X-axis:"))
        self._x_combo = QComboBox()
        self._x_combo.addItem("Timestamp (PC_Time_s)", userData=(None, None))
        for dev_path, ch_name in self._workspace.get_all_channels():
            self._x_combo.addItem(ch_name, userData=(dev_path, ch_name))
        x_row.addWidget(self._x_combo, stretch=1)
        cfg_vbox.addLayout(x_row)

        # Y-axes row — independent so the Add/Remove buttons can't truncate.
        self._yaxis_row = QWidget()
        y_layout = QHBoxLayout(self._yaxis_row)
        y_layout.setContentsMargins(0, 0, 0, 0)
        y_layout.setSpacing(8)
        y_layout.addWidget(QLabel("Y-axes:"))
        self._yaxis_count_lbl = QLabel("1  (Left only)")
        self._yaxis_count_lbl.setObjectName("subtle")
        y_layout.addWidget(self._yaxis_count_lbl, stretch=1)
        btn_add_y = QPushButton("+ Add Y-Axis")
        btn_add_y.setObjectName("secondary")
        btn_add_y.clicked.connect(self._on_add_yaxis)
        y_layout.addWidget(btn_add_y)
        self._btn_rem_y = QPushButton("− Remove")
        self._btn_rem_y.setObjectName("danger")
        self._btn_rem_y.clicked.connect(self._on_rem_yaxis)
        y_layout.addWidget(self._btn_rem_y)
        cfg_vbox.addWidget(self._yaxis_row)

        outer.addWidget(cfg_box)

        # ── Channel selector ──
        ch_box    = QGroupBox("Channels to include")
        ch_layout = QVBoxLayout(ch_box)

        scroll       = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        inner        = QWidget()
        self._ch_rows_layout = QVBoxLayout(inner)
        self._ch_rows_layout.setSpacing(6)
        self._ch_rows_layout.setContentsMargins(2, 2, 2, 2)

        channels = self._workspace.get_all_channels()
        if not channels:
            self._ch_rows_layout.addWidget(
                QLabel("No channels are active yet. Connect a device first.")
            )
        else:
            for idx, (dev_path, ch_name) in enumerate(channels):
                row = self._make_channel_row(idx, dev_path, ch_name)
                self._ch_rows_layout.addWidget(row["widget"])
                self._rows.append(row)
        self._ch_rows_layout.addStretch()

        scroll.setWidget(inner)
        ch_layout.addWidget(scroll)
        outer.addWidget(ch_box, stretch=1)

        # ── Buttons ──
        btns = QDialogButtonBox(QDialogButtonBox.Ok)
        btns.button(QDialogButtonBox.Ok).setText("Add Plot →")
        btns.button(QDialogButtonBox.Ok).setObjectName("primary")
        btns.accepted.connect(self.accept)
        outer.addWidget(btns)

    def _make_channel_row(self, idx: int, dev_path: str, ch_name: str) -> dict:
        """Build one channel row widget and return a dict with all widget refs."""
        frame = QFrame()
        row_layout = QHBoxLayout(frame)
        row_layout.setContentsMargins(4, 2, 4, 2)
        row_layout.setSpacing(10)

        cb = QCheckBox()
        cb.setChecked(True)
        row_layout.addWidget(cb)

        color = _PLOT_COLORS[idx % len(_PLOT_COLORS)]
        color_btn = QPushButton()
        color_btn.setFixedSize(20, 20)
        color_btn.setStyleSheet(
            f"background-color:{color}; border:1px solid #3a3a3a; border-radius:3px;"
        )
        color_btn.clicked.connect(lambda _, b=color_btn: self._pick_color(b))
        row_layout.addWidget(color_btn)

        name_lbl = QLabel(f"<b>{ch_name}</b>")
        name_lbl.setToolTip(dev_path)
        row_layout.addWidget(name_lbl, stretch=1)

        y_combo = QComboBox()
        self._rebuild_yaxis_combo(y_combo)
        row_layout.addWidget(y_combo)

        row = {
            "widget":    frame,
            "checkbox":  cb,
            "color_btn": color_btn,
            "color":     color,
            "y_combo":   y_combo,
            "dev_path":  dev_path,
            "ch_name":   ch_name,
        }
        # Store mutable color in a list cell so the lambda closure can update it.
        color_cell = [color]

        def _pick_color_for_row(btn=color_btn, cell=color_cell):
            from PySide6.QtGui import QColor as _QColor
            c = QColorDialog.getColor(_QColor(cell[0]), self)
            if c.isValid():
                cell[0] = c.name()
                btn.setStyleSheet(
                    f"background-color:{cell[0]}; border:1px solid #3a3a3a;"
                    f"border-radius:3px;"
                )
                row["color"] = cell[0]

        color_btn.clicked.disconnect()
        color_btn.clicked.connect(_pick_color_for_row)

        return row

    @staticmethod
    def _pick_color(btn: QPushButton) -> None:
        pass   # handled per-row above

    def _rebuild_yaxis_combo(self, combo: QComboBox) -> None:
        current = combo.currentData() or 0
        combo.blockSignals(True)
        combo.clear()
        for i, side in enumerate(self._axis_sides):
            combo.addItem(f"Axis {i} ({side.capitalize()})", userData=i)
        combo.setCurrentIndex(min(current, combo.count() - 1))
        combo.blockSignals(False)

    def _on_add_yaxis(self) -> None:
        if self._n_axes >= 8:
            return
        side = _AxisSideDialog.ask(self)
        if side is None:
            return
        self._axis_sides.append(side)
        self._update_yaxis_label()
        for row in self._rows:
            self._rebuild_yaxis_combo(row["y_combo"])

    def _on_rem_yaxis(self) -> None:
        if self._n_axes <= 1:
            return
        # Build the choice list for the dropdown menu (axis 0 is never offered).
        choices: List[Tuple[int, str]] = [
            (i, f"Axis {i} ({self._axis_sides[i].capitalize()})")
            for i in range(1, self._n_axes)
        ]
        idx = pick_axis_via_menu(self._btn_rem_y, choices)
        if idx is None:
            return
        # Re-assign rows that referenced the removed axis to axis 0; rows
        # referencing higher indices shift down by one to keep them stable.
        for row in self._rows:
            cur = row["y_combo"].currentData()
            if cur is None:
                continue
            if cur == idx:
                row["y_combo"].setCurrentIndex(0)
            elif cur > idx:
                row["y_combo"].setCurrentIndex(cur - 1)
        self._axis_sides.pop(idx)
        self._update_yaxis_label()
        for row in self._rows:
            self._rebuild_yaxis_combo(row["y_combo"])

    def _update_yaxis_label(self) -> None:
        n_left  = sum(1 for s in self._axis_sides if s == "left")
        n_right = sum(1 for s in self._axis_sides if s == "right")
        if self._n_axes == 1:
            self._yaxis_count_lbl.setText("1  (Left only)")
        else:
            parts: List[str] = []
            if n_left:
                parts.append(f"{n_left} Left" if n_left > 1 else "Left")
            if n_right:
                parts.append(f"{n_right} Right" if n_right > 1 else "Right")
            self._yaxis_count_lbl.setText(
                f"{self._n_axes}  ({' + '.join(parts)})"
            )

    def _on_renderer_toggled(self, gpu_checked: bool) -> None:
        """Hide Y-axis controls when Vispy is selected — it only supports one axis."""
        self._yaxis_row.setVisible(not gpu_checked)
        if gpu_checked and self._n_axes > 1:
            self._axis_sides = ["left"]
            self._update_yaxis_label()
            for row in self._rows:
                self._rebuild_yaxis_combo(row["y_combo"])

    # ── Result extraction ────────────────────────────────────────────────────

    def result_plot_config(self):
        """Return ``(backend, plot_config_dict)``."""
        import uuid as _uuid

        backend = "vispy" if self._rb_gpu.isChecked() else "pyqtgraph"

        x_data  = self._x_combo.currentData() or (None, None)
        x_dev, x_ch = x_data

        # Build channel list from selected rows.
        channels = []
        used_axes: set = set()
        for row in self._rows:
            if not row["checkbox"].isChecked():
                continue
            a_idx = row["y_combo"].currentData() or 0
            used_axes.add(a_idx)
            channels.append({
                "device":     row["dev_path"],
                "channel":    row["ch_name"],
                "y_axis":     a_idx,
                "color":      row["color"],
                "line_style": "solid",
                "line_width": 1.4,
            })

        # Build y_axes list — one entry per used (or declared) axis.
        n = max(self._n_axes, max(used_axes, default=0) + 1)
        y_axes = []
        for i in range(n):
            side = self._axis_sides[i] if i < len(self._axis_sides) else (
                "left" if i == 0 else "right"
            )
            y_axes.append({
                "label":  "",
                "min":    None,
                "max":    None,
                "locked": False,
                "color":  "#cfcfcf",
                "side":   side,
            })

        plot_cfg = {
            "id":              f"plot_{_uuid.uuid4().hex[:8]}",
            "title":           "New Plot",
            "x_device":        x_dev,
            "x_channel":       x_ch,
            "rolling_window_s": 0.0,
            "y_axes":          y_axes,
            "channels":        channels,
            "thresholds":      [],
            "overlay_path":    None,
        }
        return backend, plot_cfg


class _AxisSideDialog(QDialog):
    """Tiny popup asking 'Left or Right' for a new Y-axis."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add Y-Axis")
        self.setModal(True)
        self.setMinimumWidth(280)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.addWidget(QLabel("Add the new Y-axis on which side?"))

        self._rb_left  = QRadioButton("Left")
        self._rb_right = QRadioButton("Right")
        self._rb_right.setChecked(True)
        layout.addWidget(self._rb_left)
        layout.addWidget(self._rb_right)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def side(self) -> str:
        return "left" if self._rb_left.isChecked() else "right"

    @classmethod
    def ask(cls, parent) -> Optional[str]:
        dlg = cls(parent)
        if dlg.exec() != QDialog.Accepted:
            return None
        return dlg.side()


def pick_axis_via_menu(
    button: QPushButton,
    axis_choices: List[Tuple[int, str]],
) -> Optional[int]:
    """Pop a dropdown menu under *button* listing axes to remove.

    Returns the chosen axis index, or None if the user dismissed the menu.
    Axis 0 should never appear in *axis_choices* — the primary axis must
    always remain on the plot.
    """
    if not axis_choices:
        return None
    menu = QMenu(button)
    actions: Dict = {}
    for idx, label in axis_choices:
        action = menu.addAction(label)
        actions[id(action)] = idx
    chosen = menu.exec(button.mapToGlobal(button.rect().bottomLeft()))
    if chosen is None:
        return None
    return actions.get(id(chosen))


class SettingsPanel(QFrame):
    """Single collapsible sidebar containing every workspace control."""

    save_layout_requested          = Signal()
    load_layout_requested          = Signal()
    load_layout_from_path_requested = Signal(str)   # team library load

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("sidebar")

        self._workspace = None
        self._active_panel: Optional[PlotPanel] = None
        self._refreshing = False
        self._active_axis_idx: int = 0   # which Y-axis the axis-settings form targets
        self._live_dashboard = None
        self._live_value_checks: Dict[Tuple[str, str], QCheckBox] = {}

        self._prefs = QSettings("DAQPlatform", "UserPrefs")

        self._build_ui()

    # ── Wiring with the workspace ────────────────────────────────────────────

    def attach_workspace(self, workspace) -> None:
        """Bind to a ``WorkspaceWidget`` and start reflecting its state."""
        self._workspace = workspace
        self._subscribe_all_panels()
        workspace.panels_changed.connect(self._on_panels_changed, Qt.UniqueConnection)
        self.refresh()

    def attach_dashboard(self, dashboard) -> None:
        """Bind to the bottom DigitalDashboard so the Live Values group
        can populate its check-list and write changes back to it."""
        self._live_dashboard = dashboard
        self._refresh_live_values()

    def _subscribe_all_panels(self) -> None:
        if self._workspace is None:
            return
        for panel in self._workspace.get_panels():
            panel.channel_axis_changed.connect(
                self._on_panel_axis_changed, Qt.UniqueConnection
            )

    def _on_panels_changed(self) -> None:
        self._subscribe_all_panels()
        if not self._refreshing:
            self.refresh()

    def _on_panel_axis_changed(self, _key: str, _axis: int) -> None:
        if not self._refreshing:
            self.refresh()

    def set_active_panel(self, panel: Optional[PlotPanel]) -> None:
        """Switch the panel that the sidebar controls are editing."""
        self._active_panel = panel
        self.refresh()

    # ── UI construction ──────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Scrollable body.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(14, 14, 14, 14)
        body_layout.setSpacing(10)

        body_layout.addWidget(self._build_interaction_group())
        body_layout.addWidget(self._build_plots_group())
        body_layout.addWidget(self._build_xaxis_group())
        body_layout.addWidget(self._build_channels_and_axes_group())
        body_layout.addWidget(self._build_live_values_group())
        body_layout.addWidget(self._build_window_group())
        body_layout.addWidget(self._build_layout_io_group())
        body_layout.addStretch()

        scroll.setWidget(body)
        outer.addWidget(scroll, stretch=1)

        # Make every combo's popup wide enough to show full text.
        self._fix_all_combo_popups()

    def _fix_all_combo_popups(self) -> None:
        """Ensure dropdown popups are wide enough to show their item text."""
        for combo in self.findChildren(QComboBox):
            combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
            self._size_combo_popup(combo)

    @staticmethod
    def _size_combo_popup(combo: QComboBox) -> None:
        """Force the combo's dropdown view to be as wide as its widest item."""
        view = combo.view()
        if view is None:
            return
        view.setTextElideMode(Qt.ElideNone)
        fm = combo.fontMetrics()
        max_w = 0
        for i in range(combo.count()):
            max_w = max(max_w, fm.horizontalAdvance(combo.itemText(i)))
        # +60 leaves room for the scrollbar + item padding from QSS.
        view.setMinimumWidth(max(max_w + 60, combo.width()))

    # ── Plots group ──────────────────────────────────────────────────────────

    def _build_plots_group(self) -> QGroupBox:
        box = QGroupBox("Plots")
        v = QVBoxLayout(box)
        v.setSpacing(8)

        self._plot_combo = QComboBox()
        self._plot_combo.setToolTip("Pick which plot panel the settings below edit")
        self._plot_combo.currentIndexChanged.connect(self._on_active_panel_changed)
        v.addWidget(self._plot_combo)

        self._btn_add_plot = QPushButton("+  Add Plot")
        self._btn_add_plot.setObjectName("secondary")
        self._btn_add_plot.clicked.connect(self._on_add_plot)
        v.addWidget(self._btn_add_plot)

        # Add channel to the active plot — moved here from Channels & Axes.
        v.addWidget(QLabel("Add channel to this plot:"))
        add_ch_row = QHBoxLayout()
        self._add_ch_combo = QComboBox()
        self._add_ch_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        add_ch_row.addWidget(self._add_ch_combo, stretch=1)
        self._btn_add_channel = QPushButton("Add →")
        self._btn_add_channel.setObjectName("secondary")
        self._btn_add_channel.setEnabled(False)
        self._btn_add_channel.clicked.connect(self._on_add_channel_to_plot)
        add_ch_row.addWidget(self._btn_add_channel)
        v.addLayout(add_ch_row)

        return box

    # ── X-axis group ─────────────────────────────────────────────────────────

    def _build_xaxis_group(self) -> QGroupBox:
        box = QGroupBox("X-axis")
        v = QVBoxLayout(box)
        v.setSpacing(6)

        row = QHBoxLayout()
        row.addWidget(QLabel("Source:"))
        self._x_combo = QComboBox()
        self._x_combo.currentIndexChanged.connect(self._on_x_axis_changed)
        row.addWidget(self._x_combo, stretch=1)
        v.addLayout(row)

        return box

    # ── Combined Channels & Axes group ──────────────────────────────────────

    def _build_channels_and_axes_group(self) -> QGroupBox:
        box = QGroupBox("Channels & Axes")
        v = QVBoxLayout(box)
        v.setSpacing(8)

        # ── Channel selector ──
        v.addWidget(QLabel("Channel:"))
        self._channel_combo = QComboBox()
        self._channel_combo.currentIndexChanged.connect(self._on_active_channel_changed)
        v.addWidget(self._channel_combo)

        # Visibility toggle — directly below the channel selector.
        self._ch_visible_cb = QCheckBox("Show channel in plot")
        self._ch_visible_cb.setChecked(True)
        self._ch_visible_cb.toggled.connect(self._on_channel_visibility_changed)
        v.addWidget(self._ch_visible_cb)

        # Combined channel-style + axis-settings form. Matches the form style
        # used in the Analysis panel — default field growth policy keeps fields
        # at their natural size so the labels + fields read as a centered pair
        # rather than a stretched row.
        form = QFormLayout()
        form.setSpacing(7)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self._color_chip = _ColorChip()
        self._color_chip.color_changed.connect(self._on_channel_color_changed)
        form.addRow("Colour:", self._color_chip)

        self._style_combo = QComboBox()
        for s in _LINE_STYLES:
            self._style_combo.addItem(s)
        self._style_combo.currentTextChanged.connect(self._on_channel_style_changed)
        form.addRow("Line style:", self._style_combo)

        self._width_spin = QDoubleSpinBox()
        self._width_spin.setRange(0.5, 6.0)
        self._width_spin.setSingleStep(0.5)
        self._width_spin.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self._width_spin.setMinimumWidth(120)
        self._width_spin.valueChanged.connect(self._on_channel_width_changed)
        form.addRow("Line width:", self._width_spin)

        self._yaxis_combo = QComboBox()
        self._yaxis_combo.currentIndexChanged.connect(self._on_channel_yaxis_changed)
        form.addRow("Y-axis:", self._yaxis_combo)

        self._axis_label_edit = QLineEdit()
        self._axis_label_edit.setPlaceholderText("e.g. Voltage (V)")
        self._axis_label_edit.editingFinished.connect(self._on_axis_label_changed)
        form.addRow("Y-axis label:", self._axis_label_edit)

        self._autoscale_cb = QCheckBox("Auto-scale to data")
        self._autoscale_cb.toggled.connect(self._on_autoscale_toggled)
        form.addRow("", self._autoscale_cb)

        self._min_label = QLabel("Min:")
        self._min_spin = QDoubleSpinBox()
        self._min_spin.setRange(-1e9, 1e9)
        self._min_spin.setDecimals(4)
        self._min_spin.editingFinished.connect(self._on_axis_range_changed)
        form.addRow(self._min_label, self._min_spin)

        self._max_label = QLabel("Max:")
        self._max_spin = QDoubleSpinBox()
        self._max_spin.setRange(-1e9, 1e9)
        self._max_spin.setDecimals(4)
        self._max_spin.editingFinished.connect(self._on_axis_range_changed)
        form.addRow(self._max_label, self._max_spin)

        v.addLayout(form)

        axis_btn_row = QHBoxLayout()
        self._btn_add_axis = QPushButton("+  Add Y-Axis")
        self._btn_add_axis.setObjectName("secondary")
        self._btn_add_axis.clicked.connect(self._on_add_y_axis)
        axis_btn_row.addWidget(self._btn_add_axis)

        self._btn_rem_axis = QPushButton("−  Remove Y-Axis")
        self._btn_rem_axis.setObjectName("danger")
        self._btn_rem_axis.clicked.connect(self._on_remove_y_axis)
        axis_btn_row.addWidget(self._btn_rem_axis)
        v.addLayout(axis_btn_row)

        return box

    # ── Time-window group ────────────────────────────────────────────────────

    def _build_window_group(self) -> QGroupBox:
        box = QGroupBox("Time Window")
        v = QVBoxLayout(box)
        v.setSpacing(6)

        v.addWidget(QLabel(
            "Show only the most recent slice of the X-axis. "
            "Older data stays safely in the buffer.",
        ))

        row = QHBoxLayout()
        self._show_combo = QComboBox()
        self._show_combo.addItem("All data",       userData="all")
        self._show_combo.addItem("Show last…",     userData="last")
        self._show_combo.currentIndexChanged.connect(self._on_window_changed)
        row.addWidget(self._show_combo)
        v.addLayout(row)

        row2 = QHBoxLayout()
        self._window_spin = QDoubleSpinBox()
        self._window_spin.setRange(0.1, 36000.0)
        self._window_spin.setDecimals(1)
        self._window_spin.setValue(10.0)
        self._window_spin.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self._window_spin.valueChanged.connect(self._on_window_changed)
        row2.addWidget(self._window_spin)

        self._window_unit = QComboBox()
        self._window_unit.addItem("Seconds", userData="s")
        self._window_unit.addItem("Minutes", userData="min")
        self._window_unit.addItem("Hours",   userData="h")
        self._window_unit.addItem("Milliseconds", userData="ms")
        self._window_unit.setCurrentIndex(0)
        self._window_unit.currentIndexChanged.connect(self._on_window_changed)
        row2.addWidget(self._window_unit)
        v.addLayout(row2)

        # Show the value row only when "Show last…" is picked.
        self._window_value_row = QWidget()
        wvr_layout = QHBoxLayout(self._window_value_row)
        wvr_layout.setContentsMargins(0, 0, 0, 0)
        # Move existing widgets in
        # (we already added them to row2; container exists only for hiding logic)

        return box

    # ── Interaction group ────────────────────────────────────────────────────

    def _build_interaction_group(self) -> QGroupBox:
        box = QGroupBox("Interaction")
        v = QVBoxLayout(box)
        v.setSpacing(6)

        row = QHBoxLayout()
        row.addWidget(QLabel("Mouse mode:"))
        self._mouse_mode_combo = QComboBox()
        self._mouse_mode_combo.addItem("Pan (drag to scroll)",   "pan")
        self._mouse_mode_combo.addItem("Rectangle Zoom",         "rect")
        self._mouse_mode_combo.currentIndexChanged.connect(self._on_mouse_mode_changed)
        row.addWidget(self._mouse_mode_combo)
        row.addStretch()
        v.addLayout(row)

        return box

    def _on_mouse_mode_changed(self) -> None:
        import pyqtgraph as pg
        if getattr(self, "_refreshing", False):
            return
        key = self._mouse_mode_combo.currentData()
        mode = pg.ViewBox.RectMode if key == "rect" else pg.ViewBox.PanMode
        if self._workspace is None:
            return
        for panel in self._workspace.get_panels():
            panel.set_mouse_mode(mode)

    def refresh_mouse_mode(self) -> None:
        """Re-read the current mouse mode from the active panel so a
        programmatic change keeps the combo in sync with the plot."""
        import pyqtgraph as pg
        if self._workspace is None:
            return
        panels = self._workspace.get_panels()
        if not panels:
            return
        try:
            vb = panels[0]._plot_item.vb
            cur = vb.state.get("mouseMode", pg.ViewBox.PanMode)
        except Exception:                                # noqa: BLE001
            return
        target_key = "rect" if cur == pg.ViewBox.RectMode else "pan"
        self._refreshing = True
        try:
            for i in range(self._mouse_mode_combo.count()):
                if self._mouse_mode_combo.itemData(i) == target_key:
                    self._mouse_mode_combo.setCurrentIndex(i)
                    break
        finally:
            self._refreshing = False

    # ── Live Values group ───────────────────────────────────────────────────

    def _build_live_values_group(self) -> QGroupBox:
        box = QGroupBox("Live Values")
        v = QVBoxLayout(box)
        v.setSpacing(4)

        v.addWidget(QLabel(
            "Tick channels to show them as large numeric readouts at the "
            "bottom of the workspace."
        ))

        # Container that gets rebuilt every refresh — each channel becomes
        # one checkbox whose state mirrors the DigitalDashboard._boxes set.
        self._live_values_container = QWidget()
        self._live_values_layout = QVBoxLayout(self._live_values_container)
        self._live_values_layout.setContentsMargins(0, 0, 0, 0)
        self._live_values_layout.setSpacing(2)
        v.addWidget(self._live_values_container)

        # Bulk-action buttons.
        row = QHBoxLayout()
        btn_all = QPushButton("Show All")
        btn_all.setObjectName("secondary")
        btn_all.clicked.connect(self._on_live_values_show_all)
        row.addWidget(btn_all)
        btn_none = QPushButton("Clear")
        btn_none.setObjectName("secondary")
        btn_none.clicked.connect(self._on_live_values_clear)
        row.addWidget(btn_none)
        row.addStretch()
        v.addLayout(row)

        self._live_values_empty_label = QLabel(
            "Connect a device to choose live-value channels."
        )
        self._live_values_empty_label.setObjectName("aiSubLabel")
        v.addWidget(self._live_values_empty_label)

        return box

    def _refresh_live_values(self) -> None:
        """Rebuild the live-values checkbox list from the current channel set."""
        layout = getattr(self, "_live_values_layout", None)
        if layout is None:
            return
        # Drop prior checkboxes.
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._live_value_checks.clear()

        if self._workspace is None or self._live_dashboard is None:
            self._live_values_empty_label.setVisible(True)
            return

        rows = list(self._workspace.get_all_channels())
        if not rows:
            self._live_values_empty_label.setVisible(True)
            return
        self._live_values_empty_label.setVisible(False)

        active = set(getattr(self._live_dashboard, "_boxes", {}).keys())
        for dev_path, ch_name in rows:
            cb = QCheckBox(ch_name)
            cb.setChecked((dev_path, ch_name) in active)
            cb.toggled.connect(
                lambda checked, d=dev_path, c=ch_name:
                    self._on_live_value_toggled(d, c, checked)
            )
            self._live_value_checks[(dev_path, ch_name)] = cb
            layout.addWidget(cb)

    def _on_live_value_toggled(self, device: str, channel: str, on: bool) -> None:
        if self._live_dashboard is None or getattr(self, "_refreshing", False):
            return
        if on:
            self._live_dashboard.add_channels(device, [channel])
            if self._live_dashboard.isHidden():
                self._live_dashboard.show()
        else:
            boxes = getattr(self._live_dashboard, "_boxes", {})
            key = (device, channel)
            if key in boxes:
                box = boxes.pop(key)
                box.setParent(None)
                box.deleteLater()
                self._live_dashboard._update_empty_state()

    def _on_live_values_show_all(self) -> None:
        if self._live_dashboard is None or self._workspace is None:
            return
        # Group adds by device so add_channels is one call per device.
        by_dev: Dict[str, List[str]] = {}
        for dev_path, ch in self._workspace.get_all_channels():
            by_dev.setdefault(dev_path, []).append(ch)
        for dev_path, names in by_dev.items():
            self._live_dashboard.add_channels(dev_path, names)
        if self._live_dashboard.isHidden():
            self._live_dashboard.show()
        self._refresh_live_values()

    def _on_live_values_clear(self) -> None:
        if self._live_dashboard is None:
            return
        self._live_dashboard.clear()
        self._refresh_live_values()

    # ── Layout I/O group ─────────────────────────────────────────────────────

    def _build_layout_io_group(self) -> QGroupBox:
        box = QGroupBox("Layout Templates")
        v = QVBoxLayout(box)
        v.setSpacing(8)

        v.addWidget(QLabel(
            "Save or load a reusable settings template (plots, channels, "
            "colours, axes, alarms)."
        ))

        row = QHBoxLayout()
        btn_save = QPushButton("Save as Template")
        btn_save.setObjectName("primary")
        btn_save.clicked.connect(self.save_layout_requested.emit)
        row.addWidget(btn_save)

        btn_load = QPushButton("Load Template")
        btn_load.setObjectName("secondary")
        btn_load.clicked.connect(self.load_layout_requested.emit)
        row.addWidget(btn_load)
        v.addLayout(row)

        return box

    # ── Refresh: pull current state into the widgets ─────────────────────────

    def refresh(self) -> None:
        """Re-populate widgets from the current workspace state."""
        if self._workspace is None:
            return
        self._refreshing = True
        try:
            self._refresh_plots()
            self._refresh_channels()
            self._refresh_axes()
            self._refresh_window()
            self._refresh_live_values()
        finally:
            self._refreshing = False
        self.refresh_mouse_mode()
        self._fix_all_combo_popups()

    def _refresh_plots(self) -> None:
        panels = self._workspace.get_panels()
        current = self._active_panel
        self._plot_combo.blockSignals(True)
        self._plot_combo.clear()
        for p in panels:
            self._plot_combo.addItem(p.title, userData=p)
        if current in panels:
            idx = panels.index(current)
            self._plot_combo.setCurrentIndex(idx)
        elif panels:
            self._active_panel = panels[0]
            self._plot_combo.setCurrentIndex(0)
        else:
            self._active_panel = None
        self._plot_combo.blockSignals(False)

    def _refresh_channels(self) -> None:
        self._channel_combo.blockSignals(True)
        self._channel_combo.clear()
        if self._active_panel is None:
            self._channel_combo.blockSignals(False)
            self._set_channel_controls_enabled(False)
        else:
            for key, cfg in self._active_panel._channels.items():
                ax = getattr(cfg, 'y_axis_idx', 0)
                self._channel_combo.addItem(
                    f"{cfg.channel_name}  [axis {ax}]", userData=key
                )
            self._channel_combo.blockSignals(False)

            if self._channel_combo.count() > 0:
                self._set_channel_controls_enabled(True)
                self._reflect_channel_in_form()
            else:
                self._set_channel_controls_enabled(False)

        # Rebuild add-channel combo from all channels across all plots.
        self._add_ch_combo.blockSignals(True)
        self._add_ch_combo.clear()
        if self._workspace is not None:
            for dev_path, ch_name in self._workspace.get_all_channels():
                self._add_ch_combo.addItem(ch_name, userData=(dev_path, ch_name))
        self._add_ch_combo.blockSignals(False)
        can_add = self._add_ch_combo.count() > 0 and self._active_panel is not None
        self._btn_add_channel.setEnabled(can_add)

    def _reflect_channel_in_form(self) -> None:
        if self._active_panel is None:
            return
        key = self._channel_combo.currentData()
        if not key:
            return
        cfg = self._active_panel._channels.get(key)
        if cfg is None:
            return
        # Restore visibility checkbox.
        visible = getattr(self._active_panel, '_channel_visible', {}).get(key, True)
        self._ch_visible_cb.blockSignals(True)
        self._ch_visible_cb.setChecked(visible)
        self._ch_visible_cb.blockSignals(False)

        for w in (self._color_chip, self._style_combo,
                  self._width_spin, self._yaxis_combo):
            w.blockSignals(True)
        self._color_chip.set_color(cfg.color)
        self._style_combo.setCurrentText(cfg.line_style)
        self._width_spin.setValue(cfg.line_width)
        # Rebuild Y-axis combo for this panel.
        y_axis_cfgs = getattr(self._active_panel, '_y_axis_cfgs', [])
        axis_sides = getattr(self._active_panel, '_axis_sides', [])
        self._yaxis_combo.clear()
        for i in range(max(len(y_axis_cfgs), 1)):
            side = axis_sides[i] if i < len(axis_sides) else ("left" if i == 0 else "right")
            self._yaxis_combo.addItem(
                f"Axis {i} ({side.capitalize()})", userData=i
            )
        self._yaxis_combo.setCurrentIndex(
            min(cfg.y_axis_idx, self._yaxis_combo.count() - 1)
        )
        for w in (self._color_chip, self._style_combo,
                  self._width_spin, self._yaxis_combo):
            w.blockSignals(False)
        # Drive the axis settings section from this channel's axis.
        self._active_axis_idx = cfg.y_axis_idx
        self._reflect_axis_in_form()

    def _set_channel_controls_enabled(self, enabled: bool) -> None:
        for w in (self._channel_combo, self._ch_visible_cb, self._color_chip,
                  self._style_combo, self._width_spin, self._yaxis_combo):
            w.setEnabled(enabled)

    def _refresh_axes(self) -> None:
        panel = self._active_panel
        self._x_combo.blockSignals(True)
        self._x_combo.clear()
        self._x_combo.addItem("Timestamp (PC_Time_s)", userData=(None, None))
        if panel is not None:
            seen: set = set()
            for cfg in panel._channels.values():
                tup = (cfg.device_path, cfg.channel_name)
                if tup in seen:
                    continue
                seen.add(tup)
                self._x_combo.addItem(cfg.channel_name, userData=tup)
            # Restore current X selection (guarded for Vispy compat).
            if hasattr(panel, '_x_device') and hasattr(panel, '_x_ch_name'):
                cur = (panel._x_device, panel._x_ch_name)
                for i in range(self._x_combo.count()):
                    if self._x_combo.itemData(i) == cur:
                        self._x_combo.setCurrentIndex(i)
                        break
        self._x_combo.blockSignals(False)
        self._reflect_axis_in_form()

    def _reflect_axis_in_form(self) -> None:
        if self._active_panel is None:
            return
        y_axis_cfgs = getattr(self._active_panel, '_y_axis_cfgs', [])
        ax_idx = self._active_axis_idx
        # Vispy panels have no y_axis_cfgs — hide axis settings section.
        has_axes = bool(y_axis_cfgs) or hasattr(self._active_panel, 'add_y_axis')
        if not y_axis_cfgs:
            # Enable axis settings but show defaults (single unlabelled left axis).
            for w in (self._axis_label_edit, self._autoscale_cb,
                      self._min_spin, self._max_spin):
                w.blockSignals(True)
            self._axis_label_edit.setText("")
            self._autoscale_cb.setChecked(True)
            for w in (self._axis_label_edit, self._autoscale_cb,
                      self._min_spin, self._max_spin):
                w.blockSignals(False)
            for w in (self._min_label, self._min_spin, self._max_label, self._max_spin):
                w.setVisible(False)
            return
        # Lazy-init for the primary axis on pyqtgraph panels.
        while len(y_axis_cfgs) <= ax_idx:
            self._active_panel.add_y_axis()
        cfg = y_axis_cfgs[ax_idx]
        for w in (self._axis_label_edit, self._autoscale_cb,
                  self._min_spin, self._max_spin):
            w.blockSignals(True)
        self._axis_label_edit.setText(cfg.label)
        self._autoscale_cb.setChecked(not cfg.locked)
        self._min_spin.setValue(cfg.y_min if cfg.y_min is not None else 0.0)
        self._max_spin.setValue(cfg.y_max if cfg.y_max is not None else 100.0)
        for w in (self._axis_label_edit, self._autoscale_cb,
                  self._min_spin, self._max_spin):
            w.blockSignals(False)
        show_range = not self._autoscale_cb.isChecked()
        for w in (self._min_label, self._min_spin, self._max_label, self._max_spin):
            w.setVisible(show_range)

    def _refresh_window(self) -> None:
        if self._active_panel is None:
            return
        s = self._active_panel._rolling_s
        self._show_combo.blockSignals(True)
        self._window_spin.blockSignals(True)
        self._window_unit.blockSignals(True)
        if s <= 0:
            self._show_combo.setCurrentIndex(0)   # All data
            self._window_spin.setEnabled(False)
            self._window_unit.setEnabled(False)
        else:
            self._show_combo.setCurrentIndex(1)
            self._window_spin.setEnabled(True)
            self._window_unit.setEnabled(True)
            # Pick the best unit for display.
            if s >= 3600:
                self._window_unit.setCurrentIndex(2)   # Hours
                self._window_spin.setValue(s / 3600.0)
            elif s >= 60:
                self._window_unit.setCurrentIndex(1)   # Minutes
                self._window_spin.setValue(s / 60.0)
            elif s >= 1:
                self._window_unit.setCurrentIndex(0)   # Seconds
                self._window_spin.setValue(s)
            else:
                self._window_unit.setCurrentIndex(3)   # ms
                self._window_spin.setValue(s * 1000.0)
        self._show_combo.blockSignals(False)
        self._window_spin.blockSignals(False)
        self._window_unit.blockSignals(False)

    def _on_active_panel_changed(self, idx: int) -> None:
        if self._refreshing:
            return
        if idx < 0 or self._workspace is None:
            return
        panel = self._plot_combo.itemData(idx)
        self.set_active_panel(panel)

    def _on_add_plot(self) -> None:
        if self._workspace is None:
            return
        panels = self._workspace.get_panels()
        title  = f"Plot {len(panels) + 1}"
        dlg = _NewPlotDialog(self._workspace, title, parent=self)
        if dlg.exec() != QDialog.Accepted:
            return
        backend, plot_cfg = dlg.result_plot_config()
        plot_cfg["title"] = title
        new_panel = self._workspace.add_plot(title, backend=backend)
        new_panel.channel_axis_changed.connect(
            self._on_panel_axis_changed, Qt.UniqueConnection
        )
        new_panel.restore_config(plot_cfg)
        self._active_panel = new_panel
        self.refresh()

    # ── Slot handlers (channels) ─────────────────────────────────────────────

    def _on_active_channel_changed(self, _idx: int) -> None:
        if self._refreshing:
            return
        self._reflect_channel_in_form()

    def _on_channel_color_changed(self, color: str) -> None:
        if self._refreshing or self._active_panel is None:
            return
        key = self._channel_combo.currentData()
        if not key:
            return
        cfg = self._active_panel._channels.get(key)
        if cfg is None:
            return
        new_cfg = ChannelStyleConfig(
            device_path=cfg.device_path,
            channel_name=cfg.channel_name,
            y_axis_idx=cfg.y_axis_idx,
            color=color,
            line_style=cfg.line_style,
            line_width=cfg.line_width,
        )
        self._active_panel.reconfigure_channel(key, new_cfg)

    def _on_channel_style_changed(self, style: str) -> None:
        if self._refreshing or self._active_panel is None:
            return
        key = self._channel_combo.currentData()
        if not key:
            return
        cfg = self._active_panel._channels.get(key)
        if cfg is None:
            return
        new_cfg = ChannelStyleConfig(
            device_path=cfg.device_path, channel_name=cfg.channel_name,
            y_axis_idx=cfg.y_axis_idx, color=cfg.color,
            line_style=style, line_width=cfg.line_width,
        )
        self._active_panel.reconfigure_channel(key, new_cfg)

    def _on_channel_width_changed(self, width: float) -> None:
        if self._refreshing or self._active_panel is None:
            return
        key = self._channel_combo.currentData()
        if not key:
            return
        cfg = self._active_panel._channels.get(key)
        if cfg is None:
            return
        new_cfg = ChannelStyleConfig(
            device_path=cfg.device_path, channel_name=cfg.channel_name,
            y_axis_idx=cfg.y_axis_idx, color=cfg.color,
            line_style=cfg.line_style, line_width=width,
        )
        self._active_panel.reconfigure_channel(key, new_cfg)

    def _on_channel_visibility_changed(self, checked: bool) -> None:
        if self._refreshing or self._active_panel is None:
            return
        key = self._channel_combo.currentData()
        if not key:
            return
        if hasattr(self._active_panel, 'set_channel_visible'):
            self._active_panel.set_channel_visible(key, checked)

    def _on_channel_yaxis_changed(self, _idx: int) -> None:
        if self._refreshing or self._active_panel is None:
            return
        key = self._channel_combo.currentData()
        if not key:
            return
        target = self._yaxis_combo.currentData()
        if target is None:
            return
        self._active_panel.move_channel_to_axis(key, target)
        self.refresh()

    def _on_add_channel_to_plot(self) -> None:
        if self._active_panel is None or self._workspace is None:
            return
        data = self._add_ch_combo.currentData()
        if data is None:
            return
        device_path, channel_name = data
        # Skip if channel already on this plot.
        for existing in self._active_panel._channels.values():
            if (existing.device_path == device_path
                    and existing.channel_name == channel_name):
                return
        # Ensure at least one Y-axis exists (pyqtgraph panels only).
        if not getattr(self._active_panel, '_y_axis_cfgs', None):
            self._active_panel.add_y_axis()
        self._active_panel.add_channel(device_path, channel_name, y_axis_idx=0)
        self.refresh()

    # ── Slot handlers (axes) ─────────────────────────────────────────────────

    def _on_x_axis_changed(self, _idx: int) -> None:
        if self._refreshing or self._active_panel is None:
            return
        data = self._x_combo.currentData()
        if data is None:
            return
        dev, ch = data
        if hasattr(self._active_panel, 'set_x_channel'):
            self._active_panel.set_x_channel(dev, ch)

    def _on_axis_label_changed(self) -> None:
        if self._refreshing or self._active_panel is None:
            return
        y_axis_cfgs = getattr(self._active_panel, '_y_axis_cfgs', [])
        ax_idx = self._active_axis_idx
        if ax_idx >= len(y_axis_cfgs):
            return
        cfg = y_axis_cfgs[ax_idx]
        new_cfg = YAxisConfig(
            label=self._axis_label_edit.text().strip(),
            y_min=cfg.y_min, y_max=cfg.y_max,
            locked=cfg.locked, color=cfg.color,
        )
        self._active_panel.reconfigure_y_axis(ax_idx, new_cfg)

    def _on_autoscale_toggled(self, auto: bool) -> None:
        if self._refreshing or self._active_panel is None:
            return
        y_axis_cfgs = getattr(self._active_panel, '_y_axis_cfgs', [])
        ax_idx = self._active_axis_idx
        if not y_axis_cfgs:
            return
        while len(y_axis_cfgs) <= ax_idx:
            self._active_panel.add_y_axis()
        cfg = y_axis_cfgs[ax_idx]
        if auto:
            new_cfg = YAxisConfig(label=cfg.label, y_min=None, y_max=None,
                                  locked=False, color=cfg.color)
        else:
            new_cfg = YAxisConfig(label=cfg.label,
                                  y_min=self._min_spin.value(),
                                  y_max=self._max_spin.value(),
                                  locked=True, color=cfg.color)
        self._active_panel.reconfigure_y_axis(ax_idx, new_cfg)
        for w in (self._min_label, self._min_spin, self._max_label, self._max_spin):
            w.setVisible(not auto)

    def _on_axis_range_changed(self) -> None:
        if self._refreshing or self._active_panel is None:
            return
        if self._autoscale_cb.isChecked():
            return
        y_axis_cfgs = getattr(self._active_panel, '_y_axis_cfgs', [])
        ax_idx = self._active_axis_idx
        if ax_idx >= len(y_axis_cfgs):
            return
        cfg = y_axis_cfgs[ax_idx]
        new_cfg = YAxisConfig(
            label=cfg.label,
            y_min=self._min_spin.value(),
            y_max=self._max_spin.value(),
            locked=True, color=cfg.color,
        )
        self._active_panel.reconfigure_y_axis(ax_idx, new_cfg)

    def _on_add_y_axis(self) -> None:
        if self._workspace is None or self._active_panel is None:
            return
        side = _AxisSideDialog.ask(self)
        if side is None:
            return
        if hasattr(self._active_panel, 'add_y_axis'):
            self._active_panel.add_y_axis(side=side)
        self.refresh()

    def _on_remove_y_axis(self) -> None:
        if self._workspace is None or self._active_panel is None:
            return
        y_axis_cfgs = getattr(self._active_panel, '_y_axis_cfgs', [])
        axis_sides  = getattr(self._active_panel, '_axis_sides', [])
        if len(y_axis_cfgs) <= 1:
            QMessageBox.information(
                self, "Remove Y-Axis",
                "There is only one Y-axis — at least one must remain."
            )
            return
        choices: List[Tuple[int, str]] = []
        for i in range(1, len(y_axis_cfgs)):
            side  = axis_sides[i] if i < len(axis_sides) else "right"
            label = y_axis_cfgs[i].label
            text  = f"Axis {i} ({side.capitalize()})"
            if label:
                text += f" — {label}"
            choices.append((i, text))
        chosen = pick_axis_via_menu(self._btn_rem_axis, choices)
        if chosen is None:
            return
        if hasattr(self._active_panel, 'remove_y_axis'):
            self._active_panel.remove_y_axis(chosen)
        self.refresh()

    # ── Slot handlers (window) ───────────────────────────────────────────────

    def _on_window_changed(self, _arg=None) -> None:
        if self._refreshing or self._active_panel is None:
            return
        mode = self._show_combo.currentData()
        if mode == "all":
            self._active_panel._rolling_s = 0.0
            self._window_spin.setEnabled(False)
            self._window_unit.setEnabled(False)
        else:
            unit = self._window_unit.currentData() or "s"
            mult = _UNIT_TO_SECONDS.get(unit, 1.0)
            self._active_panel._rolling_s = self._window_spin.value() * mult
            self._window_spin.setEnabled(True)
            self._window_unit.setEnabled(True)
