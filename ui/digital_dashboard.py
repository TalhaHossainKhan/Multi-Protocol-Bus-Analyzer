"""DigitalDashboard.

A floating/dockable side panel that shows selected channels as large,
high-contrast numeric readouts ("DROs") — independent of the main plot.

Design contract
---------------
* Refresh rate is decoupled from the ingestion rate.  A dedicated ``QTimer``
  runs in the UI thread at ~10 Hz and pulls the *latest* scalar value for
  every tracked channel directly from the ``SessionManager``'s per-device
  ``MultiChannelRingBuffer``.  Updating Qt text properties at 1 kHz freezes
  the main thread and is unreadable to humans — 10 Hz is the sweet spot.

* Alarms are evaluated on every tick.  We walk every ``PlotPanel``'s
  ``_alarm_cfgs`` and ask "is any enabled alarm currently True for this
  channel?".  If so, the box flips to the ``alarm="true"`` QSS state
  (harsh red border + background).  When the condition clears, it flips
  back automatically.  This makes the readout a continuous indicator —
  it doesn't rely on the rising-edge ``alarm_triggered`` signal at all.

* Light EMA smoothing (α=0.35) is applied to the displayed value to stop
  the last digit from flickering on noisy sensors.  Smoothing is purely
  cosmetic; the underlying buffer values are never touched.

Public API
----------
``DigitalDashboard(session, parent)`` — construct the QDockWidget.
``set_channels(device_path, channel_names)``      — replace tracked set
                                                     for a device.
``add_channels(device_path, channel_names, …)``    — additive variant.
``remove_device(device_path)``                     — strip a device.
``attach_workspace(workspace)``                    — wire alarms.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDockWidget,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


# 10 Hz refresh — exactly the cadence specified by the design.  Faster than
# 30 Hz becomes unreadable, slower than 5 Hz feels laggy on transient events.
_DRO_REFRESH_MS = 100

# EMA smoothing factor: 1.0 = no smoothing (raw reading), 0.0 = frozen.
# 0.35 lets a true step change settle in ~6 ticks (~600 ms) — fast enough to
# feel live, slow enough to kill last-digit flicker on noisy ADCs.
_DRO_SMOOTHING = 0.35


# ── A single DRO tile ─────────────────────────────────────────────────────────

class _DROBox(QFrame):
    """One readout tile: channel name (small) + big numeric value."""

    def __init__(self, channel_name: str, unit: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("droBox")
        # Custom property the QSS pivots on — "false" by default, flipped to
        # "true" by set_alarm() to turn the box red.
        self.setProperty("alarm", "false")
        self.setFrameShape(QFrame.NoFrame)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self.setMinimumWidth(160)
        self.setMaximumWidth(260)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(4)

        header = channel_name + (f"   ·   {unit}" if unit else "")
        self._name_label = QLabel(header)
        self._name_label.setObjectName("droName")
        self._name_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        layout.addWidget(self._name_label)

        self._value_label = QLabel("—")
        self._value_label.setObjectName("droValue")
        self._value_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._value_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self._value_label, stretch=1)

        # EMA state for smoothing.  None until the first sample lands.
        self._ema: Optional[float] = None
        self._unit: str = unit

    def set_value(self, raw: Optional[float]) -> None:
        """Update the displayed number (with light EMA smoothing)."""
        if raw is None:
            self._value_label.setText("—")
            return
        if self._ema is None:
            self._ema = float(raw)
        else:
            self._ema = (
                _DRO_SMOOTHING * float(raw) + (1.0 - _DRO_SMOOTHING) * self._ema
            )
        self._value_label.setText(self._format(self._ema))

    def set_alarm(self, active: bool) -> None:
        """Flip the box into / out of the red alarm state."""
        flag = "true" if active else "false"
        if self.property("alarm") == flag:
            return
        self.setProperty("alarm", flag)
        # Qt requires an explicit polish cycle for property-driven QSS to
        # repaint — otherwise the box keeps its old style until next event.
        self.style().unpolish(self)
        self.style().polish(self)

    @staticmethod
    def _format(v: float) -> str:
        # Adaptive precision: large magnitudes lose their fractional digits so
        # the box stays readable; tiny values get more significant figures.
        av = abs(v)
        if av >= 1000:
            return f"{v:,.1f}"
        if av >= 100:
            return f"{v:.2f}"
        if av >= 1:
            return f"{v:.3f}"
        if av == 0.0:
            return "0.000"
        return f"{v:.4f}"


# ── The dock widget ───────────────────────────────────────────────────────────

class DigitalDashboard(QDockWidget):
    """Dockable side panel of large numeric channel readouts.

    The dashboard reads the latest scalar value for every tracked channel
    on a 10 Hz timer, and flips boxes red whenever the matching alarm
    condition (from the existing ``PlotPanel`` engine) is active.

    Channels are keyed by ``(device_path, channel_name)`` so the same name
    coming from two different MCUs is treated as two independent readouts.
    """

    def __init__(self, session=None, parent=None) -> None:
        super().__init__("Live Values", parent)
        self.setObjectName("liveValuesDock")
        self.setAllowedAreas(
            Qt.BottomDockWidgetArea
            | Qt.TopDockWidgetArea
            | Qt.LeftDockWidgetArea
            | Qt.RightDockWidgetArea
        )
        self.setFeatures(
            QDockWidget.DockWidgetMovable
            | QDockWidget.DockWidgetFloatable
            | QDockWidget.DockWidgetClosable
        )

        self._session = session
        self._workspace = None
        # (device_path, channel_name) -> _DROBox
        self._boxes: Dict[Tuple[str, str], _DROBox] = {}

        # ── Scrollable container so a large channel count doesn't blow the
        # ── dock width.  Boxes stack vertically; user can resize the dock.
        host = QWidget()
        host.setObjectName("droHost")
        outer = QVBoxLayout(host)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        # Horizontal scrolling for the bottom-dock layout; vertical never needed.
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self._inner = QWidget()
        self._inner.setObjectName("droInner")
        # Horizontal layout so boxes line up side-by-side when the dock
        # is placed at the bottom of the window.
        self._grid = QHBoxLayout(self._inner)
        self._grid.setContentsMargins(10, 8, 10, 8)
        self._grid.setSpacing(8)

        self._empty_label = QLabel(
            "No digital readouts selected — tick 'Live Value' on a channel "
            "in the Signal Mapping dialog to surface it here."
        )
        self._empty_label.setObjectName("subtle")
        self._empty_label.setWordWrap(False)
        self._empty_label.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        self._grid.addWidget(self._empty_label)
        self._grid.addStretch(1)   # pushes boxes left; removed when boxes are added

        scroll.setWidget(self._inner)
        outer.addWidget(scroll)
        self.setWidget(host)
        self.setMinimumHeight(110)

        # ── 10 Hz refresh timer.  Coalesced — if the UI thread is busy the
        # ── next tick simply uses the latest buffer state.
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.CoarseTimer)
        self._timer.setInterval(_DRO_REFRESH_MS)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    # ── Wiring ────────────────────────────────────────────────────────────────

    def attach_session(self, session) -> None:
        self._session = session

    def attach_workspace(self, workspace) -> None:
        """Remember the workspace so we can walk plot panel alarms each tick."""
        self._workspace = workspace

    # ── Channel set management ────────────────────────────────────────────────

    def set_channels(
        self,
        device_path: str,
        channel_names: Iterable[str],
        device_label: str = "",
        units: Optional[Dict[str, str]] = None,
    ) -> None:
        """Replace the DRO set for *device_path* with *channel_names*."""
        self.remove_device(device_path)
        self.add_channels(device_path, channel_names, device_label, units)

    def add_channels(
        self,
        device_path: str,
        channel_names: Iterable[str],
        device_label: str = "",
        units: Optional[Dict[str, str]] = None,
    ) -> None:
        units = units or {}
        added = False
        for name in channel_names:
            key = (device_path, name)
            if key in self._boxes:
                continue
            unit = units.get(name, "")
            display = (f"{device_label} · {name}" if device_label else name)
            box = _DROBox(display, unit=unit)
            self._boxes[key] = box
            self._insert_box(box)
            added = True
        if added:
            self._update_empty_state()

    def remove_device(self, device_path: str) -> None:
        gone = [k for k in self._boxes if k[0] == device_path]
        for k in gone:
            box = self._boxes.pop(k)
            box.setParent(None)
            box.deleteLater()
        self._update_empty_state()

    def clear(self) -> None:
        for box in list(self._boxes.values()):
            box.setParent(None)
            box.deleteLater()
        self._boxes.clear()
        self._update_empty_state()

    def _insert_box(self, box: _DROBox) -> None:
        # Insert just before the trailing stretch so boxes pack left.
        n = self._grid.count()
        self._grid.insertWidget(max(0, n - 1), box)
        # Remove the trailing stretch once real boxes exist so they don't
        # get squashed — the stretch is only needed for the empty state.
        if self._boxes:
            # Find and remove the stretch item (it's always last if present).
            last = self._grid.itemAt(self._grid.count() - 1)
            if last and last.spacerItem():
                self._grid.takeAt(self._grid.count() - 1)

    def _update_empty_state(self) -> None:
        empty = not self._boxes
        self._empty_label.setVisible(empty)

    # ── Refresh loop ──────────────────────────────────────────────────────────

    def _tick(self) -> None:
        """Pull the latest scalar value for every tracked channel."""
        if not self._boxes:
            return
        session = self._session
        if session is None:
            return

        # Build {channel_name: alarm_active} from every PlotPanel so we can
        # evaluate alarm overlay state without needing the panel's rising-edge
        # logic.  Cheap — alarms are few and the lookup is per-channel.
        alarm_state = self._collect_alarm_state()

        for (dev_path, ch_name), box in self._boxes.items():
            value = self._latest_value(session, dev_path, ch_name)
            box.set_value(value)
            active = alarm_state.get((dev_path, ch_name), False)
            if value is not None and active is None:
                active = False
            box.set_alarm(bool(active))

    @staticmethod
    def _latest_value(session, device_path: str, channel_name: str) -> Optional[float]:
        """Grab `[-1]` from the device's ring buffer for one channel."""
        if session is None:
            return None
        dev = session.get_device(device_path)
        if dev is None or dev.buffer is None or dev.metadata is None:
            return None
        # Map channel name → buffer row.  Row 0 is the injected PC_Time_s.
        row = None
        for i, ch in enumerate(dev.metadata.channels):
            if ch.name == channel_name:
                row = i + 1
                break
        if row is None:
            return None
        data = dev.buffer.read_latest(1)
        if data.shape[1] == 0:
            return None
        return float(data[row, -1])

    def _collect_alarm_state(self) -> Dict[Tuple[str, str], bool]:
        """Return {(device_path, channel_name): is_alarming} across all panels.

        We re-evaluate the alarm operator here instead of trusting
        ``_alarm_prev_state`` because that flag only tracks rising-edge
        firing — it's True the moment an alarm trips, but is also True
        on every subsequent frame while the condition persists, which is
        what we want.  Re-evaluating directly against the latest scalar
        keeps the dashboard accurate even if the plot panel hasn't ticked
        yet (e.g. on the Settings tab where _refresh_plots() is skipped).
        """
        out: Dict[Tuple[str, str], bool] = {}
        workspace = self._workspace
        if workspace is None:
            return out

        # Operator table mirrors PlotPanel._eval_alarm_op.
        ops = {
            ">":  lambda v, t: v >  t,
            "<":  lambda v, t: v <  t,
            ">=": lambda v, t: v >= t,
            "<=": lambda v, t: v <= t,
            "==": lambda v, t: v == t,
        }
        try:
            panels = workspace.get_panels()
        except Exception:
            return out

        for panel in panels:
            cfgs = getattr(panel, "_alarm_cfgs", None) or {}
            channels = getattr(panel, "_channels", None) or {}
            if not cfgs:
                continue
            # Map channel_name -> device_path for THIS panel so we can resolve
            # which device a same-named alarm belongs to.
            name_to_dev: Dict[str, str] = {}
            for cfg in channels.values():
                name_to_dev[cfg.channel_name] = cfg.device_path

            for alarm in cfgs.values():
                if not getattr(alarm, "enabled", True):
                    continue
                op_fn = ops.get(alarm.operator)
                if op_fn is None:
                    continue
                dev_path = name_to_dev.get(alarm.channel_name)
                if dev_path is None:
                    continue
                value = self._latest_value(self._session, dev_path, alarm.channel_name)
                if value is None:
                    continue
                active = bool(op_fn(value, float(alarm.threshold)))
                key = (dev_path, alarm.channel_name)
                # OR across alarms — if any alarm on the channel is firing,
                # the readout goes red.
                out[key] = out.get(key, False) or active
        return out

    # ── External alarm push (optional convenience) ────────────────────────────

    def force_alarm(self, device_path: str, channel_name: str, active: bool) -> None:
        """Manually toggle the alarm state on one box.

        Exposed for tests and for any external code that wants to drive
        alarm visuals directly (bypassing the auto-eval pass).  Production
        UI just lets ``_tick()`` figure it out.
        """
        box = self._boxes.get((device_path, channel_name))
        if box is not None:
            box.set_alarm(active)


