"""Connection screen.

After the user clicks "Real-Time Analysis" on the launch screen, this is the
next surface they see.  Three tabs along the top — USB/UART, BLE, TCP/Wi-Fi —
each containing the protocol-specific device picker.  Selecting a device and
clicking "Connect" emits :sig:`connect_requested` with the resolved
``transport`` key and ``params`` dict.

The MainWindow listens for ``connect_requested``, asks ``SessionManager`` to
spin up the matching ingestion worker, and routes the resulting
``device_ready`` signal into the Signal Mapping modal.
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from PySide6.QtCore import QThread

from engine.port_scanner import PortInfo, PortScanner


# ── BLE scan worker ──────────────────────────────────────────────────────────

class _BleScanWorker(QThread):
    """Runs `bleak` discovery inside its own event loop."""
    scan_complete = Signal(list, str)   # results, error_msg

    def __init__(self, timeout: float = 5.0, parent=None) -> None:
        super().__init__(parent)
        self._timeout = timeout

    def run(self) -> None:
        try:
            from engine.ble_stream import BleStreamer
        except ImportError as exc:
            self.scan_complete.emit([], f"BLE backend unavailable: {exc}")
            return
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            results = loop.run_until_complete(BleStreamer.scan(self._timeout))
            loop.close()
            self.scan_complete.emit(results, "")
        except Exception as exc:
            self.scan_complete.emit([], str(exc))


# ── CAN adapter detect worker ────────────────────────────────────────────────

class _CanDetectWorker(QThread):
    """Background worker: calls can.detect_available_configs() for one interface."""
    detect_complete = Signal(list, str)   # channel_names, error_msg

    def __init__(self, interface: str, parent=None) -> None:
        super().__init__(parent)
        self._interface = interface

    def run(self) -> None:
        try:
            import can
            configs = can.detect_available_configs(interfaces=[self._interface])
            channels = [str(c["channel"]) for c in configs if c.get("channel")]
            self.detect_complete.emit(channels, "")
        except Exception as exc:
            self.detect_complete.emit([], str(exc))


# ── CAN auto-detection (interface + channel, zero config) ─────────────────────

# python-can backends worth probing automatically.  These are the ones whose
# _detect_available_configs() actually enumerates connected hardware.
_AUTODETECT_IFACES = (
    "gs_usb", "pcan", "kvaser", "socketcan", "ixxat", "usb2can", "neovi", "vector",
)

# Serial (SLCAN-style) adapters don't show up via python-can's USB detection,
# so we sniff the serial ports ourselves.  Match either a known CAN USB
# VID:PID or a CAN-ish name in the port description.
_SERIAL_CAN_VIDPID = {
    (0x1D50, 0x606F),   # candleLight / CANable (gs_usb and slcan builds)
    (0x16D0, 0x117E),   # CANtact
    (0xAD50, 0x60C4),   # some CANable clones
    (0x0483, 0x5740),   # STM32 VCP — used by many STM32G4 CAN-FD slcan adapters
}
_SERIAL_CAN_KEYWORDS = (
    "canable", "cantact", "slcan", "usb2can", "canbus", "can-fd", "canfd",
    "lawicel", "candlelight",
)


def detect_can_adapters() -> List[Dict[str, str]]:
    """Best-effort, cross-backend discovery of connected CAN adapters.

    Returns a list of ``{"interface", "channel", "label"}`` candidates with no
    side effects, so it is safe to call repeatedly from a background thread.
    Combines python-can's native USB detection with a serial-port heuristic
    for SLCAN adapters (CANable/CANtact), which python-can does not enumerate.
    """
    found: List[Dict[str, str]] = []
    seen = set()

    # 1) python-can native detection across the USB backends.
    try:
        import can
        try:
            configs = can.detect_available_configs(interfaces=list(_AUTODETECT_IFACES))
        except Exception:
            configs = []
        for c in configs:
            iface = str(c.get("interface") or "").strip()
            chan = c.get("channel")
            if not iface or chan is None:
                continue
            chan = str(chan)
            key = (iface, chan)
            if key in seen:
                continue
            seen.add(key)
            found.append({"interface": iface, "channel": chan,
                          "label": f"{iface}  (channel {chan})"})
    except Exception:
        pass

    # 2) Serial-port heuristic for SLCAN adapters (e.g. CANable in slcan mode).
    try:
        from serial.tools import list_ports
        for p in list_ports.comports():
            vid = getattr(p, "vid", None)
            pid = getattr(p, "pid", None)
            blob = " ".join(
                str(x or "").lower()
                for x in (getattr(p, "description", ""), getattr(p, "product", ""),
                          getattr(p, "manufacturer", ""), getattr(p, "hwid", ""))
            )
            is_can = (
                (vid is not None and pid is not None and (vid, pid) in _SERIAL_CAN_VIDPID)
                or any(k in blob for k in _SERIAL_CAN_KEYWORDS)
            )
            if not is_can:
                continue
            dev = p.device
            # On macOS the callout device (/dev/cu.*) is the right one to open.
            if sys.platform == "darwin" and "/tty." in dev:
                dev = dev.replace("/tty.", "/cu.")
            key = ("slcan", dev)
            if key in seen:
                continue
            seen.add(key)
            name = getattr(p, "product", None) or getattr(p, "description", None) or "CAN adapter"
            found.append({"interface": "slcan", "channel": dev,
                          "label": f"{name}  (slcan {dev})"})
    except Exception:
        pass

    return found


class _CanAutoDetectWorker(QThread):
    """Background worker: discovers CAN adapters across every backend."""
    auto_detect_complete = Signal(list)   # list[ {interface, channel, label} ]

    def run(self) -> None:
        try:
            results = detect_can_adapters()
        except Exception:
            results = []
        self.auto_detect_complete.emit(results)


# ── USB tab ───────────────────────────────────────────────────────────────────

class _UsbTab(QWidget):
    """USB / UART device picker — live driven by the shared PortScanner."""

    device_selected = Signal(str, str)   # device_path, display_label

    _BAUD_RATES = [
        9600, 19200, 38400, 57600, 115200,
        230400, 460800, 921600, 1500000,
    ]
    _DEFAULT_BAUD = 921600

    def __init__(self, scanner: PortScanner, parent=None) -> None:
        super().__init__(parent)
        self._scanner = scanner
        self._ports: Dict[str, PortInfo] = {}
        self._last_clicked: Optional[QListWidgetItem] = None

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(16, 16, 16, 16)

        info = QLabel(
            "Plug in your MCU — it appears below automatically.\n"
            "If nothing appears, check that no other Serial Monitor "
            "has the port open."
        )
        info.setObjectName("subtle")
        info.setAlignment(Qt.AlignLeft)
        info.setWordWrap(True)
        layout.addWidget(info)

        # ── Baud rate selector ──
        baud_row = QHBoxLayout()
        baud_row.setContentsMargins(0, 0, 0, 0)
        baud_row.setSpacing(8)
        baud_label = QLabel("Baud rate:")
        baud_row.addWidget(baud_label)
        self._baud_combo = QComboBox()
        for rate in self._BAUD_RATES:
            self._baud_combo.addItem(str(rate), rate)
        self._baud_combo.setCurrentIndex(self._BAUD_RATES.index(self._DEFAULT_BAUD))
        self._baud_combo.setFixedWidth(140)
        self._baud_combo.currentIndexChanged.connect(self._on_baud_changed)
        baud_row.addWidget(self._baud_combo)
        baud_row.addStretch()
        layout.addLayout(baud_row)

        self._list = QListWidget()
        self._list.setAlternatingRowColors(False)
        self._list.itemClicked.connect(self._on_item_clicked)
        self._list.itemDoubleClicked.connect(self._on_double_click)
        layout.addWidget(self._list, stretch=1)

        self._scan_status = QLabel("Scanning for USB devices…")
        self._scan_status.setObjectName("caption")
        layout.addWidget(self._scan_status)

        # Wire the long-running scanner signals.
        self._scanner.new_port_detected.connect(self._on_new_port)
        self._scanner.port_removed.connect(self._on_port_removed)
        self._scanner.scan_tick.connect(self._on_scan_tick)

    def selected_device(self) -> Optional[str]:
        item = self._list.currentItem()
        return item.data(Qt.UserRole) if item is not None else None

    def baud_rate(self) -> int:
        return self._baud_combo.currentData() or self._DEFAULT_BAUD

    def _on_baud_changed(self, _idx: int) -> None:
        # Re-emit the current selection so the parent updates its stored params
        # with the new baud rate before Connect is clicked.
        dev = self.selected_device()
        if dev:
            self.device_selected.emit(dev, self._ports[dev].description or dev)

    def _on_new_port(self, port: PortInfo) -> None:
        if port.device in self._ports:
            return
        self._ports[port.device] = port
        label = port.description or port.device
        item = QListWidgetItem(f"  ● {port.device}\n    {label}")
        item.setData(Qt.UserRole, port.device)
        self._list.addItem(item)

    def _on_port_removed(self, device: str) -> None:
        self._ports.pop(device, None)
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item.data(Qt.UserRole) == device:
                self._list.takeItem(i)
                break

    def _on_scan_tick(self, count: int) -> None:
        if count == 0:
            self._scan_status.setText("Scanning — no USB devices detected yet.")
        else:
            self._scan_status.setText(
                f"Scanning — {count} device{'s' if count != 1 else ''} visible."
            )

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        if item is self._last_clicked:
            self._list.clearSelection()
            self._last_clicked = None
        else:
            self._last_clicked = item
            dev = item.data(Qt.UserRole)
            if dev:
                self.device_selected.emit(dev, self._ports[dev].description or dev)

    def _on_selection(self) -> None:
        dev = self.selected_device()
        if dev:
            self.device_selected.emit(dev, self._ports[dev].description or dev)

    def _on_double_click(self, item: QListWidgetItem) -> None:
        dev = item.data(Qt.UserRole)
        if dev:
            self._last_clicked = item
            self.device_selected.emit(dev, self._ports[dev].description or dev)


# ── BLE tab ───────────────────────────────────────────────────────────────────

class _BleTab(QWidget):
    """BLE device picker — manual Scan button, since BLE is power-expensive."""

    device_selected = Signal(str, str)   # address, name

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._worker: Optional[_BleScanWorker] = None
        self._last_clicked: Optional[QListWidgetItem] = None

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(16, 16, 16, 16)

        info = QLabel(
            "Click 'Scan' to look for BLE devices broadcasting the Nordic "
            "UART Service.  Scanning takes about five seconds."
        )
        info.setObjectName("subtle")
        info.setWordWrap(True)
        layout.addWidget(info)

        row = QHBoxLayout()
        self.btn_scan = QPushButton("Scan")
        self.btn_scan.setObjectName("secondary")
        self.btn_scan.clicked.connect(self._start_scan)
        row.addWidget(self.btn_scan)
        row.addStretch()
        self._scan_status = QLabel("")
        self._scan_status.setObjectName("caption")
        row.addWidget(self._scan_status)
        layout.addLayout(row)

        self._list = QListWidget()
        self._list.itemClicked.connect(self._on_item_clicked)
        self._list.itemDoubleClicked.connect(self._on_double_click)
        layout.addWidget(self._list, stretch=1)

    def selected_address(self) -> Optional[str]:
        item = self._list.currentItem()
        return item.data(Qt.UserRole) if item is not None else None

    def selected_name(self) -> str:
        item = self._list.currentItem()
        if item is None:
            return ""
        return item.data(Qt.UserRole + 1) or ""

    def _start_scan(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        self._list.clear()
        self._last_clicked = None
        self.btn_scan.setEnabled(False)
        self._scan_status.setText("Scanning for 5 seconds…")
        self._worker = _BleScanWorker(timeout=5.0, parent=self)
        self._worker.scan_complete.connect(self._on_scan_done)
        self._worker.start()

    def _on_scan_done(self, results: list, error: str) -> None:
        self.btn_scan.setEnabled(True)
        if error:
            self._scan_status.setText(f"Scan error: {error}")
            return
        if not results:
            self._scan_status.setText("No BLE devices found.")
            return
        self._scan_status.setText(f"Found {len(results)} device(s).")
        # scan() returns (address, name, rssi) — ignore rssi and any extra fields.
        for entry in results:
            addr, name = entry[0], entry[1]
            rssi = entry[2] if len(entry) > 2 else None
            display = name or "(unnamed)"
            rssi_str = f"  {rssi} dBm" if rssi is not None else ""
            item = QListWidgetItem(f"  {display}{rssi_str}\n    {addr}")
            item.setData(Qt.UserRole, addr)
            item.setData(Qt.UserRole + 1, name)
            self._list.addItem(item)

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        if item is self._last_clicked:
            self._list.clearSelection()
            self._last_clicked = None
        else:
            self._last_clicked = item
            addr = item.data(Qt.UserRole)
            if addr:
                self.device_selected.emit(addr, item.data(Qt.UserRole + 1) or addr)

    def _on_selection(self) -> None:
        addr = self.selected_address()
        if addr:
            self.device_selected.emit(addr, self.selected_name() or addr)

    def _on_double_click(self, item: QListWidgetItem) -> None:
        addr = item.data(Qt.UserRole)
        if addr:
            self._last_clicked = item
            self.device_selected.emit(addr, item.data(Qt.UserRole + 1) or addr)


# ── TCP tab ───────────────────────────────────────────────────────────────────

class _TcpTab(QWidget):
    """TCP / Wi-Fi tab — host + port form (no auto-scan)."""

    device_selected = Signal(str, str)   # device_path, display_label

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        outer = QVBoxLayout(self)
        outer.setSpacing(12)
        outer.setContentsMargins(16, 16, 16, 16)

        info = QLabel(
            "Enter the IP address and TCP port of your Wi-Fi MCU.\n"
            "The firmware prints these to Serial when it boots."
        )
        info.setObjectName("subtle")
        info.setAlignment(Qt.AlignLeft)
        info.setWordWrap(True)
        outer.addWidget(info)

        # ── Centered form column (max 560 px) ──
        content = QWidget()
        content.setMaximumWidth(560)
        col = QVBoxLayout(content)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)

        form = QGridLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(10)
        form.setColumnStretch(0, 0)
        form.setColumnStretch(1, 1)
        form.setColumnStretch(2, 0)
        # Reserve the same trailing button-slot width that CAN uses, so the
        # input boxes here are visually the same width as CAN's inputs.
        form.setColumnMinimumWidth(2, 90)

        LABEL_ALIGN = Qt.AlignLeft | Qt.AlignVCenter

        form.addWidget(QLabel("IP / hostname:"), 0, 0, LABEL_ALIGN)
        self._host_edit = QLineEdit()
        self._host_edit.setPlaceholderText("e.g.  192.168.1.50  or  device.local")
        self._host_edit.textChanged.connect(self._emit_selection)
        form.addWidget(self._host_edit, 0, 1)

        form.addWidget(QLabel("Port:"), 1, 0, LABEL_ALIGN)
        self._port_spin = QSpinBox()
        self._port_spin.setRange(1, 65535)
        self._port_spin.setValue(7777)
        self._port_spin.valueChanged.connect(self._emit_selection)
        form.addWidget(self._port_spin, 1, 1)

        col.addLayout(form)
        col.addStretch()

        center = QHBoxLayout()
        center.setContentsMargins(0, 0, 0, 0)
        center.addStretch()
        center.addWidget(content)
        center.addStretch()
        outer.addLayout(center)
        outer.addStretch()

    def host(self) -> str:
        return self._host_edit.text().strip()

    def port(self) -> int:
        return self._port_spin.value()

    def is_valid(self) -> bool:
        return bool(self.host())

    def _emit_selection(self) -> None:
        if self.is_valid():
            label = f"TCP {self.host()}:{self.port()}"
            self.device_selected.emit(f"tcp://{self.host()}:{self.port()}", label)


# ── CAN tab ───────────────────────────────────────────────────────────────────

class _CanTab(QWidget):
    """CAN bus tab — works with any python-can compatible adapter."""

    device_selected = Signal(str, str)   # device_path, display_label

    # All interfaces supported by python-can.  Editable so users can type
    # any custom backend string.
    _INTERFACES = [
        "gs_usb",     # Candlelight / CANable 2.0  (most common for hobbyists)
        "slcan",      # Serial-line CAN — CANable, any SLCAN adapter
        "pcan",       # PEAK PCAN (most common in automotive/EV)
        "kvaser",     # Kvaser
        "socketcan",  # Linux built-in kernel driver
        "ixxat",      # HMS IXXAT
        "usb2can",    # 8devices USB2CAN
        "cantact",    # CANtact
        "vector",     # Vector Informatik
        "nican",      # National Instruments (NI-CAN)
        "nixnet",     # NI-XNET (NI CAN modules — common in test labs)
    ]

    # Default channel string per interface.  Shown as placeholder and
    # auto-filled when the user picks an interface with an empty channel field.
    _CHANNEL_DEFAULTS: Dict[str, str] = {
        "gs_usb":    "0",
        "slcan":     "COM3",         # overridden to /dev/ttyUSB0 on non-Windows
        "pcan":      "PCAN_USBBUS1",
        "kvaser":    "0",
        "socketcan": "can0",
        "ixxat":     "0",
        "usb2can":   "slcan0",
        "cantact":   "COM3",
        "vector":    "0",
        "nican":     "CAN0",
        "nixnet":    "CAN0",
    }

    _BITRATES = [
        ("125 kbps",  125_000),
        ("250 kbps",  250_000),
        ("500 kbps",  500_000),
        ("1 Mbps",  1_000_000),
    ]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        # Auto-detect state — set before any widget signal can fire.
        self._auto = True               # auto-pick interface+channel until user overrides
        self._applying = False          # True while we programmatically set widgets
        self._last_sig = None           # signature of last detected adapter set
        self._detect_worker: Optional[_CanDetectWorker] = None
        self._auto_worker: Optional[_CanAutoDetectWorker] = None

        # Fix serial-port defaults for non-Windows platforms.
        if sys.platform != "win32":
            self._CHANNEL_DEFAULTS = dict(self._CHANNEL_DEFAULTS)
            for k in ("slcan", "cantact"):
                self._CHANNEL_DEFAULTS[k] = "/dev/ttyUSB0"

        outer = QVBoxLayout(self)
        outer.setSpacing(12)
        outer.setContentsMargins(16, 16, 16, 16)

        # Info text spans the full tab width (left-aligned), matching TCP.
        info = QLabel(
            "Plug in a CAN adapter and the interface + channel fill in "
            "automatically — just pick the bitrate and connect.\n"
            "Works with Canable/SLCAN, PEAK PCAN, Kvaser, NI-XNET, Vector,\n"
            "SocketCAN (Linux), and any other python-can compatible hardware.\n\n"
            "Load a .dbc file to decode signal names and units.\n"
            "Without a .dbc file, raw frame bytes are captured as a single channel."
        )
        info.setObjectName("subtle")
        info.setAlignment(Qt.AlignLeft)
        info.setWordWrap(True)
        outer.addWidget(info)

        # ── Centered form column (max 560 px) ──
        content = QWidget()
        content.setMaximumWidth(560)
        col = QVBoxLayout(content)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)

        # ── 3-column grid: label | input (stretches) | optional button ──
        # QGridLayout gives explicit control over per-cell alignment, so every
        # label centers vertically against its input regardless of whether the
        # row has a trailing button.
        form = QGridLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(10)
        form.setColumnStretch(0, 0)   # label
        form.setColumnStretch(1, 1)   # input fills available space
        form.setColumnStretch(2, 0)   # optional button (fixed width)

        LABEL_ALIGN = Qt.AlignLeft | Qt.AlignVCenter

        # Row 0: Interface — non-editable so it renders as a proper dropdown.
        form.addWidget(QLabel("Interface:"), 0, 0, LABEL_ALIGN)
        self._iface_combo = QComboBox()
        self._iface_combo.addItems(self._INTERFACES)
        self._iface_combo.currentTextChanged.connect(self._on_iface_changed)
        # activated[int] fires only on a user pick — switch off auto-detect.
        self._iface_combo.activated.connect(lambda _i: self._on_manual_override())
        form.addWidget(self._iface_combo, 0, 1)

        # Row 1: Channel + Scan button (editable combo with live detection).
        form.addWidget(QLabel("Channel:"), 1, 0, LABEL_ALIGN)
        self._channel_combo = QComboBox()
        self._channel_combo.setEditable(True)
        self._channel_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._channel_combo.currentTextChanged.connect(self._emit_selection)
        # textEdited fires only on real user typing, not programmatic changes —
        # use it to detect a manual override of the auto-picked channel.
        self._channel_combo.lineEdit().textEdited.connect(self._on_manual_override)
        form.addWidget(self._channel_combo, 1, 1)

        self._detect_btn = QPushButton("Auto-detect")
        self._detect_btn.setObjectName("secondary")
        self._detect_btn.setFixedWidth(110)
        self._detect_btn.setToolTip(
            "Automatically find the connected CAN adapter and fill in the "
            "interface + channel"
        )
        self._detect_btn.clicked.connect(self._enable_auto)
        form.addWidget(self._detect_btn, 1, 2)

        # Row 2: Bitrate
        form.addWidget(QLabel("Bitrate:"), 2, 0, LABEL_ALIGN)
        self._bitrate_combo = QComboBox()
        for lbl, val in self._BITRATES:
            self._bitrate_combo.addItem(lbl, val)
        self._bitrate_combo.setCurrentIndex(2)   # 500 kbps default
        # Make sure the popup is wide enough for the longest item label.
        self._bitrate_combo.view().setMinimumWidth(160)
        self._bitrate_combo.currentIndexChanged.connect(self._emit_selection)
        form.addWidget(self._bitrate_combo, 2, 1)

        # Row 3: DBC file + Browse button
        form.addWidget(QLabel("DBC file:"), 3, 0, LABEL_ALIGN)
        self._dbc_edit = QLineEdit()
        self._dbc_edit.setPlaceholderText("(optional) .dbc file for signal decoding")
        self._dbc_edit.textChanged.connect(self._emit_selection)
        form.addWidget(self._dbc_edit, 3, 1)

        browse_btn = QPushButton("Browse")
        browse_btn.setObjectName("secondary")
        browse_btn.setFixedWidth(90)
        browse_btn.clicked.connect(self._browse_dbc)
        form.addWidget(browse_btn, 3, 2)

        col.addLayout(form)

        self._status_label = QLabel("")
        self._status_label.setObjectName("caption")
        col.addWidget(self._status_label)
        col.addStretch()

        center = QHBoxLayout()
        center.setContentsMargins(0, 0, 0, 0)
        center.addStretch()
        center.addWidget(content)
        center.addStretch()
        outer.addLayout(center)

        # Populate channel combo with the first interface's default (programmatic,
        # so it doesn't count as a manual override).
        self._applying = True
        self._on_iface_changed(self._INTERFACES[0])
        self._applying = False

        # Poll for adapters while the CAN tab is visible, so plugging one in
        # auto-fills the form without the user touching anything.
        self._auto_timer = QTimer(self)
        self._auto_timer.setInterval(3000)
        self._auto_timer.timeout.connect(self._run_autodetect)

    # ── public ───────────────────────────────────────────────────────────────

    def params(self) -> Dict[str, Any]:
        return {
            "interface": self._iface_combo.currentText().strip(),
            "channel":   self._channel_combo.currentText().strip(),
            "bitrate":   self._bitrate_combo.currentData(),
            "dbc_path":  self._dbc_edit.text().strip() or None,
        }

    def is_valid(self) -> bool:
        return bool(self._channel_combo.currentText().strip())

    # ── private ──────────────────────────────────────────────────────────────

    def _on_iface_changed(self, iface: str) -> None:
        default = self._CHANNEL_DEFAULTS.get(iface, "")
        self._channel_combo.clear()
        self._channel_combo.addItem(default)
        self._channel_combo.setCurrentText(default)
        # Skip the per-interface probe while we're programmatically applying an
        # auto-detected adapter — the channel is already being set for us.
        if not self._applying:
            self._start_detect(iface)
        self._emit_selection()

    # ── auto-detect ────────────────────────────────────────────────────────────

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if getattr(self, "_auto_timer", None) is None:
            return
        if self._auto:
            self._run_autodetect()
        self._auto_timer.start()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        timer = getattr(self, "_auto_timer", None)
        if timer is not None:
            timer.stop()

    def _on_manual_override(self, *args) -> None:
        """The user picked an interface / typed a channel — stop auto-filling."""
        if self._applying:
            return
        if self._auto:
            self._auto = False
            self._status_label.setText(
                "Manual mode — click 'Auto-detect' to resume automatic selection."
            )

    def _enable_auto(self) -> None:
        self._auto = True
        self._last_sig = None
        self._status_label.setText("Searching for a CAN adapter…")
        self._run_autodetect()

    def _run_autodetect(self) -> None:
        if not self._auto:
            return
        if self._auto_worker is not None and self._auto_worker.isRunning():
            return
        self._auto_worker = _CanAutoDetectWorker(parent=self)
        self._auto_worker.auto_detect_complete.connect(self._on_autodetect_done)
        self._auto_worker.start()

    def _on_autodetect_done(self, candidates: list) -> None:
        if not self._auto:
            return
        sig = tuple((c["interface"], c["channel"]) for c in candidates)
        if sig == self._last_sig:
            return                          # nothing changed since last poll
        self._last_sig = sig

        if not candidates:
            self._status_label.setText("Searching for a CAN adapter — plug one in…")
            return

        best = candidates[0]
        self._apply_candidate(best)
        extra = (f"   (+{len(candidates) - 1} more — switch via the Interface list)"
                 if len(candidates) > 1 else "")
        self._status_label.setText(f"✓ Auto-detected: {best['label']}{extra}")

    def _apply_candidate(self, cand: Dict[str, str]) -> None:
        """Programmatically set the interface + channel from a detected adapter."""
        self._applying = True
        try:
            idx = self._iface_combo.findText(cand["interface"])
            if idx >= 0:
                self._iface_combo.setCurrentIndex(idx)
            channel = str(cand["channel"])
            self._channel_combo.clear()
            self._channel_combo.addItem(channel)
            self._channel_combo.setCurrentText(channel)
        finally:
            self._applying = False
        self._emit_selection()

    def _start_detect(self, iface: str) -> None:
        if self._detect_worker is not None and self._detect_worker.isRunning():
            return
        self._status_label.setText("Detecting adapters…")
        self._detect_btn.setEnabled(False)
        self._detect_worker = _CanDetectWorker(iface, parent=self)
        self._detect_worker.detect_complete.connect(self._on_detect_done)
        self._detect_worker.start()

    def _on_detect_done(self, channels: list, error: str) -> None:
        self._detect_btn.setEnabled(True)
        iface = self._iface_combo.currentText().strip()
        default = self._CHANNEL_DEFAULTS.get(iface, "")

        if channels:
            self._channel_combo.clear()
            for ch in channels:
                self._channel_combo.addItem(ch)
            self._channel_combo.setCurrentIndex(0)
            n = len(channels)
            self._status_label.setText(
                f"{n} adapter{'s' if n > 1 else ''} found."
            )
        else:
            if not self._channel_combo.currentText().strip():
                self._channel_combo.setCurrentText(default)
            self._status_label.setText(
                "No adapters detected — default channel pre-filled."
                if not error else
                f"Detection unavailable: {error.splitlines()[0]}"
            )
        self._emit_selection()

    def _browse_dbc(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open DBC file", "",
            "DBC files (*.dbc);;All files (*)"
        )
        if path:
            self._dbc_edit.setText(path)

    def _emit_selection(self) -> None:
        if not self.is_valid():
            return
        p = self.params()
        label       = f"CAN {p['interface']} / {p['channel']}"
        device_path = f"can://{p['interface']}/{p['channel']}"
        self.device_selected.emit(device_path, label)


# ── File/CSV tab ─────────────────────────────────────────────────────────────

class _FileTab(QWidget):
    """File/CSV tab — browse for a live-appending CSV, TSV, or Parquet file."""

    device_selected = Signal(str, str)   # file_path, display_label

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._file_path: str = ""

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(16, 16, 16, 16)

        info = QLabel(
            "Select a CSV, TSV, or Parquet file that another process is actively\n"
            "appending to. The software tails the file and plots new rows in real time.\n\n"
            "Supported formats: .csv  .tsv  .parquet  .pq"
        )
        info.setObjectName("subtle")
        info.setWordWrap(True)
        layout.addWidget(info)

        row = QHBoxLayout()
        self._path_edit = QLineEdit()
        self._path_edit.setPlaceholderText("No file selected")
        self._path_edit.setReadOnly(True)
        row.addWidget(self._path_edit, stretch=1)
        browse_btn = QPushButton("Browse")
        browse_btn.setObjectName("secondary")
        browse_btn.setFixedWidth(100)
        browse_btn.clicked.connect(self._browse)
        row.addWidget(browse_btn)
        layout.addLayout(row)
        layout.addStretch()

    def selected_path(self) -> str:
        return self._file_path

    def is_valid(self) -> bool:
        return bool(self._file_path)

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Live Data File", "",
            "Data Files (*.csv *.tsv *.parquet *.pq);;All Files (*)"
        )
        if path:
            self._file_path = path
            self._path_edit.setText(path)
            self.device_selected.emit(path, f"File: {os.path.basename(path)}")

    def _emit_selection(self) -> None:
        if self._file_path:
            self.device_selected.emit(
                self._file_path,
                f"File: {os.path.basename(self._file_path)}"
            )


# ── ConnectionScreen ──────────────────────────────────────────────────────────

@dataclass
class ConnectionRequest:
    """Returned by ConnectionScreen when the user clicks Connect."""
    transport: str          # "usb" | "ble" | "tcp"
    params: Dict[str, Any]
    display_label: str


class ConnectionScreen(QWidget):
    """Tabs across the top: USB / BLE / TCP.  One Connect button at the bottom."""

    home_requested       = Signal()
    connect_requested    = Signal(object)   # ConnectionRequest
    view_plots_requested = Signal()         # emitted when user clicks View Plots

    def __init__(self, scanner: PortScanner, parent=None) -> None:
        super().__init__(parent)
        self._scanner = scanner
        self._selected_transport: Optional[str] = None
        self._selected_params: Dict[str, Any] = {}
        self._selected_label: str = ""

        self._build_ui()
        self._wire_signals()
        self._refresh_connect_state()

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Top bar with Home button ──
        top = QFrame(objectName="topBar")
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(18, 10, 18, 10)

        self.btn_home = QPushButton("←  Home")
        self.btn_home.setObjectName("homeBtn")
        self.btn_home.clicked.connect(self.home_requested.emit)
        top_layout.addWidget(self.btn_home)
        top_layout.addStretch()

        root.addWidget(top)

        # ── Centered content area ──
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(40, 28, 40, 28)
        body_layout.setSpacing(20)
        body_layout.setAlignment(Qt.AlignTop)

        intro = QLabel(
            "Pick a transport, then select your device from the list "
            "the scanner builds for you."
        )
        intro.setObjectName("subtle")
        body_layout.addWidget(intro)

        # Tabs.
        self._tabs = QTabWidget()
        self._tabs.setDocumentMode(True)

        self._usb_tab  = _UsbTab(self._scanner)
        self._ble_tab  = _BleTab()
        self._tcp_tab  = _TcpTab()
        self._can_tab  = _CanTab()
        self._file_tab = _FileTab()

        self._tabs.addTab(self._usb_tab,  "USB / UART")
        self._tabs.addTab(self._ble_tab,  "Bluetooth LE")
        self._tabs.addTab(self._tcp_tab,  "TCP / Wi-Fi")
        self._tabs.addTab(self._can_tab,  "CAN Bus")
        self._tabs.addTab(self._file_tab, "File / CSV")
        self._tabs.currentChanged.connect(self._on_tab_changed)
        body_layout.addWidget(self._tabs, stretch=1)

        # ── Connected Devices panel ──
        devices_section = QWidget()
        devices_section_layout = QVBoxLayout(devices_section)
        devices_section_layout.setContentsMargins(16, 0, 16, 0)
        devices_section_layout.setSpacing(6)

        conn_label = QLabel("Connected devices:")
        conn_label.setObjectName("subtle")
        devices_section_layout.addWidget(conn_label)

        self._connected_list = QListWidget()
        self._connected_list.setMaximumHeight(80)
        self._connected_list.setObjectName("connectedList")
        self._connected_list.setFocusPolicy(Qt.NoFocus)
        self._connected_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        devices_section_layout.addWidget(self._connected_list)

        body_layout.addWidget(devices_section)

        # Bottom action row — 16px inner margin matches Connected Devices alignment.
        action_section = QWidget()
        action_row = QHBoxLayout(action_section)
        action_row.setContentsMargins(16, 0, 16, 0)

        self.status_label = QLabel("Select a device to continue.")
        self.status_label.setObjectName("subtle")
        action_row.addWidget(self.status_label)
        action_row.addStretch()

        self.btn_connect = QPushButton("Connect  →")
        self.btn_connect.setObjectName("primary")
        self.btn_connect.setMinimumWidth(160)
        self.btn_connect.clicked.connect(self._on_connect_clicked)
        action_row.addWidget(self.btn_connect)

        self.btn_view_plots = QPushButton("View Plots  →")
        self.btn_view_plots.setObjectName("primary")
        self.btn_view_plots.setMinimumWidth(160)
        self.btn_view_plots.setEnabled(False)
        self.btn_view_plots.clicked.connect(self.view_plots_requested.emit)
        action_row.addWidget(self.btn_view_plots)

        body_layout.addWidget(action_section)

        root.addWidget(body, stretch=1)

    # ── Signal wiring ────────────────────────────────────────────────────────

    def _wire_signals(self) -> None:
        self._usb_tab.device_selected.connect(
            lambda dev, lbl: self._record_selection(
                "usb",
                {"device_path": dev, "baud_rate": self._usb_tab.baud_rate()},
                lbl,
            )
        )
        self._ble_tab.device_selected.connect(
            lambda addr, name: self._record_selection(
                "ble", {"address": addr, "name": name}, name
            )
        )
        self._tcp_tab.device_selected.connect(
            lambda dev_path, lbl: self._record_selection(
                "tcp",
                {"host": self._tcp_tab.host(), "port": self._tcp_tab.port()},
                lbl,
            )
        )
        self._can_tab.device_selected.connect(
            lambda dev_path, lbl: self._record_selection(
                "can", self._can_tab.params(), lbl
            )
        )
        self._file_tab.device_selected.connect(
            lambda path, lbl: self._record_selection(
                "file", {"file_path": path}, lbl
            )
        )

    def _record_selection(self, transport: str, params: Dict[str, Any],
                          label: str) -> None:
        # Ignore emissions from tabs that aren't currently visible. The CAN
        # tab in particular auto-detects adapters on construction and emits a
        # selection asynchronously — without this guard it would silently
        # overwrite the USB tab's selection when the app first opens.
        tab_for_transport = {
            "usb":  self._usb_tab,
            "ble":  self._ble_tab,
            "tcp":  self._tcp_tab,
            "can":  self._can_tab,
            "file": self._file_tab,
        }.get(transport)
        if tab_for_transport is not None and self._tabs.currentWidget() is not tab_for_transport:
            return
        self._selected_transport = transport
        self._selected_params    = params
        self._selected_label     = label
        self._refresh_connect_state()

    def _on_tab_changed(self, _idx: int) -> None:
        self._selected_transport = None
        self._selected_params    = {}
        self._selected_label     = ""
        # Re-emit current selection from the now-active tab.
        cur = self._tabs.currentWidget()
        if cur is self._usb_tab:
            dev = self._usb_tab.selected_device()
            if dev:
                self._usb_tab._on_selection()
        elif cur is self._ble_tab:
            addr = self._ble_tab.selected_address()
            if addr:
                self._ble_tab._on_selection()
        elif cur is self._tcp_tab:
            self._tcp_tab._emit_selection()
        elif cur is self._can_tab:
            self._can_tab._emit_selection()
        elif cur is self._file_tab:
            self._file_tab._emit_selection()
        self._refresh_connect_state()

    def _refresh_connect_state(self) -> None:
        ok = self._selected_transport is not None and bool(self._selected_label)
        self.btn_connect.setEnabled(ok)
        if ok:
            self.status_label.setText(f"Ready to connect to:  {self._selected_label}")
        else:
            self.status_label.setText("Select a device to continue.")

    def reset_connect_button(self) -> None:
        """Restore the Connect button after a failed or cancelled attempt."""
        self.btn_connect.setText("Connect  →")
        self.btn_connect.setEnabled(
            self._selected_transport is not None and bool(self._selected_label)
        )
        self.status_label.setText(
            f"Ready to connect to:  {self._selected_label}"
            if self._selected_transport else "Select a device to continue."
        )

    def _on_connect_clicked(self) -> None:
        if not self._selected_transport:
            return
        self.btn_connect.setText("Connecting…")
        self.btn_connect.setEnabled(False)
        self.status_label.setText(f"Connecting to  {self._selected_label}…")
        req = ConnectionRequest(
            transport=self._selected_transport,
            params=self._selected_params,
            display_label=self._selected_label,
        )
        self.connect_requested.emit(req)

    def add_connected_device(self, label: str, transport: str) -> None:
        """Called by MainWindow when a device handshake completes successfully.

        Adds the device to the Connected Devices list, enables View Plots,
        and resets the Connect button so the user can add another device.
        """
        self._connected_list.addItem(f"  ●  {label}  ({transport})")
        n = self._connected_list.count()
        self.btn_view_plots.setEnabled(True)
        self.btn_view_plots.setText(f"View Plots ({n})  →")
        self.reset_connect_button()
        self.status_label.setText(
            "Device ready. Connect another or click View Plots."
        )

    def clear_connected_devices(self) -> None:
        """Reset the Connected Devices list and disable View Plots.

        Called when all sessions have been torn down (e.g. user returned to
        the launch screen) so the next visit to this screen does not show
        stale entries that no longer correspond to live device sessions.
        """
        self._connected_list.clear()
        self.btn_view_plots.setEnabled(False)
        self.btn_view_plots.setText("View Plots  →")
