"""Step 13 self-test — DigitalDashboard runs cleanly, ticks at ~10 Hz,
and flips its QSS state when an alarm condition trips."""

from __future__ import annotations

import math
import os
import sys
import time

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QMainWindow

from ui.digital_dashboard import DigitalDashboard
from ui.style import ENTERPRISE_QSS


# ── Minimal stubs that mirror the real Session / PlotPanel surface area ──────


class _Ch:
    def __init__(self, name, unit=""):
        self.name = name
        self.unit = unit


class _Meta:
    def __init__(self, names_units):
        self.channels = [_Ch(n, u) for n, u in names_units]


class _Buf:
    def __init__(self):
        self.rows = [(0.0, 0.0, 0.0)]

    def push(self, t, temp, volt):
        self.rows.append((t, temp, volt))
        if len(self.rows) > 8192:
            self.rows = self.rows[-8192:]

    def read_latest(self, n):
        tail = self.rows[-n:]
        arr = np.array(tail, dtype=float).T
        return arr  # shape: (3, n) — row 0=time, 1=Temp, 2=Voltage


class _DevSession:
    def __init__(self):
        self.metadata = _Meta([("Temp", "°C"), ("Voltage", "V")])
        self.buffer = _Buf()


class _Session:
    def __init__(self):
        self.dev = _DevSession()

    def get_device(self, _path):
        return self.dev


class _Alarm:
    def __init__(self, name, ch, op, thr):
        self.name = name
        self.channel_name = ch
        self.operator = op
        self.threshold = thr
        self.enabled = True


class _ChCfg:
    def __init__(self, dev, ch):
        self.device_path = dev
        self.channel_name = ch


class _Panel:
    def __init__(self):
        self._alarm_cfgs = {"overheat": _Alarm("Overheat", "Temp", ">", 80.0)}
        self._channels = {
            "Temp": _ChCfg("/dev/dummy", "Temp"),
            "Voltage": _ChCfg("/dev/dummy", "Voltage"),
        }


class _Workspace:
    def __init__(self):
        self._panels = [_Panel()]

    def get_panels(self):
        return self._panels


# ── The test ─────────────────────────────────────────────────────────────────


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyleSheet(ENTERPRISE_QSS)

    session = _Session()
    workspace = _Workspace()

    win = QMainWindow()
    win.resize(900, 480)
    dock = DigitalDashboard(session=session, parent=win)
    dock.attach_workspace(workspace)
    dock.add_channels(
        "/dev/dummy",
        ["Temp", "Voltage"],
        device_label="ESP32-Test",
        units={"Temp": "°C", "Voltage": "V"},
    )
    win.addDockWidget(Qt.RightDockWidgetArea, dock)
    win.show()

    state = {"t": 0.0, "off": 0.0}

    def pump():
        state["t"] += 0.004
        temp = 60.0 + state["off"] + 4.0 * math.sin(state["t"] * 2.0)
        volt = 12.0 + 0.05 * math.sin(state["t"] * 7.0)
        session.dev.buffer.push(state["t"], temp, volt)

    pump_timer = QTimer()
    pump_timer.setInterval(4)
    pump_timer.timeout.connect(pump)
    pump_timer.start()

    results: dict = {"baseline_red": None, "tripped_red": None, "cleared_red": None}

    def temp_box():
        return dock._boxes[("/dev/dummy", "Temp")]

    def check_baseline():
        results["baseline_red"] = temp_box().property("alarm") == "true"

    def trip():
        state["off"] = 30.0   # → ≥ 90 °C

    def check_tripped():
        results["tripped_red"] = temp_box().property("alarm") == "true"

    def clear_alarm():
        state["off"] = 0.0

    def check_cleared():
        results["cleared_red"] = temp_box().property("alarm") == "true"

    def finish():
        # Print diagnostic state.
        temp_val = temp_box()._value_label.text()
        volt_val = dock._boxes[("/dev/dummy", "Voltage")]._value_label.text()
        print(f"[self-test] Final Temp display    = {temp_val}")
        print(f"[self-test] Final Voltage display = {volt_val}")
        print(f"[self-test] baseline alarm state  = {results['baseline_red']}")
        print(f"[self-test] tripped  alarm state  = {results['tripped_red']}")
        print(f"[self-test] cleared  alarm state  = {results['cleared_red']}")

        ok = (
            results["baseline_red"] is False
            and results["tripped_red"] is True
            and results["cleared_red"] is False
        )
        print("[self-test] RESULT:", "PASS ✓" if ok else "FAIL ✗")
        app.exit(0 if ok else 1)

    # Schedule: baseline check at 400 ms, trip at 600 ms, verify at 1200 ms,
    # clear at 1400 ms, verify at 2400 ms (smoothing needs ~5 ticks to settle).
    QTimer.singleShot(400,  check_baseline)
    QTimer.singleShot(600,  trip)
    QTimer.singleShot(1200, check_tripped)
    QTimer.singleShot(1400, clear_alarm)
    QTimer.singleShot(2400, check_cleared)
    QTimer.singleShot(2600, finish)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
