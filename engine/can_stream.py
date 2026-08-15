"""CAN Bus telemetry streamer.

Monitors a CAN bus using python-can and decodes frames with cantools (DBC
files).  Mirrors the IngestionWorker contract of TelemetryStreamer.

Topology
--------
    ECU nodes ──CAN bus──▶  python-can Bus (this worker)
                                │
                          cantools .dbc decode
                                │
                          MultiChannelRingBuffer

Unlike the USB/TCP/MQTT workers, there is no DAQ V2 handshake.  The
desktop synthesizes a DeviceMetadataV2 from the DBC signal definitions
before streaming begins.  Every received CAN frame is decoded and the
latest signal values are pushed into the ring buffer.

Interfaces supported
--------------------
    "virtual"   — python-can in-process bus; two Bus objects on the same
                  channel communicate.  Requires no hardware.  Used by the
                  virtual test bench.
    "socketcan" — Linux SocketCAN (e.g. "can0"); requires a physical or
                  virtual (vcan) interface.
    "pcan"      — Peak PCAN adapters (Windows/Linux/macOS).
    "kvaser"    — Kvaser adapters.
    Any other interface string supported by python-can.

Device path
-----------
    can://<interface>/<channel>   e.g. can://virtual/test_channel
"""

from __future__ import annotations

import hashlib
import queue
import struct
import time
from typing import Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import QThread, Signal

try:
    import can
    _CAN_AVAILABLE = True
except ImportError:
    can = None
    _CAN_AVAILABLE = False

try:
    import cantools
    _CANTOOLS_AVAILABLE = True
except ImportError:
    cantools = None
    _CANTOOLS_AVAILABLE = False

from .buffer import MultiChannelRingBuffer
from .protocol import ChannelSpecV2, DeviceMetadataV2


BUFFER_CAPACITY = 50_000
CAN_STOP_TIMEOUT_MS = 1_500
CAN_RECV_TIMEOUT_S = 0.2


def can_device_path(interface: str, channel: str) -> str:
    return f"can://{interface}/{channel}"


def _friendly_bus_error(interface: str, exc: Exception) -> str:
    """Return a human-readable error when a CAN bus fails to open."""
    detail = str(exc)
    low = detail.lower()
    iface = interface.lower()

    if iface == "pcan" and any(k in low for k in ("dll", "not found", "cannot find", "no module")):
        return (
            "PEAK PCAN driver not installed.\n"
            "Download the free driver from: peak-system.com → Downloads → PCAN-Basic\n\n"
            f"Detail: {exc}"
        )
    if iface == "kvaser" and any(k in low for k in ("dll", "not found", "canlib")):
        return (
            "Kvaser driver not installed.\n"
            "Download from: kvaser.com → Support → Downloads → Kvaser drivers\n\n"
            f"Detail: {exc}"
        )
    if iface == "socketcan" and "no such device" in low:
        channel = detail.split("'")[1] if "'" in detail else "can0"
        return (
            f"SocketCAN interface '{channel}' is not up.\n"
            f"Run:  sudo ip link set {channel} up type can bitrate 500000\n\n"
            f"Detail: {exc}"
        )
    if iface in ("slcan", "cantact", "gs_usb") and any(k in low for k in ("permission", "access", "serial")):
        return (
            f"Could not open serial port for {interface}.\n"
            "Check that the adapter is plugged in and no other program has the port open.\n\n"
            f"Detail: {exc}"
        )
    return f"CAN bus open failed: {exc}"