# ── Self-test harness ────────────────────────────────────────────────────────
#
# Running this file directly stands up a tiny GUI with two dummy channels,
# feeds synthetic data through the 10 Hz timer logic, and after 1.2 s forces
# "Temp" past an alarm threshold to confirm the red-state QSS flip.

if __name__ == "__main__":   # pragma: no cover
    import math
    import sys
    import time

    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication, QMainWindow

    from ui.style import ENTERPRISE_QSS

    # ── Stub Session / PlotPanel / metadata so the dashboard can exercise
    # ── exactly the same code paths it uses in production. ─────────────────
    class _Ch:
        def __init__(self, name, unit=""): self.name, self.unit = name, unit

    class _Meta:
        def __init__(self, names):
            self.channels = [_Ch(n, "°C" if n == "Temp" else "V") for n in names]

    class _Buf:
        def __init__(self):
            self.t0 = time.perf_counter()
            self.last = [(0.0, 0.0, 0.0)]
        @property
        def shape_marker(self): return None
        def push(self, t, temp, volt):
            self.last.append((t, temp, volt))
            if len(self.last) > 4096: self.last = self.last[-4096:]
        def read_latest(self, n):
            import numpy as np
            tail = self.last[-n:]
            arr = np.array(tail, dtype=float).T
            return arr  # rows: 0=time, 1=Temp, 2=Volt

    class _DevSession:
        def __init__(self):
            self.metadata = _Meta(["Temp", "Voltage"])
            self.buffer = _Buf()

    class _Session:
        def __init__(self): self.dev = _DevSession()
        def get_device(self, path): return self.dev

    # Stub alarm config that matches AlarmConfig's shape.
    class _Alarm:
        def __init__(self, name, channel, op, thr):
            self.name, self.channel_name = name, channel
            self.operator, self.threshold = op, thr
            self.enabled = True

    class _ChCfg:
        def __init__(self, dev, ch):
            self.device_path, self.channel_name = dev, ch

    class _Panel:
        def __init__(self):
            self._alarm_cfgs = {"a": _Alarm("Overheat", "Temp", ">", 80.0)}
            self._channels = {
                "k1": _ChCfg("/dev/dummy", "Temp"),
                "k2": _ChCfg("/dev/dummy", "Voltage"),
            }

    class _Workspace:
        def __init__(self): self._panels = [_Panel()]
        def get_panels(self): return self._panels

    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyleSheet(ENTERPRISE_QSS)

    session = _Session()
    workspace = _Workspace()

    win = QMainWindow()
    win.setWindowTitle("DigitalDashboard — Self-Test")
    win.resize(900, 520)

    dock = DigitalDashboard(session=session, parent=win)
    dock.attach_workspace(workspace)
    dock.add_channels(
        "/dev/dummy",
        ["Temp", "Voltage"],
        device_label="ESP32-Test",
        units={"Temp": "°C", "Voltage": "V"},
    )
    win.addDockWidget(Qt.RightDockWidgetArea, dock)

    central = QLabel("Plot would render here.  Watching DRO behaviour →")
    central.setAlignment(Qt.AlignCenter)
    central.setMinimumWidth(540)
    win.setCentralWidget(central)
    win.show()

    # Synthetic data pump — fast (250 Hz) — to prove the 10 Hz DRO loop
    # really does throttle.
    state = {"t": 0.0, "temp_offset": 0.0}
    def pump():
        state["t"] += 0.004
        temp = 65.0 + state["temp_offset"] + 5.0 * math.sin(state["t"] * 2.0)
        volt = 12.0 + 0.05 * math.sin(state["t"] * 7.0)
        session.dev.buffer.push(state["t"], temp, volt)

    pump_timer = QTimer()
    pump_timer.setInterval(4)
    pump_timer.timeout.connect(pump)
    pump_timer.start()

    # Force alarm condition after 1.2 s by shifting Temp above 80 °C.
    def trip():
        state["temp_offset"] = 25.0   # baseline 65 + 25 + sin → ≥ 85 °C
        print("[self-test] Temp pushed above 80 °C — DRO should turn RED.")
    QTimer.singleShot(1200, trip)

    # Reset after another 1.5 s to confirm auto-clear.
    def clear():
        state["temp_offset"] = 0.0
        print("[self-test] Temp restored — DRO should revert to NORMAL.")
    QTimer.singleShot(2700, clear)

    # Auto-quit after the full cycle so the harness terminates cleanly.
    QTimer.singleShot(4200, app.quit)

    sys.exit(app.exec())
