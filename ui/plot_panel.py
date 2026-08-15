"""Advanced PlotPanel — core visualization widget.

Architecture
============
Each PlotPanel is a self-contained QWidget that lives inside a pyqtgraph
DockArea Dock.  It contains:

* A ``pg.PlotWidget`` as the canvas.
* A **primary ViewBox** (VB0) that owns the left Y-axis — always present.
* Up to 3 **secondary ViewBoxes** (VB1-3) for right-side Y-axes, each
  X-linked to VB0 so scrolling/zooming the X-axis stays synchronised.
* One ``pg.PlotDataItem`` per assigned *live* channel, living in the
  correct ViewBox.
* One semi-transparent ``pg.PlotDataItem`` per *overlay* channel (dashed,
  loaded from a historical CSV/Parquet file).
* ``pg.InfiniteLine`` objects (horizontal) for threshold markers.

Refresh contract
================
``panel.refresh(session_manager)`` is called by the WorkspaceWidget's
60 Hz QTimer.  It reads the latest samples from each device's ring buffer,
optionally clips to the rolling time window, and calls ``setData`` on the
corresponding live curves.  The ring buffer is **never modified** — the
rolling window only affects which slice of data is handed to the renderer.

Multiple Y-axes
===============
Call ``add_y_axis(label, y_min, y_max, locked)`` once per axis.  The
first call configures the primary left axis.  Subsequent calls create new
ViewBoxes on the right side.  PyQtGraph's ``sigResized`` callback keeps
all extra ViewBoxes spatially aligned with the primary one.

Serialization
=============
``panel.to_config()`` → dict   (suitable for JSON persistence)
``panel.restore_config(d)``    (restores axes, channels, thresholds)
"""

from __future__ import annotations

import copy
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

PLOT_WINDOW_SAMPLES = 10_000

_LINE_STYLES: Dict[str, Qt.PenStyle] = {
    "solid":   Qt.SolidLine,
    "dash":    Qt.DashLine,
    "dot":     Qt.DotLine,
    "dashdot": Qt.DashDotLine,
}

_DEFAULT_COLORS = (
    "#4FC3F7", "#FFB74D", "#81C784", "#F06292",
    "#BA68C8", "#FFD54F", "#90CAF9", "#A1887F",
    "#80CBC4", "#EF9A9A", "#CE93D8", "#80DEEA",
)


# ── Dataclasses ──────────────────────────────────────────────────────────────