class CanStreamer(QThread):
    """IngestionWorker that reads and decodes CAN frames from a python-can Bus.

    Channel layout (ring buffer):
        row 0        : PC_Time_s  (injected by SessionManager)
        rows 1..N    : decoded DBC signal values in signal-definition order
    """

    metadata_ready  = Signal(str, object)
    handshake_failed = Signal(str, str)
    stream_stopped  = Signal(str, str)

    def __init__(self, interface: str, channel: str,
                 dbc_path: Optional[str] = None,
                 arbitration_ids: Optional[List[int]] = None,
                 bitrate: int = 500_000,
                 parent=None) -> None:
        super().__init__(parent)
        if not _CAN_AVAILABLE:
            raise RuntimeError("python-can not installed — `pip install python-can`")

        self._interface = interface
        self._channel = channel
        self._dbc_path = dbc_path
        self._filter_ids = set(arbitration_ids) if arbitration_ids else None
        self._bitrate = bitrate
        self._device_path = can_device_path(interface, channel)

        self._bus: Optional["can.BusABC"] = None
        self._db = None                             # cantools Database or None
        self._running = False

        # Signal → latest decoded float value (updated as frames arrive).
        self._latest: Dict[str, float] = {}

        # Ordered list of signal names (same order as ring buffer channels).
        self._signal_names: List[str] = []

        # message lookup: arbitration_id → cantools Message object
        self._id_to_msg: Dict[int, object] = {}

        self._buffer: Optional[MultiChannelRingBuffer] = None
        self._external_buffer: Optional[MultiChannelRingBuffer] = None
        self._logging_queue: Optional[queue.Queue] = None
        self._producer_dropped = 0
        self._session_clock: Optional[Callable[[], float]] = None
        self._metadata: Optional[DeviceMetadataV2] = None

    # ------------------------------------------------------------------ #
    #  Public accessors                                                    #
    # ------------------------------------------------------------------ #

    @property
    def device_path(self) -> str:
        return self._device_path

    @property
    def interface(self) -> str:
        return self._interface

    @property
    def channel(self) -> str:
        return self._channel

    def buffer(self) -> Optional[MultiChannelRingBuffer]:
        return self._buffer

    def metadata(self) -> Optional[DeviceMetadataV2]:
        return self._metadata

    @property
    def producer_dropped(self) -> int:
        return self._producer_dropped

    def set_logging_queue(self, q: Optional[queue.Queue]) -> None:
        self._logging_queue = q

    def set_session_clock(self, clock_fn: Optional[Callable[[], float]]) -> None:
        self._session_clock = clock_fn

    def attach_buffer(self, buffer: MultiChannelRingBuffer) -> None:
        self._external_buffer = buffer
        self._buffer = buffer

    def stop(self) -> None:
        self._running = False

    def abort(self) -> None:
        self._running = False
        bus = self._bus
        if bus is not None:
            try:
                bus.shutdown()
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    #  Metadata synthesis from DBC                                         #
    # ------------------------------------------------------------------ #

    def _build_metadata(self) -> DeviceMetadataV2:
        """Synthesise DeviceMetadataV2 from the loaded DBC database."""
        channels: List[ChannelSpecV2] = []
        ch_idx = 0

        if self._db is not None:
            # Collect signals in message → signal order.
            for msg in sorted(self._db.messages, key=lambda m: m.frame_id):
                if self._filter_ids and msg.frame_id not in self._filter_ids:
                    continue
                self._id_to_msg[msg.frame_id] = msg
                for sig in sorted(msg.signals, key=lambda s: s.start):
                    self._signal_names.append(sig.name)
                    self._latest[sig.name] = 0.0
                    channels.append(ChannelSpecV2(
                        channel_id=ch_idx,
                        name=sig.name[:16],
                        unit=(sig.unit or "")[:8],
                        type_code=0x08,          # float64
                        struct_fmt="d",
                        size_bytes=8,
                        label="float64",
                        range_min=float(sig.minimum) if sig.minimum is not None else float("nan"),
                        range_max=float(sig.maximum) if sig.maximum is not None else float("nan"),
                    ))
                    ch_idx += 1
        else:
            # No DBC — expose one raw channel for the raw hex bytes per frame.
            # Channels are created lazily as we see frames; for the metadata
            # we create a placeholder that the UI will display.
            self._signal_names = ["raw_value"]
            self._latest = {"raw_value": 0.0}
            channels.append(ChannelSpecV2(
                channel_id=0,
                name="raw_value",
                unit="",
                type_code=0x08,
                struct_fmt="d",
                size_bytes=8,
                label="float64",
                range_min=float("nan"),
                range_max=float("nan"),
            ))

        # Device UID derived from DBC content hash for stable registry identity.
        uid_src = (self._dbc_path or "") + self._interface + self._channel
        uid = hashlib.sha256(uid_src.encode()).hexdigest()[:32]

        dbc_name = ""
        if self._dbc_path:
            import os
            dbc_name = f" ({os.path.basename(self._dbc_path)})"

        return DeviceMetadataV2(
            device_uid=uid,
            device_name=f"CAN{dbc_name}"[:32],
            firmware_version="can-bus",
            channels=tuple(channels),
        )

    # ------------------------------------------------------------------ #
    #  QThread entry                                                        #
    # ------------------------------------------------------------------ #

    def run(self) -> None:
        # 1. Load DBC.
        if self._dbc_path is not None:
            if not _CANTOOLS_AVAILABLE:
                self.handshake_failed.emit(
                    self._device_path,
                    "cantools not installed — `pip install cantools`")
                return
            try:
                self._db = cantools.database.load_file(self._dbc_path)
            except Exception as exc:
                self.handshake_failed.emit(
                    self._device_path, f"DBC load failed: {exc}")
                return

        # 2. Build synthetic metadata from DBC signals.
        try:
            self._metadata = self._build_metadata()
        except Exception as exc:
            self.handshake_failed.emit(
                self._device_path, f"metadata build failed: {exc}")
            return

        # 3. Open the CAN bus.
        try:
            kwargs: dict = {"interface": self._interface,
                            "channel": self._channel}
            if self._interface not in ("virtual",):
                kwargs["bitrate"] = self._bitrate
            self._bus = can.Bus(**kwargs)
        except Exception as exc:
            self.handshake_failed.emit(
                self._device_path, _friendly_bus_error(self._interface, exc))
            return

        # 4. Hand off synthetic metadata (SessionManager allocates buffer).
        self.metadata_ready.emit(self._device_path, self._metadata)
        if self._external_buffer is None:
            self._buffer = MultiChannelRingBuffer(
                BUFFER_CAPACITY, self._metadata.num_channels)

        # 5. Stream — decode frames and push to ring buffer.
        self._running = True
        reason = self._receive_loop()
        try:
            self._bus.shutdown()
        except Exception:
            pass
        self.stream_stopped.emit(self._device_path, reason)

    # ------------------------------------------------------------------ #
    #  Receive loop                                                         #
    # ------------------------------------------------------------------ #

    def _receive_loop(self) -> str:
        assert self._bus is not None
        assert self._buffer is not None

        session_clock = self._session_clock
        prepend_ts = (
            session_clock is not None
            and self._buffer.num_channels == self._metadata.num_channels + 1
        )
        push = self._buffer.push
        n_signals = len(self._signal_names)

        while self._running:
            try:
                msg = self._bus.recv(timeout=CAN_RECV_TIMEOUT_S)
            except Exception as exc:
                return f"io_error: {exc}"

            if msg is None:
                continue

            # Decode the frame if we have a DBC entry for it.
            if self._db is not None and msg.arbitration_id in self._id_to_msg:
                db_msg = self._id_to_msg[msg.arbitration_id]
                try:
                    decoded = db_msg.decode(msg.data, decode_choices=False)
                    for sig_name, value in decoded.items():
                        if sig_name in self._latest:
                            self._latest[sig_name] = float(value)
                except Exception:
                    continue
            elif self._db is None:
                # Raw mode: pack the data bytes as a float.
                raw = int.from_bytes(msg.data, "big") if msg.data else 0
                self._latest["raw_value"] = float(raw)

            # Build a sample tuple in signal order.
            sample_vals = [self._latest.get(n, 0.0) for n in self._signal_names]

            if prepend_ts:
                augmented = (session_clock(), *sample_vals)
            else:
                augmented = tuple(sample_vals)

            try:
                push(augmented)
            except ValueError:
                continue

            log_q = self._logging_queue
            if log_q is not None:
                try:
                    log_q.put_nowait(augmented)
                except queue.Full:
                    self._producer_dropped += 1

        return "user_stop"
