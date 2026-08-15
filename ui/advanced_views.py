"""Advanced visualization windows.

Four specialized windows launched from the Analysis tab:

  FFTWindow          — 2-D FFT line plot + waterfall spectrogram (ImageItem)
  ThreeDWindow       — OpenGL 3-D scatter / line plot (3 channel axes)
  OscilloscopeWindow — Software-triggered capture window (rising/falling edge)

Each window is a standalone QWidget (not parented to MainWindow) so it can
float freely on any monitor.  All windows accept raw numpy arrays so the
caller owns the data-fetch logic.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from engine.analysis import compute_fft, compute_spectrogram, find_trigger


# ═══════════════════════════════════════════════════════════════════════════════
# FFT + Spectrogram window
# ═══════════════════════════════════════════════════════════════════════════════

class FFTWindow(QWidget):
    """Two pyqtgraph plots stacked: FFT line (top) + spectrogram waterfall (bottom)."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("FFT & Spectrogram")
        self.setMinimumSize(700, 560)
        self.setAttribute(Qt.WA_DeleteOnClose, False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # ── Controls ──────────────────────────────────────────────────────────
        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("nperseg:"))
        self._nperseg_spin = QSpinBox()
        self._nperseg_spin.setRange(16, 4096)
        self._nperseg_spin.setValue(256)
        self._nperseg_spin.setSingleStep(64)
        ctrl.addWidget(self._nperseg_spin)
        ctrl.addStretch()
        self._info_label = QLabel("")
        self._info_label.setStyleSheet("color:#8a8a8a; font-size:11px;")
        ctrl.addWidget(self._info_label)
        layout.addLayout(ctrl)

        # ── FFT line plot ─────────────────────────────────────────────────────
        self._fft_plot = pg.PlotWidget(title="FFT — Magnitude Spectrum")
        self._fft_plot.setLabel("bottom", "Frequency (Hz)")
        self._fft_plot.setLabel("left",   "Magnitude")
        self._fft_plot.setBackground("#121212")
        self._fft_plot.showGrid(x=True, y=True, alpha=0.15)
        self._fft_curve = self._fft_plot.plot(
            pen=pg.mkPen("#06b6d4", width=1.5)
        )
        layout.addWidget(self._fft_plot, stretch=2)

        # ── Spectrogram (waterfall) ───────────────────────────────────────────
        self._spec_widget = pg.GraphicsLayoutWidget(title="Spectrogram")
        self._spec_widget.setBackground("#121212")
        self._spec_view = self._spec_widget.addPlot()
        self._spec_view.setLabel("bottom", "Time (s)")
        self._spec_view.setLabel("left",   "Frequency (Hz)")

        self._image_item = pg.ImageItem()
        self._spec_view.addItem(self._image_item)

        # Colormap: inferno (bright = high power)
        try:
            cmap = pg.colormap.get("inferno")
        except Exception:
            cmap = pg.colormap.get("CET-L9")    # fallback built-in
        self._image_item.setColorMap(cmap)

        # Colorbar
        try:
            self._colorbar = pg.ColorBarItem(colorMap=cmap, label="Power (dB)")
            self._colorbar.setImageItem(self._image_item, insert_in=self._spec_view)
        except Exception:
            pass   # colorbar is cosmetic only — skip if API differs

        layout.addWidget(self._spec_widget, stretch=3)

    # ── Public API ────────────────────────────────────────────────────────────

    def update_data(
        self, x: np.ndarray, y: np.ndarray, channel_name: str = ""
    ) -> None:
        """Recompute FFT and spectrogram from (x, y) and refresh both plots."""
        if len(y) < 4:
            return

        from engine.analysis import estimate_sample_rate
        fs = estimate_sample_rate(x)
        nperseg = self._nperseg_spin.value()

        # FFT
        freqs, mags = compute_fft(y, sample_rate=fs)
        self._fft_curve.setData(freqs, mags)
        self._fft_plot.setTitle(
            f"FFT — {channel_name}   (Fs ≈ {fs:.1f} Hz,  "
            f"freq resolution ≈ {fs / len(y):.3f} Hz)"
        )

        # Spectrogram
        f, t, Sxx_dB = compute_spectrogram(y, sample_rate=fs, nperseg=nperseg)
        # ImageItem expects shape (n_x_pixels, n_y_pixels) = (time_frames, freq_bins)
        img = Sxx_dB.T  # (n_time_frames, n_freq_bins)
        self._image_item.setImage(img, autoLevels=True)
        # Map pixel coords → real-world axes
        if len(t) > 1 and len(f) > 1:
            dt = t[1] - t[0]
            df = f[1] - f[0]
            self._image_item.setRect(
                pg.QtCore.QRectF(float(t[0]), float(f[0]),
                                 float(t[-1] - t[0] + dt),
                                 float(f[-1] - f[0] + df))
            )

        n_samples = len(y)
        duration = float(x[-1] - x[0]) if len(x) > 1 else 0.0
        self._info_label.setText(
            f"{n_samples:,} samples  ·  {duration:.2f} s  ·  "
            f"peak {freqs[np.argmax(mags)]:.2f} Hz"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 3-D OpenGL window
# ═══════════════════════════════════════════════════════════════════════════════

class ThreeDWindow(QWidget):
    """3-D scatter / line plot mapping three channels to X, Y, Z axes.

    Falls back to a QLabel if pyqtgraph.opengl is unavailable.
    """

    _HAS_GL: bool = False

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("3-D Plot")
        self.setMinimumSize(640, 520)
        self.setAttribute(Qt.WA_DeleteOnClose, False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        try:
            import pyqtgraph.opengl as gl
            ThreeDWindow._HAS_GL = True
            self._gl = gl

            self._view = gl.GLViewWidget()
            self._view.setBackgroundColor("#121212")
            self._view.opts["distance"] = 40
            layout.addWidget(self._view, stretch=1)

            self._scatter: Optional[gl.GLScatterPlotItem] = None
            self._line:    Optional[gl.GLLinePlotItem]   = None

            # Axis grid
            gx = gl.GLGridItem()
            gx.rotate(90, 0, 1, 0)
            gx.translate(-10, 0, 0)
            self._view.addItem(gx)
            gy = gl.GLGridItem()
            gy.rotate(90, 1, 0, 0)
            gy.translate(0, -10, 0)
            self._view.addItem(gy)
            gz = gl.GLGridItem()
            gz.translate(0, 0, -10)
            self._view.addItem(gz)

        except Exception:
            ThreeDWindow._HAS_GL = False
            lbl = QLabel(
                "OpenGL not available on this system.\n"
                "Install PyOpenGL:  pip install PyOpenGL"
            )
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet("color:#8a8a8a;")
            layout.addWidget(lbl)

        # Controls
        ctrl = QHBoxLayout()
        self._mode_combo = QComboBox()
        self._mode_combo.addItem("Scatter", "scatter")
        self._mode_combo.addItem("Line",    "line")
        self._mode_combo.currentIndexChanged.connect(self._refresh_mode)
        ctrl.addWidget(QLabel("Mode:"))
        ctrl.addWidget(self._mode_combo)
        self._axis_labels: Dict[str, QLabel] = {}
        for ax in ("X", "Y", "Z"):
            lbl = QLabel(f"{ax}: —")
            lbl.setStyleSheet("color:#8a8a8a; font-size:11px;")
            self._axis_labels[ax] = lbl
            ctrl.addWidget(lbl)
        ctrl.addStretch()
        layout.addLayout(ctrl)

        self._last_pos: Optional[np.ndarray] = None

    # ── Public API ────────────────────────────────────────────────────────────

    def update_data(
        self,
        x_arr: np.ndarray,
        y_arr: np.ndarray,
        z_arr: np.ndarray,
        x_name: str = "X",
        y_name: str = "Y",
        z_name: str = "Z",
    ) -> None:
        """Plot three equal-length arrays as a 3-D scatter / line."""
        n = min(len(x_arr), len(y_arr), len(z_arr))
        if n < 2:
            return

        def _norm(a: np.ndarray) -> np.ndarray:
            lo, hi = float(a.min()), float(a.max())
            span = hi - lo or 1.0
            return (a - lo) / span * 20.0 - 10.0   # map to [-10, 10]

        xn, yn, zn = _norm(x_arr[:n]), _norm(y_arr[:n]), _norm(z_arr[:n])
        pos = np.column_stack([xn, yn, zn]).astype(np.float32)
        self._last_pos = pos

        for ax, name in zip(("X", "Y", "Z"), (x_name, y_name, z_name)):
            self._axis_labels[ax].setText(f"{ax}: {name}")

        self._refresh_mode()

    def _refresh_mode(self) -> None:
        if not ThreeDWindow._HAS_GL or self._last_pos is None:
            return
        gl = self._gl
        mode = self._mode_combo.currentData()

        # Remove old items
        for item in (self._scatter, self._line):
            if item is not None:
                try:
                    self._view.removeItem(item)
                except Exception:
                    pass
        self._scatter = self._line = None

        pos = self._last_pos
        color = np.ones((len(pos), 4), dtype=np.float32)
        color[:, 0] = 0.024; color[:, 1] = 0.714; color[:, 2] = 0.831   # cyan

        if mode == "scatter":
            self._scatter = gl.GLScatterPlotItem(
                pos=pos, color=color, size=4, pxMode=True
            )
            self._view.addItem(self._scatter)
        else:
            self._line = gl.GLLinePlotItem(
                pos=pos, color=(0.024, 0.714, 0.831, 1.0),
                width=1.5, antialias=True
            )
            self._view.addItem(self._line)


# ═══════════════════════════════════════════════════════════════════════════════
# Oscilloscope window
# ═══════════════════════════════════════════════════════════════════════════════

class OscilloscopeWindow(QWidget):
    """Software-triggered capture display mimicking a hardware oscilloscope.

    Call ``attach_session(session, device_path, channel_name)`` to bind it to
    a live ring buffer, then ``start()`` / ``stop()`` to begin scanning.
    """

    trigger_fired = Signal(float)   # PC timestamp of the trigger

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Oscilloscope")
        self.setMinimumSize(680, 460)
        self.setAttribute(Qt.WA_DeleteOnClose, False)

        self._session      = None
        self._device_path: Optional[str] = None
        self._channel_name: Optional[str] = None
        self._running: bool = False
        self._last_y_val: float = 0.0   # previous sample for edge detection

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # ── Trigger controls ──────────────────────────────────────────────────
        ctrl_box = QGroupBox("Trigger")
        ctrl_form = QFormLayout(ctrl_box)
        ctrl_form.setSpacing(6)

        self._edge_combo = QComboBox()
        self._edge_combo.addItem("Rising ↑",  "rising")
        self._edge_combo.addItem("Falling ↓", "falling")
        ctrl_form.addRow("Edge:", self._edge_combo)

        self._thr_spin = QDoubleSpinBox()
        self._thr_spin.setRange(-1e9, 1e9)
        self._thr_spin.setDecimals(4)
        ctrl_form.addRow("Threshold:", self._thr_spin)

        self._hyst_spin = QDoubleSpinBox()
        self._hyst_spin.setRange(0.0, 1e6)
        self._hyst_spin.setDecimals(4)
        self._hyst_spin.setValue(0.0)
        ctrl_form.addRow("Hysteresis:", self._hyst_spin)

        self._capture_spin = QSpinBox()
        self._capture_spin.setRange(16, 50_000)
        self._capture_spin.setValue(500)
        ctrl_form.addRow("Capture (samples):", self._capture_spin)

        btn_row = QHBoxLayout()
        self.btn_start  = QPushButton("▶  Arm Trigger")
        self.btn_start.setObjectName("primary")
        self.btn_start.clicked.connect(self._toggle_armed)
        btn_row.addWidget(self.btn_start)

        self.btn_force  = QPushButton("⚡  Force Trigger")
        self.btn_force.setObjectName("secondary")
        self.btn_force.clicked.connect(self._force_trigger)
        btn_row.addWidget(self.btn_force)
        ctrl_form.addRow(btn_row)

        layout.addWidget(ctrl_box)

        # ── Display ───────────────────────────────────────────────────────────
        self._status = QLabel("Status: Idle")
        self._status.setStyleSheet("color:#8a8a8a; font-size:11px;")
        layout.addWidget(self._status)

        self._plot = pg.PlotWidget()
        self._plot.setBackground("#121212")
        self._plot.setLabel("bottom", "Sample offset")
        self._plot.setLabel("left",   "Value")
        self._plot.showGrid(x=True, y=True, alpha=0.15)

        self._trace  = self._plot.plot(pen=pg.mkPen("#06b6d4", width=1.5))
        self._trig_line = pg.InfiniteLine(
            pos=0, angle=0,
            pen=pg.mkPen("#ff6600", width=1, style=Qt.DashLine),
            label="Threshold",
            labelOpts={"color": "#ff6600"},
        )
        self._plot.addItem(self._trig_line)
        layout.addWidget(self._plot, stretch=1)

        # Polling timer (60 Hz)
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._poll)

    # ── Public API ────────────────────────────────────────────────────────────

    def attach_session(
        self,
        session,
        device_path: str,
        channel_name: str,
    ) -> None:
        self._session      = session
        self._device_path  = device_path
        self._channel_name = channel_name
        self.setWindowTitle(
            f"Oscilloscope  —  {channel_name}  @  {device_path}"
        )

    def to_config(self) -> dict:
        return {
            "edge":            self._edge_combo.currentData(),
            "threshold":       self._thr_spin.value(),
            "hysteresis":      self._hyst_spin.value(),
            "capture_samples": self._capture_spin.value(),
        }

    def from_config(self, d: dict) -> None:
        edge = d.get("edge", "rising")
        idx = 0 if edge == "rising" else 1
        self._edge_combo.setCurrentIndex(idx)
        self._thr_spin.setValue(float(d.get("threshold", 0.0)))
        self._hyst_spin.setValue(float(d.get("hysteresis", 0.0)))
        self._capture_spin.setValue(int(d.get("capture_samples", 500)))

    # ── Internal ──────────────────────────────────────────────────────────────

    def _toggle_armed(self) -> None:
        if self._running:
            self._stop()
        else:
            self._start()

    def _start(self) -> None:
        self._running = True
        self.btn_start.setText("⏹  Disarm")
        self._status.setText("Status: Armed — waiting for trigger…")
        self._trig_line.setValue(self._thr_spin.value())
        self._last_y_val = 0.0
        self._timer.start()

    def _stop(self) -> None:
        self._running = False
        self._timer.stop()
        self.btn_start.setText("▶  Arm Trigger")
        self._status.setText("Status: Idle")

    def _force_trigger(self) -> None:
        """Immediately capture without waiting for a trigger edge."""
        data = self._fetch_latest()
        if data is not None:
            self._display_capture(data)

    def _poll(self) -> None:
        if not self._running or self._session is None:
            return
        data = self._fetch_latest()
        if data is None:
            return
        x, y = data
        thr   = self._thr_spin.value()
        hyst  = self._hyst_spin.value()
        edge  = self._edge_combo.currentData()
        idx   = find_trigger(y, threshold=thr, edge=edge, hysteresis=hyst)
        if idx is not None:
            # Capture window centred slightly after trigger
            n  = self._capture_spin.value()
            lo = max(0, idx - n // 4)
            hi = min(len(y), lo + n)
            self._display_capture((x[lo:hi], y[lo:hi]))
            self._stop()
            ts = float(x[idx]) if len(x) > idx else 0.0
            self.trigger_fired.emit(ts)

    def _fetch_latest(self) -> Optional[Tuple[np.ndarray, np.ndarray]]:
        if not self._session or not self._device_path:
            return None
        dev = self._session.get_device(self._device_path)
        if dev is None or dev.buffer is None or dev.metadata is None:
            return None
        n_cap = self._capture_spin.value() * 4   # oversample for trigger search
        data  = dev.buffer.read_latest(n_cap)
        if data.shape[1] == 0:
            return None
        x = data[0]
        for i, ch in enumerate(dev.metadata.channels):
            if ch.name == self._channel_name:
                return x, data[i + 1]
        return None

    def _display_capture(self, xy: Tuple[np.ndarray, np.ndarray]) -> None:
        x, y = xy
        self._trace.setData(np.arange(len(y)), y)
        self._status.setText(
            f"Status: Triggered  ·  {len(y)} samples  ·  "
            f"peak {float(np.max(np.abs(y))):.4g}  ·  "
            f"rms {float(np.sqrt(np.mean(y**2))):.4g}"
        )

    def closeEvent(self, event) -> None:
        self._stop()
        super().closeEvent(event)