@dataclass
class ChannelStyleConfig:
    device_path: str
    channel_name: str
    y_axis_idx: int = 0
    color: str = "#4FC3F7"
    line_style: str = "solid"
    line_width: float = 1.2

    def key(self) -> str:
        return f"{self.device_path}::{self.channel_name}"

    def make_pen(self) -> pg.mkPen:
        style = _LINE_STYLES.get(self.line_style, Qt.SolidLine)
        return pg.mkPen(color=self.color, width=self.line_width, style=style)

    def to_dict(self) -> dict:
        return {
            "device": self.device_path,
            "channel": self.channel_name,
            "y_axis": self.y_axis_idx,
            "color": self.color,
            "line_style": self.line_style,
            "line_width": self.line_width,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ChannelStyleConfig":
        return cls(
            device_path=d["device"],
            channel_name=d["channel"],
            y_axis_idx=int(d.get("y_axis", 0)),
            color=d.get("color", "#4FC3F7"),
            line_style=d.get("line_style", "solid"),
            line_width=float(d.get("line_width", 1.2)),
        )


@dataclass
class YAxisConfig:
    label: str = ""
    y_min: Optional[float] = None
    y_max: Optional[float] = None
    locked: bool = False
    color: str = "#cfcfcf"
    side: str = "right"   # "left" or "right" — ignored for axis 0 (always left)

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "min": self.y_min,
            "max": self.y_max,
            "locked": self.locked,
            "color": self.color,
            "side": self.side,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "YAxisConfig":
        return cls(
            label=d.get("label", ""),
            y_min=d.get("min"),
            y_max=d.get("max"),
            locked=bool(d.get("locked", False)),
            color=d.get("color", "#cfcfcf"),
            side=d.get("side", "right"),
        )


@dataclass
class ThresholdConfig:
    value: float
    color: str = "#ff4444"
    label: str = ""
    y_axis_idx: int = 0
    threshold_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])

    def to_dict(self) -> dict:
        return {
            "id": self.threshold_id,
            "value": self.value,
            "color": self.color,
            "label": self.label,
            "y_axis": self.y_axis_idx,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ThresholdConfig":
        return cls(
            value=float(d["value"]),
            color=d.get("color", "#ff4444"),
            label=d.get("label", ""),
            y_axis_idx=int(d.get("y_axis", 0)),
            threshold_id=d.get("id", str(uuid.uuid4())[:8]),
        )


@dataclass
class AlarmConfig:
    """Logic-based alarm: fires when channel operator threshold."""
    name: str
    channel_name: str
    operator: str          # '>' | '<' | '>=' | '<=' | '=='
    threshold: float
    color: str = "#ff6600"
    enabled: bool = True
    alarm_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])

    def to_dict(self) -> dict:
        return {
            "id": self.alarm_id,
            "name": self.name,
            "channel": self.channel_name,
            "operator": self.operator,
            "threshold": self.threshold,
            "color": self.color,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AlarmConfig":
        return cls(
            name=d.get("name", "Alarm"),
            channel_name=d["channel"],
            operator=d.get("operator", ">"),
            threshold=float(d.get("threshold", 0.0)),
            color=d.get("color", "#ff6600"),
            enabled=bool(d.get("enabled", True)),
            alarm_id=d.get("id", str(uuid.uuid4())[:8]),
        )


@dataclass
class XMarkerConfig:
    """Vertical InfiniteLine at a specific X (time) position."""
    x_value: float
    color: str = "#4488ff"
    label: str = ""
    marker_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])

    def to_dict(self) -> dict:
        return {
            "id": self.marker_id,
            "x": self.x_value,
            "color": self.color,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "XMarkerConfig":
        return cls(
            x_value=float(d["x"]),
            color=d.get("color", "#4488ff"),
            label=d.get("label", ""),
            marker_id=d.get("id", str(uuid.uuid4())[:8]),
        )


@dataclass
class AnalysisTagConfig:
    """Shaded LinearRegionItem marking a region of interest."""
    start: float
    end: float
    name: str = ""
    color: str = "#ffaa00"
    tag_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])

    def to_dict(self) -> dict:
        return {
            "id": self.tag_id,
            "start": self.start,
            "end": self.end,
            "name": self.name,
            "color": self.color,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AnalysisTagConfig":
        return cls(
            start=float(d["start"]),
            end=float(d["end"]),
            name=d.get("name", ""),
            color=d.get("color", "#ffaa00"),
            tag_id=d.get("id", str(uuid.uuid4())[:8]),
        )


# ── Config dialogs ────────────────────────────────────────────────────────────

class _ColorButton(QPushButton):
    """A QPushButton that shows a color swatch and opens a color picker."""

    def __init__(self, color: str = "#4FC3F7", parent=None):
        super().__init__(parent)
        self._color = color
        self._refresh()
        self.clicked.connect(self._pick)

    def color(self) -> str:
        return self._color

    def set_color(self, c: str) -> None:
        self._color = c
        self._refresh()

    def _refresh(self) -> None:
        self.setStyleSheet(
            f"background-color:{self._color}; border:1px solid #555; "
            f"min-width:48px; min-height:20px;"
        )

    def _pick(self) -> None:
        from PySide6.QtGui import QColor
        c = QColorDialog.getColor(QColor(self._color), self)
        if c.isValid():
            self.set_color(c.name())


class ChannelStyleDialog(QDialog):
    """Edit a single channel's visual style and Y-axis assignment."""

    def __init__(
        self,
        cfg: ChannelStyleConfig,
        n_axes: int,
        axis_cfgs: Optional[list] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle(f"Channel: {cfg.channel_name}")
        self.setMinimumWidth(340)
        self._cfg = cfg

        form = QFormLayout(self)
        form.setSpacing(8)

        self._color_btn = _ColorButton(cfg.color)
        form.addRow("Colour:", self._color_btn)

        self._style_combo = QComboBox()
        for s in _LINE_STYLES:
            self._style_combo.addItem(s)
        self._style_combo.setCurrentText(cfg.line_style)
        form.addRow("Line style:", self._style_combo)

        self._width_spin = QDoubleSpinBox()
        self._width_spin.setRange(0.5, 6.0)
        self._width_spin.setSingleStep(0.5)
        self._width_spin.setValue(cfg.line_width)
        form.addRow("Line width:", self._width_spin)

        self._axis_combo = self._make_axis_combo(
            n_axes, cfg.y_axis_idx, axis_cfgs or []
        )
        self._axis_combo.setToolTip(
            "Which Y-axis this channel is plotted against.\n"
            "The axis line and ticks will be tinted to match this channel's colour."
        )
        form.addRow("Y-axis:", self._axis_combo)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        form.addRow(btns)

    @staticmethod
    def _make_axis_combo(
        n_axes: int, current: int, axis_cfgs: list
    ) -> QComboBox:
        cb = QComboBox()
        for i in range(max(n_axes, 1)):
            side = "Left (primary)" if i == 0 else f"Right-{i}"
            label = ""
            if i < len(axis_cfgs) and axis_cfgs[i].label:
                label = axis_cfgs[i].label
            disp = f"Axis {i}  [{side}]"
            if label:
                disp = f"Axis {i}  [{side}] — {label}"
            cb.addItem(disp, userData=i)
        cb.setCurrentIndex(min(current, cb.count() - 1))
        return cb

    def result_config(self) -> ChannelStyleConfig:
        self._cfg.color      = self._color_btn.color()
        self._cfg.line_style = self._style_combo.currentText()
        self._cfg.line_width = self._width_spin.value()
        self._cfg.y_axis_idx = self._axis_combo.currentData() or 0
        return self._cfg


class ThresholdDialog(QDialog):
    """Add or edit a threshold InfiniteLine."""

    def __init__(self, cfg: Optional[ThresholdConfig] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Threshold Line")
        self.setMinimumWidth(360)

        outer = QVBoxLayout(self)
        outer.setSpacing(10)

        info = QLabel(
            "<b>What is a threshold?</b><br>"
            "A threshold is a horizontal reference line drawn across the plot at a "
            "fixed Y value.  It acts as a visual limit marker — for example, a red "
            "line at 85 °C makes it immediately obvious when a temperature channel "
            "crosses the danger zone, without watching the numbers tick by."
        )
        info.setWordWrap(True)
        info.setStyleSheet(
            "color:#aaaaaa; font-size:11px; "
            "background:#1e1e1e; padding:6px; border-radius:3px;"
        )
        outer.addWidget(info)

        form = QFormLayout()
        form.setSpacing(8)
        outer.addLayout(form)

        self._value_spin = QDoubleSpinBox()
        self._value_spin.setRange(-1e9, 1e9)
        self._value_spin.setDecimals(4)
        self._value_spin.setValue(cfg.value if cfg else 0.0)
        form.addRow("Value:", self._value_spin)

        self._color_btn = _ColorButton(cfg.color if cfg else "#ff4444")
        form.addRow("Color:", self._color_btn)

        self._label_edit = QLineEdit(cfg.label if cfg else "")
        form.addRow("Label:", self._label_edit)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        outer.addWidget(btns)

        self._existing_id = cfg.threshold_id if cfg else None

    def result_config(self) -> ThresholdConfig:
        cfg = ThresholdConfig(
            value=self._value_spin.value(),
            color=self._color_btn.color(),
            label=self._label_edit.text().strip(),
        )
        if self._existing_id:
            cfg.threshold_id = self._existing_id
        return cfg


class YAxisDialog(QDialog):
    """Edit a single Y-axis config.

    Autoscale is the default — the fixed-range controls only appear when
    the user explicitly enables 'Lock to fixed range'.
    """

    def __init__(self, cfg: YAxisConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Y-Axis Configuration")
        self.setMinimumWidth(340)

        outer = QVBoxLayout(self)
        outer.setSpacing(10)

        form = QFormLayout()
        form.setSpacing(8)
        outer.addLayout(form)

        self._label_edit = QLineEdit(cfg.label)
        self._label_edit.setPlaceholderText("e.g.  Voltage (V)  or  RPM")
        form.addRow("Axis label:", self._label_edit)

        self._color_btn = _ColorButton(cfg.color)
        form.addRow("Label colour:", self._color_btn)

        # ── Fixed-range group (hidden when autoscaling) ──────────────────
        self._range_group = QGroupBox("Lock to fixed range")
        self._range_group.setCheckable(True)
        self._range_group.setChecked(cfg.locked)
        self._range_group.setToolTip(
            "When checked: the Y-axis is locked to the Min/Max you specify.\n"
            "When unchecked (default): the axis auto-scales to the data."
        )
        rg_form = QFormLayout(self._range_group)
        rg_form.setSpacing(6)

        self._min_spin = QDoubleSpinBox()
        self._min_spin.setRange(-1e9, 1e9)
        self._min_spin.setDecimals(4)
        self._min_spin.setValue(cfg.y_min if cfg.y_min is not None else 0.0)
        rg_form.addRow("Min:", self._min_spin)

        self._max_spin = QDoubleSpinBox()
        self._max_spin.setRange(-1e9, 1e9)
        self._max_spin.setDecimals(4)
        self._max_spin.setValue(cfg.y_max if cfg.y_max is not None else 100.0)
        rg_form.addRow("Max:", self._max_spin)

        outer.addWidget(self._range_group)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        outer.addWidget(btns)

    def result_config(self) -> YAxisConfig:
        locked = self._range_group.isChecked()
        return YAxisConfig(
            label=self._label_edit.text().strip(),
            color=self._color_btn.color(),
            locked=locked,
            y_min=self._min_spin.value() if locked else None,
            y_max=self._max_spin.value() if locked else None,
        )


# ── PlotPanel ─────────────────────────────────────────────────────────────────

class PlotPanel(QWidget):
    """Advanced enterprise plot panel with multi-axis, rolling window,
    threshold markers, historical overlay, and full config serialization."""

    # Emitted at 60 Hz whenever an alarm transitions False→True.
    # Args: alarm_name, channel_name, value, pc_timestamp
    alarm_triggered = Signal(str, str, float, float)

    # Emitted when a channel is moved to a different Y-axis.
    # Args: channel_key, new_axis_idx (-1 = removed from plot)
    channel_axis_changed = Signal(str, int)

    def __init__(self, panel_id: str, title: str = "Plot",
                 show_toolbar: bool = True, parent=None) -> None:
        super().__init__(parent)
        self._panel_id   = panel_id
        self._title      = title
        self._show_toolbar = show_toolbar
        self._rolling_s: float = 0.0

        # X-axis assignment (None = use PC_Time_s)
        self._x_device:   Optional[str] = None
        self._x_ch_name:  Optional[str] = None

        # Per-channel configs, live curves, and visibility state
        self._channels: Dict[str, ChannelStyleConfig] = {}
        self._live_curves: Dict[str, pg.PlotDataItem] = {}
        self._channel_visible: Dict[str, bool] = {}   # True = shown, False = hidden

        # Y-axis configs and corresponding ViewBoxes
        self._y_axis_cfgs: List[YAxisConfig] = []
        self._all_vbs: List[pg.ViewBox] = []    # index = axis_idx; [0] = primary
        self._extra_vbs: List[pg.ViewBox] = []  # [0] = axis 1, [1] = axis 2, …
        self._extra_axis_items: List[pg.AxisItem] = []
        # Side ('left' | 'right') for each axis_idx. axis 0 is always 'left'.
        self._axis_sides: List[str] = []

        # Thresholds
        self._threshold_cfgs: Dict[str, ThresholdConfig] = {}
        self._threshold_lines: Dict[str, pg.InfiniteLine] = {}

        # ── Alarms ───────────────────────────────────────────────────
        self._alarm_cfgs: Dict[str, AlarmConfig] = {}
        # Per-alarm ScatterPlotItem accumulating trigger points
        self._alarm_marker_items: Dict[str, pg.ScatterPlotItem] = {}
        self._alarm_marker_pts: Dict[str, Tuple[List[float], List[float]]] = {}
        # Rising-edge tracking: was alarm condition true on the previous frame?
        self._alarm_prev_state: Dict[str, bool] = {}

        # ── X-axis markers (vertical InfiniteLines) ──────────────────
        self._x_marker_cfgs: Dict[str, XMarkerConfig] = {}
        self._x_marker_lines: Dict[str, pg.InfiniteLine] = {}

        # ── Analysis tags (LinearRegionItem) ─────────────────────────
        self._tag_cfgs: Dict[str, AnalysisTagConfig] = {}
        self._tag_items: Dict[str, pg.LinearRegionItem] = {}

        # Custom-math formulas plotted on this panel by the Analysis tab.
        # _formula_specs persists across save/load; _formula_curves holds the
        # live PlotDataItem instances so we can swap them on re-run.
        self._formula_specs: List[Dict[str, Any]] = []
        self._formula_curves: Dict[str, pg.PlotDataItem] = {}

        # Historical overlay
        self._overlay_data: Dict[str, Tuple[np.ndarray, np.ndarray]] = {}
        self._overlay_curves: Dict[str, pg.PlotDataItem] = {}
        # id(curve) -> the ViewBox we added it to. Used by clear_overlay so we
        # only call removeItem on the actual owning VB (otherwise PyQtGraph
        # spits "QGraphicsScene::removeItem: item's scene (0x0) is different"
        # warnings for every non-owner VB).
        self._overlay_curve_vb: Dict[int, pg.ViewBox] = {}
        self._overlay_path: Optional[str] = None     # serialised path

        # Per-device sample-rate estimates (updated lazily during refresh).
        # Used to compute the minimum number of samples needed for the current
        # rolling window — avoids copying 10 k samples when only 500 are shown.
        self._est_rate: Dict[str, float] = {}

        # Build UI
        self._build_ui()
        self._apply_theme()

    # ── UI construction ──────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)

        # ── Toolbar ──
        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)

        self._title_edit = QLineEdit(self._title)
        self._title_edit.setMaximumWidth(150)
        self._title_edit.setPlaceholderText("Plot title")
        self._title_edit.textChanged.connect(self._on_title_changed)
        toolbar.addWidget(QLabel("Title:"))
        toolbar.addWidget(self._title_edit)

        sep = QWidget(); sep.setFixedWidth(1)
        sep.setStyleSheet("background:#444")
        toolbar.addWidget(sep)

        self._x_combo = QComboBox()
        self._x_combo.setMinimumWidth(160)
        self._x_combo.setToolTip(
            "Channel to use as the X-axis (default: PC_Time_s timestamp)"
        )
        self._x_combo.addItem("PC_Time_s (timestamp)", userData=(None, None))
        self._x_combo.currentIndexChanged.connect(self._on_x_axis_changed)
        toolbar.addWidget(QLabel("X:"))
        toolbar.addWidget(self._x_combo)

        sep2 = QWidget(); sep2.setFixedWidth(1)
        sep2.setStyleSheet("background:#444")
        toolbar.addWidget(sep2)

        self._rolling_spin = QDoubleSpinBox()
        self._rolling_spin.setRange(0.1, 3600.0)
        self._rolling_spin.setSingleStep(1.0)
        self._rolling_spin.setDecimals(1)
        self._rolling_spin.setValue(10.0)
        self._rolling_spin.setMaximumWidth(90)
        self._rolling_spin.setEnabled(False)   # disabled until unit is chosen
        self._rolling_spin.setToolTip(
            "Show only the most recent window of data on the X-axis.\n"
            "Older data stays safely in the ring buffer — only the displayed\n"
            "view is clipped.  Select a time unit with the dropdown to the right."
        )
        self._rolling_spin.valueChanged.connect(self._on_rolling_value_changed)

        self._unit_combo = QComboBox()
        self._unit_combo.setMaximumWidth(82)
        self._unit_combo.addItem("All data", userData=("all", 1.0))
        self._unit_combo.addItem("ms",       userData=("ms",  0.001))
        self._unit_combo.addItem("s",        userData=("s",   1.0))
        self._unit_combo.addItem("min",      userData=("min", 60.0))
        self._unit_combo.addItem("h",        userData=("h",   3600.0))
        self._unit_combo.setCurrentIndex(0)
        self._unit_combo.setToolTip(
            "Time unit for the rolling window.\n"
            "Choose 'All data' to show the full session history."
        )
        self._unit_combo.currentIndexChanged.connect(self._on_rolling_unit_changed)

        toolbar.addWidget(QLabel("Window:"))
        toolbar.addWidget(self._rolling_spin)
        toolbar.addWidget(self._unit_combo)

        toolbar.addStretch()

        # Whether to actually attach the toolbar to the visible layout —
        # the enterprise workspace hides it (controls live in the
        # unified Settings Panel) but tests and the standalone path keep it.

        btn_cfg = QPushButton("Configure")
        btn_cfg.setToolTip(
            "Configure this plot:\n"
            "• Y-Axes — add independent scales for channels with very different ranges\n"
            "  (e.g. 0–5 V and 0–4000 RPM on the same graph without either signal\n"
            "  being flattened by the other's scale)\n"
            "• Channels — change colour, line style, line width, and axis assignment\n"
            "• Thresholds — add horizontal limit/warning lines at specific Y values"
        )
        btn_cfg.clicked.connect(self._show_config_dialog)
        toolbar.addWidget(btn_cfg)

        btn_overlay = QPushButton("📁  Load Overlay")
        btn_overlay.setToolTip(
            "Load Overlay — open a previously recorded CSV or Parquet file and draw\n"
            "its channels as faded dashed curves behind the live data on this plot.\n"
            "The historical data is frozen in the background while your live stream\n"
            "updates on top — letting you visually compare a past test run against\n"
            "the current one in real time without switching windows."
        )
        btn_overlay.clicked.connect(self._load_overlay_clicked)
        toolbar.addWidget(btn_overlay)

        # in the new enterprise workspace, all controls live in the
        # unified Settings Panel.  Only attach the toolbar when explicitly
        # requested (standalone usage, tests, debugging).
        if self._show_toolbar:
            outer.addLayout(toolbar)

        # ── Plot widget ──
        self._plot_widget = pg.PlotWidget()
        self._plot_item   = self._plot_widget.getPlotItem()
        self._legend      = self._plot_item.addLegend(offset=(10, 10))
        self._plot_item.vb.setMenuEnabled(False)
        outer.addWidget(self._plot_widget, stretch=1)

        # Primary ViewBox is always axis 0.
        self._all_vbs = [self._plot_item.vb]

    def _apply_theme(self) -> None:
        self._plot_widget.setBackground("#121212")
        self._plot_widget.showGrid(x=True, y=True, alpha=0.15)
        self._plot_widget.setAntialiasing(False)
        self._plot_widget.setMouseEnabled(x=True, y=True)
        self._plot_item.getAxis("left").setPen(pg.mkPen("#555"))
        self._plot_item.getAxis("bottom").setPen(pg.mkPen("#555"))
        self._plot_item.setLabel("bottom", "Time (s)")

    # ── Y-axis management ────────────────────────────────────────────────────

    def add_y_axis(
        self,
        label: str = "",
        y_min: Optional[float] = None,
        y_max: Optional[float] = None,
        locked: bool = False,
        color: str = "#cfcfcf",
        side: str = "right",
    ) -> int:
        """Add a Y-axis and return its 0-based index.

        The *first* call configures the primary left axis. Subsequent calls
        create new ViewBoxes on either the left or right side of the plot,
        controlled by *side*.
        """
        effective_side = "left" if len(self._y_axis_cfgs) == 0 else (
            side if side in ("left", "right") else "right"
        )
        cfg = YAxisConfig(label=label, y_min=y_min, y_max=y_max,
                          locked=locked, color=color, side=effective_side)
        self._y_axis_cfgs.append(cfg)
        axis_idx = len(self._y_axis_cfgs) - 1

        if axis_idx == 0:
            # Primary axis is always the built-in left.
            self._axis_sides.append("left")
            pi = self._plot_item
            if label:
                pi.setLabel("left", label, color=color)
            vb = pi.vb
            self._apply_axis_range(vb, cfg)
        else:
            self._axis_sides.append(side if side in ("left", "right") else "right")
            vb = self._create_extra_vb(axis_idx, cfg, self._axis_sides[-1])
            self._extra_vbs.append(vb)
            self._all_vbs.append(vb)
            self._relayout_axes()
            self._update_vb_geometries()

        return axis_idx

    def _create_extra_vb(self, axis_idx: int, cfg: YAxisConfig, side: str) -> pg.ViewBox:
        """Create a new ViewBox + AxisItem on the given *side*."""
        pi = self._plot_item
        vb = pg.ViewBox()

        # Always create a fresh AxisItem on the requested side. We deliberately
        # avoid reusing the built-in right axis so that multi-left layouts can
        # be reshuffled freely by _relayout_axes().
        axis_item = pg.AxisItem("left" if side == "left" else "right")
        if cfg.label:
            axis_item.setLabel(cfg.label, color=cfg.color)
        pi.scene().addItem(vb)
        axis_item.linkToView(vb)

        self._extra_axis_items.append(axis_item)
        vb.setXLink(pi)
        vb.setZValue(-axis_idx)  # primary VB on top visually
        self._apply_axis_range(vb, cfg)

        # Keep geometry in sync with primary on resize.
        if len(self._extra_vbs) == 0:          # connect once
            pi.vb.sigResized.connect(self._update_vb_geometries)

        return vb

    @staticmethod
    def _grid_remove(layout, item) -> bool:
        """Remove *item* from *layout* by index, no-op if not present.

        QGraphicsGridLayout.removeItem warns to stderr ("removeAt: invalid
        index -1") when given an item it doesn't own. We avoid the warning
        by scanning for the item first.
        """
        for i in range(layout.count() - 1, -1, -1):
            if layout.itemAt(i) is item:
                layout.removeAt(i)
                return True
        return False

    def _relayout_axes(self) -> None:
        """Re-position the primary axes + every extra AxisItem in the grid.

        Order (left-to-right):
            extra-left axes | primary left | plot | primary right | extra-right axes
        """
        pi = self._plot_item
        layout = pi.layout
        primary_left  = pi.getAxis("left")
        primary_right = pi.getAxis("right")
        plot_vb       = pi.vb

        for item in [primary_left, primary_right, plot_vb] + list(self._extra_axis_items):
            self._grid_remove(layout, item)

        # Hide the built-in right axis — we control all non-primary axes via
        # _extra_axis_items now.
        pi.hideAxis("right")

        col = 0
        # Extra LEFT axes (in insertion order, leftmost = first added).
        for i, side in enumerate(self._axis_sides[1:], start=1):
            if side == "left":
                ax = self._extra_axis_items[i - 1]
                layout.addItem(ax, 2, col); col += 1
                ax.show()
        # Primary left.
        layout.addItem(primary_left, 2, col); col += 1
        # Plot area.
        layout.addItem(plot_vb, 2, col); col += 1
        # Primary right slot (kept hidden but in grid for layout consistency).
        layout.addItem(primary_right, 2, col); col += 1
        # Extra RIGHT axes.
        for i, side in enumerate(self._axis_sides[1:], start=1):
            if side == "right":
                ax = self._extra_axis_items[i - 1]
                layout.addItem(ax, 2, col); col += 1
                ax.show()

    def remove_y_axis(self, axis_idx: int) -> bool:
        """Remove the Y-axis at *axis_idx* (1+ only — axis 0 is the primary).

        Any channels assigned to the removed axis are migrated to axis 0.
        Returns True on success. Removing axis 0 is rejected.
        """
        if axis_idx <= 0 or axis_idx >= len(self._y_axis_cfgs):
            return False

        # Migrate channels off the removed axis BEFORE the indices shift.
        for key, cfg in list(self._channels.items()):
            if cfg.y_axis_idx == axis_idx:
                self.move_channel_to_axis(key, 0)
            elif cfg.y_axis_idx > axis_idx:
                self.move_channel_to_axis(key, cfg.y_axis_idx - 1)

        # Same shift for threshold axis indices.
        for tcfg in self._threshold_cfgs.values():
            if tcfg.y_axis_idx == axis_idx:
                tcfg.y_axis_idx = 0
            elif tcfg.y_axis_idx > axis_idx:
                tcfg.y_axis_idx -= 1

        # Drop the axis from every parallel list.
        extra_idx = axis_idx - 1     # _extra_* lists are 1-offset from _y_axis_cfgs
        vb = self._all_vbs.pop(axis_idx)
        self._extra_vbs.pop(extra_idx)
        ax_item = self._extra_axis_items.pop(extra_idx)
        self._y_axis_cfgs.pop(axis_idx)
        self._axis_sides.pop(axis_idx)

        # Detach the ViewBox + AxisItem from the scene/layout.
        try:
            self._plot_item.scene().removeItem(vb)
        except Exception:
            pass
        self._grid_remove(self._plot_item.layout, ax_item)

        # Re-pack the layout and sync colours so the remaining axes line up.
        self._relayout_axes()
        self._update_vb_geometries()
        self._sync_axis_colors()
        return True

    def _apply_axis_range(self, vb: pg.ViewBox, cfg: YAxisConfig) -> None:
        if cfg.locked and cfg.y_min is not None and cfg.y_max is not None:
            vb.setRange(yRange=(cfg.y_min, cfg.y_max), padding=0)
            vb.disableAutoRange(axis="y")
            vb.setMouseEnabled(x=True, y=False)
        else:
            vb.enableAutoRange(axis="y")
            vb.setMouseEnabled(x=True, y=True)

    def _update_vb_geometries(self) -> None:
        """Keep all extra ViewBoxes geometrically aligned with the primary."""
        r = self._plot_item.vb.sceneBoundingRect()
        if r.isEmpty():
            return
        for vb in self._extra_vbs:
            vb.setGeometry(r)
            vb.linkedViewChanged(self._plot_item.vb, vb.XAxis)

    def _sync_axis_colors(self) -> None:
        """Tint each Y-axis line/ticks/label to match the first channel on it.

        This makes the visual correspondence obvious: the blue axis owns the
        blue channel, the orange axis owns the orange channel, etc.  If no
        channel is assigned to an axis the axis falls back to its own
        YAxisConfig.color.
        """
        pi = self._plot_item
        for ax_idx, ax_cfg in enumerate(self._y_axis_cfgs):
            channels_here = [
                c for c in self._channels.values() if c.y_axis_idx == ax_idx
            ]
            color = channels_here[0].color if channels_here else ax_cfg.color
            pen = pg.mkPen(color=color)
            text_pen = pg.mkPen(color=color)

            if ax_idx == 0:
                pi.getAxis("left").setPen(pen)
                pi.getAxis("left").setTextPen(text_pen)
            elif ax_idx - 1 < len(self._extra_axis_items):
                ax = self._extra_axis_items[ax_idx - 1]
                ax.setPen(pen)
                ax.setTextPen(text_pen)

    def reconfigure_y_axis(self, axis_idx: int, cfg: YAxisConfig) -> None:
        """Apply updated YAxisConfig to an existing axis."""
        if axis_idx >= len(self._y_axis_cfgs):
            return
        self._y_axis_cfgs[axis_idx] = cfg
        pi = self._plot_item
        if axis_idx == 0:
            pi.setLabel("left", cfg.label, color=cfg.color)
            self._apply_axis_range(self._all_vbs[0], cfg)
        elif axis_idx < len(self._all_vbs):
            ax = self._extra_axis_items[axis_idx - 1]
            ax.setLabel(cfg.label, color=cfg.color)
            self._apply_axis_range(self._all_vbs[axis_idx], cfg)
        self._sync_axis_colors()

    # ── Channel management ───────────────────────────────────────────────────

    def add_channel(
        self,
        device_path: str,
        channel_name: str,
        y_axis_idx: int = 0,
        color: Optional[str] = None,
        line_style: str = "solid",
        line_width: float = 1.2,
    ) -> str:
        """Add a live channel.  Returns the channel key."""
        if color is None:
            color = _DEFAULT_COLORS[len(self._channels) % len(_DEFAULT_COLORS)]

        cfg = ChannelStyleConfig(
            device_path=device_path,
            channel_name=channel_name,
            y_axis_idx=y_axis_idx,
            color=color,
            line_style=line_style,
            line_width=line_width,
        )
        key = cfg.key()
        self._channels[key] = cfg

        # Ensure the target axis exists (add primary if needed).
        while len(self._y_axis_cfgs) <= y_axis_idx:
            self.add_y_axis()

        vb = self._all_vbs[y_axis_idx] if y_axis_idx < len(self._all_vbs) else self._all_vbs[0]

        curve = pg.PlotDataItem(pen=cfg.make_pen(), name=channel_name)
        curve.setDownsampling(auto=True, method="peak")
        # Do NOT setClipToView on secondary-axis curves — those ViewBoxes may
        # not have geometry until the first resize, causing spurious clipping.
        if y_axis_idx == 0:
            curve.setClipToView(True)
        vb.addItem(curve)
        self._live_curves[key] = curve
        if self._legend is not None:
            self._legend.addItem(curve, channel_name)

        # Also update x-combo with new channel option.
        self._x_combo.addItem(
            f"{device_path.split('/')[-1]} · {channel_name}",
            userData=(device_path, channel_name),
        )
        self._channel_visible[key] = True
        self._sync_axis_colors()
        # Notify Channel Manager and Settings sidebar that this channel exists.
        self.channel_axis_changed.emit(key, y_axis_idx)
        return key

    def set_channel_visible(self, key: str, visible: bool) -> None:
        """Show or hide a channel's curve without removing it."""
        self._channel_visible[key] = visible
        curve = self._live_curves.get(key)
        if curve is None:
            return
        curve.setVisible(visible)
        # pyqtgraph's LegendItem does NOT auto-hide when setVisible(False) is called,
        # so we manually sync the legend entry.
        if self._legend is not None:
            cfg = self._channels.get(key)
            try:
                self._legend.removeItem(curve)
            except Exception:
                pass
            if visible and cfg:
                self._legend.addItem(curve, cfg.channel_name)

    def remove_channel(self, key: str) -> None:
        self._channel_visible.pop(key, None)
        cfg = self._channels.pop(key, None)
        curve = self._live_curves.pop(key, None)
        if curve is not None:
            try:
                vb_idx = cfg.y_axis_idx if cfg else 0
                vb = self._all_vbs[vb_idx] if vb_idx < len(self._all_vbs) else self._all_vbs[0]
                vb.removeItem(curve)
            except Exception:
                pass
            if self._legend is not None:
                try:
                    self._legend.removeItem(curve)
                except Exception:
                    pass
        self._sync_axis_colors()

    def move_channel_to_axis(self, key: str, target) -> int:
        """Move *key* to axis index *target* (int) or create a new axis ('new').

        Returns the final axis index. Emits channel_axis_changed.
        """
        cfg = self._channels.get(key)
        if cfg is None:
            return 0
        if target == "new":
            target = self.add_y_axis()
        target = int(target)
        if target == cfg.y_axis_idx:
            return target
        self.remove_channel(key)
        while len(self._y_axis_cfgs) <= target:
            self.add_y_axis()
        self.add_channel(cfg.device_path, cfg.channel_name,
                         y_axis_idx=target, color=cfg.color,
                         line_style=cfg.line_style, line_width=cfg.line_width)
        self.channel_axis_changed.emit(key, target)
        return target

    def _curve_to_key(self, item) -> Optional[str]:
        for k, curve in self._live_curves.items():
            if curve is item:
                return k
        return None

    def _edit_channel_style(self, key: str) -> None:
        cfg = self._channels.get(key)
        if cfg is None:
            return
        dlg = ChannelStyleDialog(
            copy.copy(cfg),
            self.n_y_axes,
            self._y_axis_cfgs,
            parent=self,
        )
        if dlg.exec() == QDialog.Accepted:
            new_cfg = dlg.result_config()
            if new_cfg.y_axis_idx != cfg.y_axis_idx:
                self.move_channel_to_axis(key, new_cfg.y_axis_idx)
                # Update color/style on the newly re-added channel
                updated_key = new_cfg.key()
                if updated_key in self._channels:
                    self._channels[updated_key].color      = new_cfg.color
                    self._channels[updated_key].line_style = new_cfg.line_style
                    self._channels[updated_key].line_width = new_cfg.line_width
                    self.reconfigure_channel(updated_key, self._channels[updated_key])
            else:
                self.reconfigure_channel(key, new_cfg)

    def reconfigure_channel(self, key: str, new_cfg: ChannelStyleConfig) -> None:
        """Apply updated style to an existing channel."""
        self._channels[key] = new_cfg
        curve = self._live_curves.get(key)
        if curve is not None:
            curve.setPen(new_cfg.make_pen())
        self._sync_axis_colors()

    # ── Threshold management ─────────────────────────────────────────────────

    def add_threshold(self, cfg: ThresholdConfig) -> str:
        """Add a horizontal InfiniteLine on the specified Y-axis ViewBox."""
        tid = cfg.threshold_id
        pen = pg.mkPen(color=cfg.color, width=1.5, style=Qt.DashLine)
        line = pg.InfiniteLine(
            pos=cfg.value,
            angle=0,
            pen=pen,
            movable=True,
            label=cfg.label,
            labelOpts={"color": cfg.color, "fill": (0, 0, 0, 100)},
        )
        self._threshold_cfgs[tid] = cfg
        self._threshold_lines[tid] = line
        vb = (self._all_vbs[cfg.y_axis_idx]
              if cfg.y_axis_idx < len(self._all_vbs) else self._all_vbs[0])
        vb.addItem(line)
        return tid

    def remove_threshold(self, threshold_id: str) -> None:
        cfg  = self._threshold_cfgs.pop(threshold_id, None)
        line = self._threshold_lines.pop(threshold_id, None)
        if line is not None:
            ax_idx = cfg.y_axis_idx if cfg else 0
            vb = (self._all_vbs[ax_idx]
                  if ax_idx < len(self._all_vbs) else self._all_vbs[0])
            try:
                vb.removeItem(line)
            except Exception:
                pass

    # ── Alarm management ─────────────────────────────────────────────────────

    def add_alarm(self, cfg: AlarmConfig) -> str:
        """Register a logic alarm.  Returns alarm_id."""
        self._alarm_cfgs[cfg.alarm_id] = cfg
        self._alarm_prev_state.setdefault(cfg.alarm_id, False)
        return cfg.alarm_id

    def remove_alarm(self, alarm_id: str) -> None:
        self._alarm_cfgs.pop(alarm_id, None)
        self._alarm_prev_state.pop(alarm_id, None)
        scatter = self._alarm_marker_items.pop(alarm_id, None)
        if scatter is not None:
            for vb in self._all_vbs:
                try:
                    vb.removeItem(scatter)
                except Exception:
                    pass
        self._alarm_marker_pts.pop(alarm_id, None)

    @staticmethod
    def _eval_alarm_op(op: str, value: float, threshold: float) -> bool:
        if op == ">":   return value > threshold
        if op == "<":   return value < threshold
        if op == ">=":  return value >= threshold
        if op == "<=":  return value <= threshold
        if op == "==":  return abs(value - threshold) < 1e-9
        return False

    def _fire_alarm_marker(
        self, alarm_id: str, cfg: AlarmConfig,
        ts: float, val: float, y_axis_idx: int,
    ) -> None:
        vb = (self._all_vbs[y_axis_idx]
              if y_axis_idx < len(self._all_vbs) else self._all_vbs[0])
        scatter = self._alarm_marker_items.get(alarm_id)
        if scatter is None:
            scatter = pg.ScatterPlotItem(
                size=11,
                brush=pg.mkBrush(cfg.color),
                pen=pg.mkPen("#ffffff", width=1),
                symbol="t",          # filled triangle
            )
            vb.addItem(scatter)
            self._alarm_marker_items[alarm_id] = scatter
        xs, ys = self._alarm_marker_pts.get(alarm_id, ([], []))
        xs = list(xs) + [ts]
        ys = list(ys) + [val]
        self._alarm_marker_pts[alarm_id] = (xs, ys)
        scatter.setData(xs, ys)

    # ── X-marker management (vertical InfiniteLines) ─────────────────────────

    def add_x_marker(self, cfg: XMarkerConfig) -> str:
        """Add a vertical time marker.  Returns marker_id."""
        pen = pg.mkPen(color=cfg.color, width=1.5, style=Qt.DashDotLine)
        line = pg.InfiniteLine(
            pos=cfg.x_value,
            angle=90,
            pen=pen,
            movable=True,
            label=cfg.label,
            labelOpts={"color": cfg.color, "fill": (0, 0, 0, 100),
                       "position": 0.95},
        )
        mid = cfg.marker_id
        self._x_marker_cfgs[mid] = cfg
        self._x_marker_lines[mid] = line
        self._plot_item.vb.addItem(line)

        # Keep config in sync when user drags the line.
        line.sigPositionChanged.connect(
            lambda ln, m=mid: self._on_x_marker_moved(m, ln)
        )
        return mid

    def _on_x_marker_moved(self, marker_id: str, line: pg.InfiniteLine) -> None:
        cfg = self._x_marker_cfgs.get(marker_id)
        if cfg is not None:
            cfg.x_value = float(line.value())

    def remove_x_marker(self, marker_id: str) -> None:
        line = self._x_marker_lines.pop(marker_id, None)
        if line is not None:
            try:
                self._plot_item.vb.removeItem(line)
            except Exception:
                pass
        self._x_marker_cfgs.pop(marker_id, None)

    # ── Analysis tag management (LinearRegionItem) ───────────────────────────

    def add_analysis_tag(self, cfg: AnalysisTagConfig) -> str:
        """Add a shaded region-of-interest tag.  Returns tag_id."""
        from PySide6.QtGui import QColor
        c = QColor(cfg.color)
        brush = pg.mkBrush(c.red(), c.green(), c.blue(), 40)
        pen   = pg.mkPen(cfg.color, width=1.5)
        region = pg.LinearRegionItem(
            values=[cfg.start, cfg.end],
            brush=brush,
            pen=pen,
            movable=True,
        )
        region.setZValue(10)

        # Floating label above the region.
        if cfg.name:
            label = pg.InfLineLabel(
                region.lines[0],
                text=cfg.name,
                movable=False,
                position=0.9,
                color=cfg.color,
            )

        tid = cfg.tag_id
        self._tag_cfgs[tid] = cfg
        self._tag_items[tid] = region
        self._plot_item.vb.addItem(region)

        # Keep cfg in sync when the user drags the region.
        region.sigRegionChanged.connect(
            lambda r, t=tid: self._on_tag_moved(t, r)
        )
        return tid

    def _on_tag_moved(self, tag_id: str, region: pg.LinearRegionItem) -> None:
        cfg = self._tag_cfgs.get(tag_id)
        if cfg is not None:
            rng = region.getRegion()
            cfg.start = float(rng[0])
            cfg.end   = float(rng[1])

    def remove_analysis_tag(self, tag_id: str) -> None:
        region = self._tag_items.pop(tag_id, None)
        if region is not None:
            try:
                self._plot_item.vb.removeItem(region)
            except Exception:
                pass
        self._tag_cfgs.pop(tag_id, None)

    # ── X-axis assignment ────────────────────────────────────────────────────

    def set_x_channel(self, device_path: Optional[str],
                      channel_name: Optional[str]) -> None:
        self._x_device  = device_path
        self._x_ch_name = channel_name
        if device_path is None:
            self._plot_item.setLabel("bottom", "Time (s)")
        else:
            self._plot_item.setLabel("bottom", channel_name or "")

    def update_x_combo_choices(self, available: List[Tuple[str, str, str]]) -> None:
        """Populate the X-axis combo with (device_path, ch_name, label) triples."""
        current_data = self._x_combo.currentData()
        self._x_combo.blockSignals(True)
        self._x_combo.clear()
        self._x_combo.addItem("PC_Time_s (timestamp)", userData=(None, None))
        for dev_path, ch_name, label in available:
            self._x_combo.addItem(label, userData=(dev_path, ch_name))
        # Restore previous selection.
        for i in range(self._x_combo.count()):
            if self._x_combo.itemData(i) == current_data:
                self._x_combo.setCurrentIndex(i)
                break
        self._x_combo.blockSignals(False)

    # ── Historical overlay ───────────────────────────────────────────────────

    def load_overlay(
        self,
        csv_path: str,
        channel_configs: Optional[List[ChannelStyleConfig]] = None,
        channel_filter: Optional[set] = None,
        clear: bool = True,
        faded: bool = False,
    ) -> None:
        """Load a CSV/Parquet file and render its columns as background overlays.

        Resilient to recordings that were corrupted by two device loggers
        writing to the same file: rows with the wrong column count are
        skipped, and any cell that can't be parsed as a float becomes NaN
        (and is then dropped pairwise with its time-stamp).

        Parameters
        ----------
        csv_path
            File to read.
        channel_configs
            Optional list of saved ChannelStyleConfigs to colour-match.
        channel_filter
            Optional set of bare channel names to RENDER. Columns whose bare
            name isn't in the set are ignored. Use this in post-analysis so a
            panel only draws the channels it actually owns — without it every
            column from every file lands on every panel, mixing legends and
            wiping data when multiple files load into the same panel.
        clear
            When True (default) the previous overlay is wiped first. Set
            False in post-analysis so multiple per-device files can stack
            onto the same panel.
        """
        import pandas as pd

        if clear:
            self.clear_overlay()
        self._overlay_path = os.path.abspath(csv_path)

        ext = os.path.splitext(csv_path)[1].lower()
        df = None
        if ext in (".parquet", ".pq"):
            try:
                df = pd.read_parquet(csv_path)
            except Exception as exc:
                raise ValueError(f"Cannot load overlay file: {exc}") from exc
        else:
            # Read every cell as a string so type inference can't choke on
            # corrupted values mid-file. We coerce to numeric below, turning
            # any unparseable cell (e.g. two floats stomped together with no
            # delimiter) into NaN instead of raising.
            read_errors: list[str] = []
            for engine in ("c", "python"):
                try:
                    df = pd.read_csv(
                        csv_path,
                        on_bad_lines="skip",   # rows with wrong NF get dropped
                        engine=engine,
                        dtype=str,
                        low_memory=False,
                        encoding_errors="replace",
                    )
                    break
                except Exception as exc:
                    read_errors.append(f"{engine}: {exc}")
            if df is None:
                raise ValueError(
                    "Cannot load overlay file: " + " | ".join(read_errors)
                )

        cols = list(df.columns)
        if len(cols) < 2:
            raise ValueError(
                f"Overlay file has too few columns ({len(cols)}) — expected "
                "PC_Time_s plus at least one channel."
            )
        x_col = cols[0]   # first column is always X (usually PC_Time_s)
        # Coerce x to numeric; any corrupt cell becomes NaN.
        x_arr = pd.to_numeric(df[x_col], errors="coerce").to_numpy(dtype=np.float64)

        # DataLogger writes column names like "Voltage[V]" (name + unit suffix).
        # Plot configs only store the bare channel name, so we strip the
        # bracketed suffix when looking up so colours/axes match the live config.
        def _bare(col_name: str) -> str:
            i = col_name.find("[")
            return col_name[:i].strip() if i > 0 else col_name

        cfg_map: Dict[str, ChannelStyleConfig] = {}
        if channel_configs:
            for cfg in channel_configs:
                cfg_map[cfg.channel_name] = cfg

        touched_vbs: set = set()
        rows_drawn = 0
        for col in cols[1:]:
            if col not in df.columns:
                continue
            if channel_filter is not None and _bare(col) not in channel_filter:
                continue   # post-analysis: skip columns this panel doesn't own
            # Coerce-to-numeric forgives garbage (returns NaN) instead of
            # raising. This is what rescues files corrupted by interleaved
            # writes — the bad cell becomes NaN and the row is dropped.
            y_raw = pd.to_numeric(df[col], errors="coerce").to_numpy(dtype=np.float64)
            # Drop NaN rows pairwise with x.
            mask  = ~(np.isnan(y_raw) | np.isnan(x_arr))
            if not mask.any():
                continue
            x_use = x_arr[mask]
            y_arr = y_raw[mask]
            rows_drawn = max(rows_drawn, int(mask.sum()))

            cfg = cfg_map.get(_bare(col)) or cfg_map.get(col)
            if cfg:
                color = cfg.color
                width = max(cfg.line_width, 1.4)
                y_ax  = cfg.y_axis_idx
            else:
                color = "#cfcfcf"
                width = 1.4
                y_ax  = 0

            # Use a solid line in post-analysis mode so the historical data is
            # easy to see. The dashed/faded style only made sense as an overlay
            # on top of a live trace. regression comparison opts
            # into the faded/dashed look so a baseline session sits visibly
            # *behind* the current run.
            if faded:
                pen = pg.mkPen(color=color, width=max(width - 0.2, 1.0),
                               style=Qt.DashLine)
            else:
                pen = pg.mkPen(color=color, width=width)
            curve = pg.PlotDataItem(x_use, y_arr, pen=pen, name=_bare(col))
            curve.setAlpha(0.5 if faded else 0.95, False)

            vb = self._all_vbs[y_ax] if y_ax < len(self._all_vbs) else self._all_vbs[0]
            vb.addItem(curve)
            touched_vbs.add(vb)
            if self._legend is not None:
                # Suppress duplicate legend entries: if a live curve already
                # carries this channel name (which it does in post-analysis,
                # because restore_config calls add_channel for every saved
                # channel), don't add the overlay too — both have the same
                # colour and would produce two identical legend rows.
                bare = _bare(col)
                live_already = any(
                    c.channel_name == bare for c in self._channels.values()
                )
                if not live_already:
                    try:
                        self._legend.addItem(curve, bare)
                    except Exception:
                        pass
            self._overlay_curves[col] = curve
            self._overlay_curve_vb[id(curve)] = vb
            self._overlay_data[col] = (x_use, y_arr)

        # Fit the view to the loaded data — without this, an empty live plot's
        # default (0, 1) range keeps the recorded data off-screen.
        for vb in touched_vbs:
            try:
                vb.enableAutoRange(axis="xy")
                vb.autoRange()
            except Exception:
                pass

        # Stash the count so the post-analysis loader can surface it in the
        # status bar — "loaded 7685 rows" beats silent success.
        self._overlay_last_rows = rows_drawn

    def clear_overlay(self) -> None:
        for curve in self._overlay_curves.values():
            owner = self._overlay_curve_vb.get(id(curve))
            if owner is not None:
                try:
                    owner.removeItem(curve)
                except Exception:
                    pass
            # Also detach from the legend so re-loads don't accumulate ghost
            # entries that reference now-removed curves.
            if self._legend is not None:
                try:
                    self._legend.removeItem(curve)
                except Exception:
                    pass
        self._overlay_curves.clear()
        self._overlay_data.clear()
        self._overlay_curve_vb.clear()
        self._overlay_path = None

    # ── Custom math (formula) curves ────────────────────────────────────────

    def add_formula_curve(
        self,
        name: str,
        formula: str,
        color: str,
        x: np.ndarray,
        y: np.ndarray,
        y_axis_idx: int = 0,
    ) -> None:
        """Plot a custom-math result on this panel and remember it.

        The (name, formula, color, y_axis_idx) tuple is persisted via to_config
        so post-analysis can replay the same curve. Re-running with the same
        name replaces the prior curve in place and avoids a duplicate legend entry.
        """
        y_axis_idx = int(y_axis_idx) if y_axis_idx else 0

        # Remove any prior curve registered under this name (from any VB).
        old = self._formula_curves.pop(name, None)
        if old is not None:
            for vb in self._all_vbs:
                try:
                    vb.removeItem(old)
                except Exception:
                    pass
            try:
                self._plot_item.removeItem(old)
            except Exception:
                pass
            if self._legend is not None:
                try:
                    self._legend.removeItem(old)
                except Exception:
                    pass

        curve = pg.PlotDataItem(
            x[:len(y)], y,
            pen=pg.mkPen(color, width=1.5, style=Qt.DotLine),
            name=name,
        )

        # Route to the correct ViewBox.  axis 0 = primary PlotItem (legend auto-add);
        # axis 1+ = extra ViewBoxes that need a manual legend entry.
        if y_axis_idx == 0 or y_axis_idx >= len(self._all_vbs):
            self._plot_item.addItem(curve)   # legend registration is automatic
        else:
            self._all_vbs[y_axis_idx].addItem(curve)
            if self._legend is not None:
                try:
                    self._legend.addItem(curve, name)
                except Exception:
                    pass

        self._formula_curves[name] = curve

        # Update the persisted spec list (last write wins for a given name).
        self._formula_specs = [s for s in self._formula_specs if s.get("name") != name]
        self._formula_specs.append({
            "name": name, "formula": formula, "color": color, "y_axis_idx": y_axis_idx,
        })

    def replay_formulas(self) -> None:
        """Re-evaluate every saved formula against the panel's overlay data.

        Called after a post-analysis session loads and overlays are populated
        — restores the custom-math curves the user had during recording.
        """
        if not self._formula_specs or not self._overlay_data:
            return
        # Lazy import: engine.analysis pulls in numexpr which we'd rather not
        # require at PlotPanel import time.
        from engine.analysis import evaluate_formula, sanitize_name

        ch_data: Dict[str, np.ndarray] = {}
        x_axis: Optional[np.ndarray] = None
        for col, (xa, ya) in self._overlay_data.items():
            bare = col.split("[", 1)[0].strip() if "[" in col else col
            ch_data[sanitize_name(bare)] = ya
            if x_axis is None:
                x_axis = xa

        for spec in list(self._formula_specs):
            formula    = spec.get("formula", "")
            name       = spec.get("name") or formula
            color      = spec.get("color", "#E0A030")
            y_axis_idx = int(spec.get("y_axis_idx", 0) or 0)
            if not formula:
                continue
            try:
                result = evaluate_formula(formula, ch_data)
            except Exception:
                continue
            if x_axis is None:
                xa = np.arange(len(result), dtype=float)
            else:
                xa = x_axis[:len(result)]
            self.add_formula_curve(name, formula, color, xa, result, y_axis_idx=y_axis_idx)

    # ── 60 Hz refresh ────────────────────────────────────────────────────────

    def _samples_to_read(self, device_path: str) -> int:
        """Adaptive sample count — fetch only what the current view needs.

        When a rolling window is active we only need rolling_s × sample_rate
        samples (plus a small margin).  In "All Data" mode we request the full
        ring buffer so the historical timeline is never silently truncated by
        PLOT_WINDOW_SAMPLES.  read_latest() clamps to the actual stored count.
        """
        if self._rolling_s > 0:
            est = self._est_rate.get(device_path, 100.0)
            needed = int(self._rolling_s * est * 1.1) + 50
            return min(needed, PLOT_WINDOW_SAMPLES)
        # "All Data" — cap to a safe upper bound. pyqtgraph's auto-downsample
        # can otherwise compute an oversized reshape on huge arrays and raise
        # "Maximum allowed dimension exceeded".
        return 100_000

    def refresh(self, session_manager) -> None:
        """Update all live curves.  Called by the workspace's 60 Hz timer."""
        live_ns: Dict[str, np.ndarray] = {}
        live_x: Optional[np.ndarray] = None

        for key, cfg in self._channels.items():
            curve = self._live_curves.get(key)
            if curve is None:
                continue

            dev_session = session_manager.get_device(cfg.device_path)
            if dev_session is None or dev_session.buffer is None:
                continue

            data = dev_session.buffer.read_latest(
                self._samples_to_read(cfg.device_path)
            )
            if data.shape[1] == 0:
                continue

            # Update estimated sample rate (used by _samples_to_read).
            if data.shape[1] >= 2:
                dt = float(data[0, -1] - data[0, 0])
                if dt > 0:
                    self._est_rate[cfg.device_path] = data.shape[1] / dt

            meta = dev_session.metadata
            if meta is None:
                continue

            # ── X data ──
            if (self._x_device == cfg.device_path
                    and self._x_ch_name is not None):
                x_row = self._buffer_row(self._x_ch_name, meta)
                x = data[x_row] if x_row is not None else data[0]
            else:
                x = data[0]  # PC_Time_s

            # ── Y data ──
            y_row = self._buffer_row(cfg.channel_name, meta)
            if y_row is None:
                continue
            y = data[y_row]

            # ── Rolling window (time axis only) ──
            if self._rolling_s > 0 and self._x_device is None and len(x):
                cutoff = x[-1] - self._rolling_s
                mask   = x >= cutoff
                x, y   = x[mask], y[mask]

            try:
                curve.setData(x, y)
            except (ValueError, OverflowError):
                # pyqtgraph's auto-downsample can occasionally raise on
                # pathological data (e.g. extreme view range vs sample count).
                # Skip this frame rather than crash the whole UI thread.
                continue

            # Collect channel data for live formula evaluation.
            try:
                from engine.analysis import sanitize_name as _san
                live_ns[_san(cfg.channel_name)] = y
                if live_x is None:
                    live_x = x
            except Exception:
                pass

            # ── Alarm evaluation (rising-edge) ────────────────────────────
            if len(y) and self._alarm_cfgs:
                latest_val = float(y[-1])
                latest_ts  = float(x[-1]) if len(x) else 0.0
                for aid, alarm in list(self._alarm_cfgs.items()):
                    if not alarm.enabled:
                        continue
                    if alarm.channel_name != cfg.channel_name:
                        continue
                    now_active = self._eval_alarm_op(
                        alarm.operator, latest_val, alarm.threshold
                    )
                    was_active = self._alarm_prev_state.get(aid, False)
                    if now_active and not was_active:
                        self._fire_alarm_marker(
                            aid, alarm, latest_ts, latest_val, cfg.y_axis_idx
                        )
                        self.alarm_triggered.emit(
                            alarm.name, alarm.channel_name,
                            latest_val, latest_ts,
                        )
                    self._alarm_prev_state[aid] = now_active

        # ── Live formula evaluation ──────────────────────────────────────────
        if self._formula_specs and live_ns and live_x is not None:
            self._update_live_formulas(live_ns, live_x)

    def _update_live_formulas(
        self,
        live_ns: Dict[str, np.ndarray],
        x: np.ndarray,
    ) -> None:
        """Evaluate saved formula specs against current live channel data."""
        try:
            from engine.analysis import evaluate_formula
        except ImportError:
            return

        min_len = min((len(v) for v in live_ns.values()), default=0)
        if min_len == 0:
            return
        ns = {k: v[:min_len] for k, v in live_ns.items()}
        x_common = x[:min_len]

        for spec in self._formula_specs:
            formula    = spec.get("formula", "")
            name       = spec.get("name") or formula
            color      = spec.get("color", "#E0A030")
            y_axis_idx = int(spec.get("y_axis_idx", 0) or 0)
            if not formula:
                continue
            try:
                result = evaluate_formula(formula, ns)
                xa = x_common[:len(result)]
                curve = self._formula_curves.get(name)
                if curve is not None:
                    curve.setData(xa, result)
                else:
                    # First live evaluation: create the curve on the correct VB.
                    new_curve = pg.PlotDataItem(
                        xa, result,
                        pen=pg.mkPen(color, width=1.5, style=Qt.DotLine),
                        name=name,
                    )
                    if y_axis_idx == 0 or y_axis_idx >= len(self._all_vbs):
                        self._plot_item.addItem(new_curve)
                    else:
                        self._all_vbs[y_axis_idx].addItem(new_curve)
                        if self._legend is not None:
                            try:
                                self._legend.addItem(new_curve, name)
                            except Exception:
                                pass
                    self._formula_curves[name] = new_curve
            except Exception:
                continue

    @staticmethod
    def _buffer_row(ch_name: str, metadata) -> Optional[int]:
        """Return the buffer row index for *ch_name* (1-based, 0 = PC_Time_s)."""
        for i, ch in enumerate(metadata.channels):
            if ch.name == ch_name:
                return i + 1
        return None

    # ── Serialization ────────────────────────────────────────────────────────

    def to_config(self) -> dict:
        return {
            "id": self._panel_id,
            "title": self._title,
            "x_device": self._x_device,
            "x_channel": self._x_ch_name,
            "rolling_window_s": self._rolling_s,
            "y_axes": [c.to_dict() for c in self._y_axis_cfgs],
            "channels": [c.to_dict() for c in self._channels.values()],
            "thresholds": [c.to_dict() for c in self._threshold_cfgs.values()],
            "overlay_path": self._overlay_path,
            #
            "alarms":        [c.to_dict() for c in self._alarm_cfgs.values()],
            "x_markers":     [c.to_dict() for c in self._x_marker_cfgs.values()],
            "analysis_tags": [c.to_dict() for c in self._tag_cfgs.values()],
            "formulas":      list(self._formula_specs),
        }

    def restore_config(self, d: dict) -> None:
        """Restore visual state from a config dict (does NOT add live channels)."""
        self._title = d.get("title", self._title)
        self._title_edit.setText(self._title)

        rolling_s = float(d.get("rolling_window_s", 0.0))
        self._rolling_s = rolling_s
        if rolling_s <= 0.0:
            self._unit_combo.setCurrentIndex(0)   # "All data"
            self._rolling_spin.setEnabled(False)
        else:
            # Restore as raw seconds; find the "s" entry in the combo.
            for i in range(self._unit_combo.count()):
                ud = self._unit_combo.itemData(i)
                if ud and ud[0] == "s":
                    self._unit_combo.blockSignals(True)
                    self._unit_combo.setCurrentIndex(i)
                    self._unit_combo.blockSignals(False)
                    break
            self._rolling_spin.blockSignals(True)
            self._rolling_spin.setDecimals(1)
            self._rolling_spin.setRange(0.1, 3600.0)
            self._rolling_spin.setSingleStep(1.0)
            self._rolling_spin.setValue(rolling_s)
            self._rolling_spin.blockSignals(False)
            self._rolling_spin.setEnabled(True)

        self._x_device  = d.get("x_device")
        self._x_ch_name = d.get("x_channel")

        for ax_d in d.get("y_axes", []):
            cfg = YAxisConfig.from_dict(ax_d)
            self.add_y_axis(cfg.label, cfg.y_min, cfg.y_max,
                            cfg.locked, cfg.color, side=cfg.side)

        for ch_d in d.get("channels", []):
            cfg = ChannelStyleConfig.from_dict(ch_d)
            self.add_channel(
                cfg.device_path, cfg.channel_name,
                cfg.y_axis_idx, cfg.color, cfg.line_style, cfg.line_width,
            )

        for thr_d in d.get("thresholds", []):
            self.add_threshold(ThresholdConfig.from_dict(thr_d))

        for al_d in d.get("alarms", []):
            self.add_alarm(AlarmConfig.from_dict(al_d))

        for xm_d in d.get("x_markers", []):
            self.add_x_marker(XMarkerConfig.from_dict(xm_d))

        for tg_d in d.get("analysis_tags", []):
            self.add_analysis_tag(AnalysisTagConfig.from_dict(tg_d))

        # Re-load overlay if the saved file is still on disk.
        ov_path = d.get("overlay_path")
        if ov_path and os.path.exists(ov_path):
            try:
                self.load_overlay(ov_path)
            except Exception:
                pass

        # Persist the formula specs now; the actual curves get drawn after
        # post-analysis loads its overlays (replay_formulas is called then).
        formulas = d.get("formulas", [])
        if isinstance(formulas, list):
            self._formula_specs = [dict(s) for s in formulas if isinstance(s, dict)]

    # ── Internal slot handlers ───────────────────────────────────────────────

    def _on_rolling_unit_changed(self, _index: int) -> None:
        """Adapt the spinbox range/step when the user picks a different time unit."""
        data = self._unit_combo.currentData()
        if data is None:
            return
        key, _ = data

        if key == "all":
            self._rolling_spin.setEnabled(False)
            self._rolling_s = 0.0
            return

        prev_s = self._rolling_s   # remember current seconds value for conversion

        self._rolling_spin.blockSignals(True)
        if key == "ms":
            self._rolling_spin.setDecimals(0)
            self._rolling_spin.setRange(100.0, 600_000.0)
            self._rolling_spin.setSingleStep(100.0)
            self._rolling_spin.setValue(prev_s * 1000.0 if prev_s > 0 else 1000.0)
        elif key == "s":
            self._rolling_spin.setDecimals(1)
            self._rolling_spin.setRange(0.1, 3600.0)
            self._rolling_spin.setSingleStep(1.0)
            self._rolling_spin.setValue(prev_s if prev_s > 0 else 10.0)
        elif key == "min":
            self._rolling_spin.setDecimals(1)
            self._rolling_spin.setRange(0.1, 60.0)
            self._rolling_spin.setSingleStep(0.5)
            self._rolling_spin.setValue(prev_s / 60.0 if prev_s > 0 else 1.0)
        elif key == "h":
            self._rolling_spin.setDecimals(2)
            self._rolling_spin.setRange(0.01, 24.0)
            self._rolling_spin.setSingleStep(0.25)
            self._rolling_spin.setValue(prev_s / 3600.0 if prev_s > 0 else 0.25)
        self._rolling_spin.blockSignals(False)
        self._rolling_spin.setEnabled(True)

        _, mult = data
        self._rolling_s = self._rolling_spin.value() * mult

    def _on_rolling_value_changed(self, value: float) -> None:
        """Recompute _rolling_s (always in seconds) when the spinbox changes."""
        data = self._unit_combo.currentData()
        if data is None or data[0] == "all":
            self._rolling_s = 0.0
            return
        _, mult = data
        self._rolling_s = value * mult

    def _on_title_changed(self, text: str) -> None:
        self._title = text

    def _on_x_axis_changed(self, _index: int) -> None:
        data = self._x_combo.currentData()
        if data:
            dev, ch = data
        else:
            dev, ch = None, None
        self.set_x_channel(dev, ch)

    # ── Config dialog ────────────────────────────────────────────────────────

    def _show_config_dialog(self) -> None:
        dlg = _PanelConfigDialog(self, parent=self)
        dlg.exec()

    # ── Overlay loader ────────────────────────────────────────────────────────

    def _load_overlay_clicked(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Load Overlay Data",
            "",
            "Data Files (*.csv *.tsv *.parquet *.pq);;All Files (*)",
        )
        if path:
            try:
                self.load_overlay(path)
            except Exception as exc:
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.warning(self, "Overlay Error", str(exc))

    # ── Accessors ────────────────────────────────────────────────────────────

    # ── Image capture ────────────────────────────────────────────────────────

    def set_mouse_mode(self, mode: int) -> None:
        """Switch pyqtgraph ViewBox mouse mode (pg.ViewBox.PanMode / RectMode)."""
        self._plot_item.vb.setMouseMode(mode)
        for vb in self._all_vbs[1:]:
            vb.setMouseMode(mode)

    def capture(self, path: str) -> str:
        """Render the plot to a PNG image file and return the saved path.

        Uses pyqtgraph's ImageExporter so the output is pixel-perfect at the
        widget's current resolution.  Works headlessly (renders to QImage).
        """
        import pyqtgraph.exporters as exp
        exporter = exp.ImageExporter(self._plot_item)
        # Ensure a clean white-on-dark render with full current size
        exporter.parameters()["width"]  = max(self._plot_widget.width(),  640)
        exporter.parameters()["height"] = max(self._plot_widget.height(), 400)
        exporter.export(path)
        return path

    def set_time_range(self, start: float, end: float) -> None:
        """Zoom the X-axis to [start, end] and disable the rolling window."""
        self._rolling_s = 0.0
        self._unit_combo.blockSignals(True)
        self._unit_combo.setCurrentIndex(0)   # "All data"
        self._unit_combo.blockSignals(False)
        self._rolling_spin.setEnabled(False)
        self._plot_item.setXRange(float(start), float(end), padding=0)

    def set_rolling_window_seconds(self, seconds: float) -> None:
        """Set the live rolling time window to *seconds* (0 = show all data)."""
        seconds = float(seconds)
        self._rolling_s = seconds
        if seconds <= 0.0:
            self._unit_combo.blockSignals(True)
            self._unit_combo.setCurrentIndex(0)   # "All data"
            self._unit_combo.blockSignals(False)
            self._rolling_spin.setEnabled(False)
        else:
            for i in range(self._unit_combo.count()):
                ud = self._unit_combo.itemData(i)
                if ud and ud[0] == "s":
                    self._unit_combo.blockSignals(True)
                    self._unit_combo.setCurrentIndex(i)
                    self._unit_combo.blockSignals(False)
                    break
            self._rolling_spin.blockSignals(True)
            self._rolling_spin.setDecimals(1)
            self._rolling_spin.setRange(0.1, max(3600.0, seconds))
            self._rolling_spin.setSingleStep(1.0)
            self._rolling_spin.setValue(seconds)
            self._rolling_spin.blockSignals(False)
            self._rolling_spin.setEnabled(True)

    def freeze_view(self) -> None:
        """Freeze the current X-axis view range and stop live scrolling."""
        try:
            vr = self._plot_item.vb.viewRange()
            x_min, x_max = vr[0]
        except Exception:
            return
        self._rolling_s = 0.0
        self._unit_combo.blockSignals(True)
        self._unit_combo.setCurrentIndex(0)   # "All data"
        self._unit_combo.blockSignals(False)
        self._rolling_spin.setEnabled(False)
        if x_min < x_max:
            self._plot_item.setXRange(x_min, x_max, padding=0)

    @property
    def panel_id(self) -> str:
        return self._panel_id

    @property
    def title(self) -> str:
        return self._title

    @property
    def n_y_axes(self) -> int:
        return len(self._y_axis_cfgs)


# ── Full panel-config dialog (axes / channels / thresholds tabs) ─────────────

class _PanelConfigDialog(QDialog):
    def __init__(self, panel: PlotPanel, parent=None):
        super().__init__(parent)
        self._panel = panel
        self.setWindowTitle(f"Configure: {panel.title}")
        self.setMinimumSize(500, 400)

        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        tabs.addTab(self._build_axes_tab(), "Y-Axes")
        tabs.addTab(self._build_channels_tab(), "Channels")
        tabs.addTab(self._build_thresholds_tab(), "Thresholds")
        layout.addWidget(tabs)

        btns = QDialogButtonBox(QDialogButtonBox.Close)
        btns.rejected.connect(self.accept)
        layout.addWidget(btns)

    # ── Axes tab ─────────────────────────────────────────────────────────────

    def _build_axes_tab(self) -> QWidget:
        w = QWidget()
        vbox = QVBoxLayout(w)

        note = QLabel(
            "Axis 0 is always the <b>primary left</b> axis — it always exists "
            "and can always be edited.  Add a right-side axis when you need an "
            "independent scale (e.g. RPM alongside Voltage)."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#888; font-size:11px; padding:2px;")
        vbox.addWidget(note)

        self._axes_list = QListWidget()
        self._axes_list.setMinimumHeight(120)
        self._refresh_axes_list()
        vbox.addWidget(self._axes_list)

        row = QHBoxLayout()
        btn_add  = QPushButton("+ Add Right Axis")
        btn_add.setToolTip("Add a new right-side Y-axis with its own independent scale")
        btn_add.clicked.connect(self._add_axis)
        btn_edit = QPushButton("Edit Selected")
        btn_edit.setToolTip("Edit the label, colour, and range of the selected axis")
        btn_edit.clicked.connect(self._edit_axis)
        row.addWidget(btn_add)
        row.addWidget(btn_edit)
        vbox.addLayout(row)
        return w

    def _refresh_axes_list(self) -> None:
        self._axes_list.clear()
        # Always show at least axis 0, even if no config has been saved yet.
        n_show = max(len(self._panel._y_axis_cfgs), 1)
        for i in range(n_show):
            if i < len(self._panel._y_axis_cfgs):
                cfg = self._panel._y_axis_cfgs[i]
            else:
                cfg = YAxisConfig()  # axis 0 default (never been explicitly configured)

            side = "Left — primary" if i == 0 else f"Right-{i}"
            if cfg.locked and cfg.y_min is not None and cfg.y_max is not None:
                scale = f"🔒 fixed  {cfg.y_min:.3g} → {cfg.y_max:.3g}"
            else:
                scale = "🔓 auto-scale"

            label_str = cfg.label if cfg.label else "(no label set)"

            # Show which channels live on this axis.
            ch_names = [
                c.channel_name
                for c in self._panel._channels.values()
                if c.y_axis_idx == i
            ]
            ch_str = f"   ←  {', '.join(ch_names)}" if ch_names else ""

            item = QListWidgetItem(
                f"Axis {i}  [{side}]   {label_str}   {scale}{ch_str}"
            )
            # Tint the list row to match the axis/channel colour so the
            # axis-to-channel correspondence is immediately visible.
            from PySide6.QtGui import QColor
            item.setForeground(QColor(cfg.color))
            self._axes_list.addItem(item)

    def _add_axis(self) -> None:
        dlg = YAxisDialog(YAxisConfig(), parent=self)
        if dlg.exec() == QDialog.Accepted:
            cfg = dlg.result_config()
            self._panel.add_y_axis(cfg.label, cfg.y_min, cfg.y_max, cfg.locked, cfg.color)
            self._refresh_axes_list()

    def _edit_axis(self) -> None:
        row = self._axes_list.currentRow()
        if row < 0:
            return
        # Lazily create the primary axis config if it doesn't exist yet.
        while len(self._panel._y_axis_cfgs) <= row:
            self._panel.add_y_axis()
        existing = self._panel._y_axis_cfgs[row]
        dlg = YAxisDialog(existing, parent=self)
        if dlg.exec() == QDialog.Accepted:
            new_cfg = dlg.result_config()
            self._panel.reconfigure_y_axis(row, new_cfg)
            self._refresh_axes_list()

    # ── Channels tab ─────────────────────────────────────────────────────────

    def _build_channels_tab(self) -> QWidget:
        w = QWidget()
        vbox = QVBoxLayout(w)

        self._ch_list = QListWidget()
        self._refresh_ch_list()
        vbox.addWidget(self._ch_list)

        row = QHBoxLayout()
        btn_edit = QPushButton("Edit Style")
        btn_edit.clicked.connect(self._edit_channel)
        btn_rm = QPushButton("Remove")
        btn_rm.clicked.connect(self._remove_channel)
        row.addWidget(btn_edit)
        row.addWidget(btn_rm)
        vbox.addLayout(row)
        return w

    def _refresh_ch_list(self) -> None:
        self._ch_list.clear()
        for key, cfg in self._panel._channels.items():
            ax_idx   = cfg.y_axis_idx
            ax_cfgs  = self._panel._y_axis_cfgs
            side     = "L" if ax_idx == 0 else f"R{ax_idx}"
            ax_label = ax_cfgs[ax_idx].label if ax_idx < len(ax_cfgs) and ax_cfgs[ax_idx].label else ""
            ax_str   = f"Axis {ax_idx} [{side}]" + (f" '{ax_label}'" if ax_label else "")
            item = QListWidgetItem(
                f"{cfg.channel_name}  ·  {ax_str}  ·  "
                f"{cfg.line_style}  w={cfg.line_width:.1f}"
            )
            item.setData(Qt.UserRole, key)
            from PySide6.QtGui import QColor
            item.setForeground(QColor(cfg.color))
            self._ch_list.addItem(item)

    def _edit_channel(self) -> None:
        item = self._ch_list.currentItem()
        if item is None:
            return
        key = item.data(Qt.UserRole)
        cfg = self._panel._channels.get(key)
        if cfg is None:
            return
        import copy
        dlg = ChannelStyleDialog(
            copy.copy(cfg),
            self._panel.n_y_axes,
            self._panel._y_axis_cfgs,
            parent=self,
        )
        if dlg.exec() == QDialog.Accepted:
            new_cfg = dlg.result_config()
            self._panel.reconfigure_channel(key, new_cfg)
            self._refresh_ch_list()
            self._refresh_axes_list()   # update axis list in case assignment changed

    def _remove_channel(self) -> None:
        item = self._ch_list.currentItem()
        if item is None:
            return
        key = item.data(Qt.UserRole)
        self._panel.remove_channel(key)
        self._refresh_ch_list()

    # ── Thresholds tab ────────────────────────────────────────────────────────

    def _build_thresholds_tab(self) -> QWidget:
        w = QWidget()
        vbox = QVBoxLayout(w)

        self._thr_list = QListWidget()
        self._refresh_thr_list()
        vbox.addWidget(self._thr_list)

        row = QHBoxLayout()
        btn_add = QPushButton("+ Add Threshold")
        btn_add.clicked.connect(self._add_threshold)
        btn_rm  = QPushButton("Remove")
        btn_rm.clicked.connect(self._remove_threshold)
        row.addWidget(btn_add)
        row.addWidget(btn_rm)
        vbox.addLayout(row)
        return w

    def _refresh_thr_list(self) -> None:
        self._thr_list.clear()
        for tid, cfg in self._panel._threshold_cfgs.items():
            self._thr_list.addItem(
                f"{cfg.value:.4g}  {cfg.label or ''}  [{cfg.color}]"
            )
            item = self._thr_list.item(self._thr_list.count() - 1)
            item.setData(Qt.UserRole, tid)

    def _add_threshold(self) -> None:
        dlg = ThresholdDialog(parent=self)
        if dlg.exec() == QDialog.Accepted:
            self._panel.add_threshold(dlg.result_config())
            self._refresh_thr_list()

    def _remove_threshold(self) -> None:
        item = self._thr_list.currentItem()
        if item is None:
            return
        tid = item.data(Qt.UserRole)
        self._panel.remove_threshold(tid)
        self._refresh_thr_list()
