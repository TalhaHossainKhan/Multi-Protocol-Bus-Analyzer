"""Cross-platform serial port discovery service.

Runs in its own QThread, polls `serial.tools.list_ports.comports()` on a
fixed cadence, applies a debounce window to suppress connect/disconnect
bounce, and probes each newly-stable port with an open/close to detect
the case where another application (e.g. Arduino IDE) holds the port.

Successful detections are emitted to the UI via `new_port_detected`;
locked ports are emitted via `port_locked` so the UI can warn the user
without offering to connect.

Threading contract:
    * All OS calls (list_ports, Serial.open) happen on this thread.
    * UI never touches internal state directly — it only receives signals.
    * Re-prompt suppression is automatic: a port stays in `_known` until
      it physically disappears from a scan, so a true unplug+replug is
      required to surface the same device again.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from typing import Dict, Optional, Set

import serial
from serial.tools import list_ports
from PySide6.QtCore import QThread, Signal


@dataclass(frozen=True)
class PortInfo:
    device: str          # "/dev/cu.usbmodem14101" or "COM3"
    description: str
    hwid: str


def _is_relevant_port(device: str) -> bool:
    """Return True if `device` matches the per-OS serial naming conventions."""
    if sys.platform.startswith("win"):
        return device.upper().startswith("COM")
    if sys.platform == "darwin":
        # macOS callout devices for USB serial / USB modem adapters.
        return device.startswith("/dev/cu.usb") or device.startswith("/dev/cu.modem")
    # Linux: covers FTDI / CDC-ACM during development on Linux hosts.
    return device.startswith("/dev/ttyUSB") or device.startswith("/dev/ttyACM")


class PortScanner(QThread):
    SCAN_INTERVAL_S = 0.25       # poll list_ports four times per second
    DEBOUNCE_S = 0.5             # require a port to persist this long
    _SLEEP_SLICE_MS = 50         # keep stop() responsive

    new_port_detected = Signal(object)   # PortInfo
    port_locked = Signal(str, str)       # device, error message
    port_removed = Signal(str)           # device (physical unplug)
    scan_tick = Signal(int)              # number of currently-visible ports

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._running = False
        self._known: Set[str] = set()        # already announced (or locked)
        self._pending: Dict[str, float] = {}  # device -> first_seen monotonic ts

    def stop(self) -> None:
        self._running = False

    def forget_port(self, device: str) -> None:
        """Remove a device from the known set so it can be re-detected.

        Safe to call from the UI thread — set.discard is atomic under CPython's GIL.
        The scanner's next tick will re-announce the port if it is still physically
        connected, triggering a fresh ``new_port_detected`` signal.
        """
        self._known.discard(device)
        self._pending.pop(device, None)

    def run(self) -> None:
        self._running = True
        while self._running:
            try:
                self._scan_once()
            except Exception:
                # A single enumeration hiccup must never kill the service.
                pass
            self._sleep_responsive(self.SCAN_INTERVAL_S)

    # ---------- internals ----------

    def _sleep_responsive(self, seconds: float) -> None:
        ms_total = int(seconds * 1000)
        elapsed = 0
        while self._running and elapsed < ms_total:
            self.msleep(self._SLEEP_SLICE_MS)
            elapsed += self._SLEEP_SLICE_MS

    def _scan_once(self) -> None:
        now = time.monotonic()
        infos: Dict[str, PortInfo] = {
            p.device: PortInfo(p.device, p.description or "", p.hwid or "")
            for p in list_ports.comports()
            if _is_relevant_port(p.device)
        }
        current = set(infos.keys())
        self.scan_tick.emit(len(current))

        # Unplug detection: anything we were tracking but is no longer present
        # is forgotten, so a re-plug counts as a fresh detection.
        for gone in list(self._known - current):
            self._known.discard(gone)
            self.port_removed.emit(gone)
        for gone in list(set(self._pending.keys()) - current):
            self._pending.pop(gone, None)

        for device in current:
            if device in self._known:
                continue
            first_seen = self._pending.get(device)
            if first_seen is None:
                self._pending[device] = now
                continue
            if now - first_seen < self.DEBOUNCE_S:
                continue

            # Debounce satisfied — probe and announce exactly once.
            self._pending.pop(device, None)
            self._known.add(device)  # suppress re-emission until unplug

            error = self._probe_port(device)
            if error is None:
                self.new_port_detected.emit(infos[device])
            else:
                self.port_locked.emit(device, error)

    @staticmethod
    def _probe_port(device: str) -> Optional[str]:
        """Open then immediately close the port.

        Returns None on success, or a human-readable error message if the
        port is held by another process (PermissionError / Access Denied /
        Resource busy). Any open failure is treated as "locked" — this is
        intentionally conservative: better to warn than silently prompt
        for a port we cannot actually use.
        """
        try:
            ser = serial.Serial(device, timeout=0.1)
        except (PermissionError, serial.SerialException, OSError) as exc:
            return str(exc) or exc.__class__.__name__
        try:
            ser.close()
        except Exception:
            # Closing failed but the open succeeded — port is usable.
            pass
        return None
