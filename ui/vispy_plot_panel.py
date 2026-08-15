"""VispyPlotPanel — GPU-accelerated plot panel for high-frequency data.

Uses Vispy's OpenGL backend for smooth rendering at >10 kHz sustained sample
rates or with >64 simultaneous channels — workloads that make pyqtgraph slow.

current limitations:
  • Single Y-axis only (no multi-axis support yet)
  • No thresholds, alarms, or overlay curves
  • No X-axis channel assignment (always uses PC_Time_s)

Public API is intentionally shaped to match PlotPanel so WorkspaceWidget can
treat both panel types identically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

try:
    import vispy
    from vispy import scene as vscene
    from vispy.app import use_app
    use_app("pyside6")
    _VISPY_OK = True
except Exception:
    _VISPY_OK = False


_DEFAULT_COLORS = (
    "#4FC3F7", "#FFB74D", "#81C784", "#F06292",
    "#BA68C8", "#FFD54F", "#90CAF9", "#A1887F",
    "#80CBC4", "#EF9A9A", "#CE93D8", "#80DEEA",
)


def _hex_to_rgba(hex_color: str, alpha: float = 1.0) -> Tuple[float, float, float, float]:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r = int(h[0:2], 16) / 255.0
    g = int(h[2:4], 16) / 255.0
    b = int(h[4:6], 16) / 255.0
    return (r, g, b, alpha)


@dataclass
class _ChannelState:
    device_path: str
    channel_name: str
    color: str
    line_width: float = 1.5
    line_style: str = "solid"  # kept for PlotPanel API compat; Vispy always draws solid
    visible: bool = True
    y_axis_idx: int = 0       # always 0 for Vispy v1; kept for API compat
    _line: object = field(default=None, repr=False)   # vispy Line visual


class VispyPlotPanel(QWidget):
    """GPU-accelerated plot panel (single Y-axis, N channels).

    Drop-in replacement for PlotPanel in high-frequency use cases.
    """

    # Match PlotPanel signal signatures so WorkspaceWidget wires them the same way.
    alarm_triggered     = Signal(str, str, float, float)
    channel_axis_changed = Signal(str, int)

    def __init__(self, panel_id: str, title: str = "Plot", parent=None) -> None:
        super().__init__(parent)
        self._panel_id   = panel_id
        self._title      = title
        self._rolling_s: float = 0.0
        self._channels: Dict[str, _ChannelState] = {}
        self._est_rate:  Dict[str, float] = {}
        self._canvas = None
        self._view   = None

        # PlotPanel API compatibility attributes used by SettingsPanel.
        self._x_device: Optional[str] = None
        self._x_ch_name: Optional[str] = None
        self._y_axis_cfgs: list = []   # always empty — Vispy v1 has single implicit axis

        self._build_ui()

    # ── UI construction ──────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)

        # Minimal toolbar: just a title field.
        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)
        toolbar.addWidget(QLabel("Title:"))
        self._title_edit = QLineEdit(self._title)
        self._title_edit.setMaximumWidth(180)
        self._title_edit.textChanged.connect(lambda t: setattr(self, "_title", t))
        toolbar.addWidget(self._title_edit)
        toolbar.addStretch()
        gpu_badge = QLabel("GPU")
        gpu_badge.setObjectName("gpuBadge")
        gpu_badge.setToolTip(
            "This panel uses GPU-accelerated Vispy rendering.\n"
            "Best for >10 kHz sustained sample rates or >64 channels.\n"
            "Advanced features (multi-axis, alarms) are available on standard panels."
        )
        toolbar.addWidget(gpu_badge)
        outer.addLayout(toolbar)

        if not _VISPY_OK:
            msg = QLabel(
                "Vispy is not installed or failed to initialise.\n"
                "Run:  pip install vispy"
            )
            msg.setObjectName("subtle")
            msg.setAlignment(Qt.AlignCenter)
            outer.addWidget(msg, stretch=1)
            return

        # ── Vispy canvas ──
        self._canvas = vscene.SceneCanvas(
            keys="interactive",
            bgcolor="#121212",
            show=False,
        )
        self._canvas.native.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        grid = self._canvas.central_widget.add_grid(margin=10)

        # Y-axis on the left.
        yaxis = vscene.AxisWidget(
            orientation="left",
            axis_color="#555555",
            text_color="#cccccc",
            font_size=8,
        )
        yaxis.stretch = (0.08, 1)
        grid.add_widget(yaxis, row=0, col=0)

        # X-axis at the bottom.
        xaxis = vscene.AxisWidget(
            orientation="bottom",
            axis_color="#555555",
            text_color="#cccccc",
            font_size=8,
        )
        xaxis.stretch = (1, 0.08)
        grid.add_widget(xaxis, row=1, col=1)

        # Main view with panzoom camera.
        self._view = grid.add_view(row=0, col=1, camera="panzoom")
        self._view.camera.set_range(x=(0, 10), y=(-1, 1))
        yaxis.link_view(self._view)
        xaxis.link_view(self._view)

        outer.addWidget(self._canvas.native, stretch=1)

        # ── Legend overlay (Qt labels) ──
        self._legend_widget = QWidget()
        self._legend_widget.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._legend_layout = QVBoxLayout(self._legend_widget)
        self._legend_layout.setContentsMargins(12, 12, 12, 12)
        self._legend_layout.setSpacing(3)
        self._legend_layout.addStretch()
        # Position legend as an overlay — simpler than a floating widget.
        # In practice it sits below the plot.

    # ── Channel management ───────────────────────────────────────────────────

    def add_channel(
        self,
        device_path: str,
        channel_name: str,
        y_axis_idx: int = 0,
        color: Optional[str] = None,
        line_style: str = "solid",
        line_width: float = 1.5,
    ) -> str:
        if color is None:
            color = _DEFAULT_COLORS[len(self._channels) % len(_DEFAULT_COLORS)]

        state = _ChannelState(
            device_path=device_path,
            channel_name=channel_name,
            color=color,
            line_width=line_width,
        )
        key = f"{device_path}::{channel_name}"
        self._channels[key] = state

        if _VISPY_OK and self._view is not None:
            rgba = _hex_to_rgba(color)
            # Start with a 2-point placeholder; gets replaced on first refresh.
            placeholder = np.zeros((2, 2), dtype=np.float32)
            line = vscene.visuals.Line(
                pos=placeholder,
                color=rgba,
                width=line_width,
                method="gl",
                parent=self._view.scene,
            )
            state._line = line

        self.channel_axis_changed.emit(key, 0)
        return key

    def remove_channel(self, key: str) -> None:
        state = self._channels.pop(key, None)
        if state is not None and state._line is not None:
            try:
                state._line.parent = None
            except Exception:
                pass
        self.channel_axis_changed.emit(key, -1)

    def move_channel_to_axis(self, key: str, target) -> int:
        """Single-axis panel — no-op, always returns 0."""
        return 0

    def add_y_axis(self, **_kwargs) -> int:
        return 0

    def set_channel_visible(self, key: str, visible: bool) -> None:
        state = self._channels.get(key)
        if state is None:
            return
        state.visible = visible
        if state._line is not None:
            state._line.visible = visible

    # ── Rolling window ───────────────────────────────────────────────────────

    def set_rolling_window(self, seconds: float) -> None:
        self._rolling_s = seconds

    def set_x_channel(self, device_path: Optional[str], channel_name: Optional[str]) -> None:
        """Stub — Vispy panel always uses PC_Time_s on X."""
        self._x_device  = device_path
        self._x_ch_name = channel_name

    # ── 60 Hz refresh ────────────────────────────────────────────────────────

    def refresh(self, session_manager) -> None:
        if not _VISPY_OK or self._view is None:
            return

        x_min_global: Optional[float] = None
        x_max_global: Optional[float] = None
        y_min_global: Optional[float] = None
        y_max_global: Optional[float] = None

        for key, state in self._channels.items():
            if not state.visible or state._line is None:
                continue

            dev = session_manager.get_device(state.device_path)
            if dev is None or dev.buffer is None:
                continue

            n_samples = self._samples_to_read(state.device_path)
            data = dev.buffer.read_latest(n_samples)
            if data.shape[1] < 2:
                continue

            # Update sample rate estimate.
            dt = float(data[0, -1] - data[0, 0])
            if dt > 0:
                self._est_rate[state.device_path] = data.shape[1] / dt

            meta = dev.metadata
            if meta is None:
                continue

            x = data[0]  # PC_Time_s

            # Find channel row in buffer.
            ch_row: Optional[int] = None
            for r, ch_spec in enumerate(meta.channels, start=1):
                if ch_spec.name == state.channel_name:
                    ch_row = r
                    break
            if ch_row is None or ch_row >= data.shape[0]:
                continue

            y = data[ch_row]

            # Apply rolling window clip.
            if self._rolling_s > 0 and len(x) > 0:
                t_end = x[-1]
                t_start = t_end - self._rolling_s
                mask = x >= t_start
                x = x[mask]
                y = y[mask]

            if len(x) < 2:
                continue

            pos = np.column_stack([x, y]).astype(np.float32)
            state._line.set_data(pos=pos)

            # Track global ranges for auto-fit.
            x0, x1 = float(x[0]), float(x[-1])
            y0, y1 = float(np.nanmin(y)), float(np.nanmax(y))
            x_min_global = x0 if x_min_global is None else min(x_min_global, x0)
            x_max_global = x1 if x_max_global is None else max(x_max_global, x1)
            y_min_global = y0 if y_min_global is None else min(y_min_global, y0)
            y_max_global = y1 if y_max_global is None else max(y_max_global, y1)

        # Auto-fit the camera to the data range.
        if x_min_global is not None and x_max_global is not None:
            x_pad = max((x_max_global - x_min_global) * 0.02, 0.1)
            y_pad = max((y_max_global - y_min_global) * 0.05, 0.1) if y_min_global is not None else 0.1
            self._view.camera.set_range(
                x=(x_min_global - x_pad, x_max_global + x_pad),
                y=(y_min_global - y_pad, y_max_global + y_pad),
                margin=0,
            )

    def _samples_to_read(self, device_path: str) -> int:
        if self._rolling_s > 0:
            est = self._est_rate.get(device_path, 500.0)
            return int(self._rolling_s * est * 1.1) + 100
        return 2_000_000  # request all — Vispy handles millions of points efficiently

    # ── Serialization ─────────────────────────────────────────────────────────

    def to_config(self) -> dict:
        return {
            "id":              self._panel_id,
            "title":           self._title,
            "backend":         "vispy",
            "rolling_window_s": self._rolling_s,
            "y_axes":          [{"label": "", "min": None, "max": None, "locked": False, "color": "#cfcfcf"}],
            "channels": [
                {
                    "device":     s.device_path,
                    "channel":    s.channel_name,
                    "y_axis":     0,
                    "color":      s.color,
                    "line_style": "solid",
                    "line_width": s.line_width,
                }
                for s in self._channels.values()
            ],
            "thresholds": [],
            "overlay_path": None,
        }

    def restore_config(self, d: dict) -> None:
        self._rolling_s = float(d.get("rolling_window_s", 0.0))
        self._title_edit.setText(d.get("title", self._title))
        for ch in d.get("channels", []):
            self.add_channel(
                device_path=ch.get("device", ""),
                channel_name=ch.get("channel", ""),
                y_axis_idx=0,
                color=ch.get("color", _DEFAULT_COLORS[0]),
                line_width=float(ch.get("line_width", 1.5)),
            )

    # ── Properties ──────────────────────────────────────────────────────────

    @property
    def panel_id(self) -> str:
        return self._panel_id

    @property
    def title(self) -> str:
        return self._title

    @property
    def n_y_axes(self) -> int:
        return 1

    # ── Stub methods for PlotPanel API compatibility ─────────────────────────

    def reconfigure_channel(self, key: str, cfg) -> None:
        state = self._channels.get(key)
        if state is None:
            return
        state.color = cfg.color
        state.line_width = cfg.line_width
        if state._line is not None:
            state._line.set_data(color=_hex_to_rgba(cfg.color))

    def get_panels(self) -> list:
        return []

    def get_all_channels(self) -> list:
        return [(s.device_path, s.channel_name) for s in self._channels.values()]
