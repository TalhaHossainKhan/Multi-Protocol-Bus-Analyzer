"""SessionManager — single source of truth for the active multi-device session.

Step-5 orchestrator.  Owns every per-device state object (`DeviceSession`),
the session-start reference (`time.perf_counter()`), and the lifecycle of
the per-device `TelemetryStreamer` and `DataLogger` threads.

Thread-safety contract
======================
* `_devices` is guarded by `_lock` (a `threading.Lock`).  The lock is held
  ONLY around dict mutation or snapshot — microseconds, never across I/O,
  never across a Qt signal emission, never across a buffer read or a
  streamer call.
* `_session_start_perf` is a single Python float.  Assignment / read of a
  bound name is atomic under CPython's GIL, so it can be read without the
  lock from the ingestion threads via `session_clock_now()`.
* Per-device `MultiChannelRingBuffer`s have their own internal lock; we
  never wrap their reads or writes in the registry lock.
* Per-device `DataLogger.queue` is a `queue.Queue` (its own internal lock).
  We never hold the registry lock while putting to or getting from it.

Lock ordering (top-down, never reversed):
    _registry_lock  →  (release)  →  buffer._lock  →  (release)
                                  →  queue.Queue lock  →  (release)
No nested acquisition.  Deadlock is structurally impossible.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Union

# Any ingestion worker that mirrors the TelemetryStreamer contract.
IngestionWorker = Union[
    "TelemetryStreamer", "TcpStreamer", "BleStreamer", "CanStreamer",
    "LiveFileWorker",
]

from PySide6.QtCore import QObject, Qt, Signal, Slot

from .buffer import MultiChannelRingBuffer
from .data_logger import (
    ChannelSpec,
    DataLogger,
    make_bounded_queue,
)
from .protocol import DeviceMetadataV2
from .telemetry_stream import (
    BUFFER_CAPACITY,
    DEFAULT_BAUD,
    STREAMER_STOP_TIMEOUT_MS,
    TelemetryStreamer,
)
from .tcp_stream import (
    DEFAULT_TCP_PORT,
    TCP_STOP_TIMEOUT_MS,
    TcpStreamer,
    tcp_device_path,
)
from .ble_stream import (
    BLE_STOP_TIMEOUT_MS,
    BleStreamer,
    ble_device_path,
)
from .can_stream import (
    CAN_STOP_TIMEOUT_MS,
    CanStreamer,
    can_device_path,
)
from .file_stream import (
    FILE_STOP_TIMEOUT_MS,
    LiveFileWorker,
    file_device_path,
)

LOGGER_STOP_TIMEOUT_MS = 2_000

# Name of the injected PC-monotonic timestamp column (CSV header).
PC_TIME_COLUMN_NAME = "PC_Time_s"


@dataclass
class DeviceSession:
    """All per-device state owned by the SessionManager.

    The streamer is created at `add_device`; metadata/buffer get attached
    once the handshake completes.  Logger is lazy-attached on first
    recording start.
    """
    device_path: str
    streamer: "IngestionWorker"   # serial / TCP / BLE / CAN / file — same contract
    metadata: Optional[DeviceMetadataV2] = None
    buffer: Optional[MultiChannelRingBuffer] = None
    logger: Optional[DataLogger] = None
    color_index: int = 0
    transport: str = "usb"        # "usb" | "tcp" | "ble" | "can" | "file" — UI hint


class SessionManager(QObject):
    """Centralised orchestrator for every active device in the test session."""

    # Per-device lifecycle.
    device_added       = Signal(str)              # device_path (row just listed)
    device_ready       = Signal(str, object)      # device_path, DeviceMetadataV2
    device_removed     = Signal(str)              # device_path
    device_failed      = Signal(str, str)         # device_path, error message

    # Session-wide.
    session_started    = Signal(float)            # perf_counter reference
    session_ended      = Signal()

    # File-watcher header negotiation.
    file_headers_detected = Signal(str, list)     # device_path, [col_name, …]

    # Recording.
    recording_changed  = Signal(str, bool)        # device_path, is_recording
    recording_error    = Signal(str, str)         # device_path, error message
    recording_rows     = Signal(str, int, str)    # device_path, total_rows, file_path
    recording_backlog  = Signal(str, int)         # device_path, queue_depth
    recording_dropped  = Signal(str, int)         # device_path, total_dropped

    # ------------------------------------------------------------------ #
    #  Construction                                                       #
    # ------------------------------------------------------------------ #

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._lock = threading.Lock()
        self._devices: Dict[str, DeviceSession] = {}
        self._session_start_perf: Optional[float] = None
        self._next_color_idx: int = 0

    # ------------------------------------------------------------------ #
    #  Session clock                                                       #
    # ------------------------------------------------------------------ #

    def start_session(self) -> float:
        """Establish t=0 if not yet running.  Returns the perf_counter ref."""
        fire_started = False
        with self._lock:
            if self._session_start_perf is None:
                self._session_start_perf = time.perf_counter()
                fire_started = True
            start = self._session_start_perf
        if fire_started:
            self.session_started.emit(start)
        return start

    def session_clock_now(self) -> float:
        """Seconds elapsed since session start.  0.0 if no session is active.

        Called from EVERY ingestion thread on every parsed frame.  Hot path —
        avoids the lock by reading the float reference directly (atomic in
        CPython).  Worst-case race: a brand-new session reads None just as
        another thread sets the start time → returns 0.0 for one sample.
        That's harmless.
        """
        start = self._session_start_perf
        if start is None:
            return 0.0
        return time.perf_counter() - start

    def _maybe_end_session(self) -> None:
        """If the device registry is empty, clear the session clock."""
        fire_ended = False
        with self._lock:
            if not self._devices and self._session_start_perf is not None:
                self._session_start_perf = None
                fire_ended = True
        if fire_ended:
            self.session_ended.emit()

    # ------------------------------------------------------------------ #
    #  Device lifecycle                                                    #
    # ------------------------------------------------------------------ #

    def add_device(self, device_path: str, baud: int = DEFAULT_BAUD) -> DeviceSession:
        """Spin up a USB-serial TelemetryStreamer for `device_path`."""
        streamer = TelemetryStreamer(device_path, baud=baud, parent=self)
        return self._register_worker(device_path, streamer, transport="usb")

    def add_tcp_device(self, host: str,
                       port: int = DEFAULT_TCP_PORT) -> DeviceSession:
        """Spin up a TCP IngestionWorker for an MCU at `host:port`."""
        device_path = tcp_device_path(host, port)
        streamer = TcpStreamer(host, port=port, parent=self)
        return self._register_worker(device_path, streamer, transport="tcp")

    def add_ble_device(self, address: str) -> DeviceSession:
        """Spin up a BLE IngestionWorker subscribed to the device at `address`."""
        device_path = ble_device_path(address)
        streamer = BleStreamer(address, parent=self)
        return self._register_worker(device_path, streamer, transport="ble")

    def add_can_device(self, interface: str, channel: str,
                       dbc_path: Optional[str] = None,
                       arbitration_ids: Optional[list] = None,
                       bitrate: int = 500_000) -> DeviceSession:
        """Spin up a CAN IngestionWorker on `interface`/`channel`.

        `dbc_path` is optional; without it the worker captures raw frame data.
        `arbitration_ids` filters which message IDs are decoded (None = all in DBC).
        """
        device_path = can_device_path(interface, channel)
        streamer = CanStreamer(
            interface=interface, channel=channel,
            dbc_path=dbc_path, arbitration_ids=arbitration_ids,
            bitrate=bitrate, parent=self,
        )
        return self._register_worker(device_path, streamer, transport="can")

    def add_file_device(self, file_path: str) -> DeviceSession:
        """Spin up a LiveFileWorker that tails `file_path` as a live stream.

        The worker reads the file's header row and then blocks until the UI
        provides a column mapping via the worker's ``set_column_mapping()``
        method.  ``file_headers_detected`` is forwarded to the UI so it can
        show the ColumnMappingDialog before unblocking the worker.
        """
        device_path = file_device_path(file_path)
        worker = LiveFileWorker(file_path, parent=self)
        # Connect the file-specific signal BEFORE _register_worker starts the
        # thread so the queued delivery can never outrace the connection.
        worker.headers_detected.connect(
            lambda path, cols: self.file_headers_detected.emit(path, cols)
        )
        return self._register_worker(device_path, worker, transport="file")

    def _register_worker(self, device_path: str, streamer: "IngestionWorker",
                         transport: str) -> DeviceSession:
        """Shared registration: clock injection, dict insert, signal wiring.

        Idempotent: returns the existing session if one already exists for
        this device_path.
        """
        # Set t=0 BEFORE the worker starts so its first frame is already
        # measured against the session clock.
        self.start_session()
        streamer.set_session_clock(self.session_clock_now)

        with self._lock:
            existing = self._devices.get(device_path)
            if existing is not None:
                return existing                  # safe re-add: same session
            color_idx = self._next_color_idx
            self._next_color_idx += 1
            session = DeviceSession(
                device_path=device_path,
                streamer=streamer,
                color_index=color_idx,
                transport=transport,
            )
            self._devices[device_path] = session

        # Wire signals OUTSIDE the lock — slot invocation could otherwise
        # call back into SessionManager and try to acquire the lock again.
        # Qt.DirectConnection on metadata_ready guarantees attach_buffer
        # runs before the worker's emit() returns (no race).
        streamer.metadata_ready.connect(self._on_metadata, Qt.DirectConnection)
        streamer.handshake_failed.connect(self._on_failed)
        streamer.stream_stopped.connect(self._on_stopped)
        streamer.start()
        self.device_added.emit(device_path)
        return session

    def remove_device(self, device_path: str) -> None:
        """Stop ingestion + logging and drop the device from the registry."""
        with self._lock:
            session = self._devices.pop(device_path, None)
        if session is None:
            return

        # Tear down the logger FIRST so any in-flight recording finalises
        # cleanly before we cut off the producer.
        logger = session.logger
        if logger is not None:
            if logger.is_recording():
                logger.stop_recording()
            logger.stop()
            logger.wait(LOGGER_STOP_TIMEOUT_MS)

        session.streamer.set_logging_queue(None)
        session.streamer.abort()
        # Pick a transport-appropriate join timeout.
        timeouts = {
            "usb":  STREAMER_STOP_TIMEOUT_MS,
            "tcp":  TCP_STOP_TIMEOUT_MS,
            "ble":  BLE_STOP_TIMEOUT_MS,
            "can":  CAN_STOP_TIMEOUT_MS,
            "file": FILE_STOP_TIMEOUT_MS,
        }
        session.streamer.wait(timeouts.get(session.transport,
                                           STREAMER_STOP_TIMEOUT_MS))

        self.device_removed.emit(device_path)
        self._maybe_end_session()

    def list_active(self) -> List[DeviceSession]:
        """Snapshot of every device known to the session.  Safe to iterate."""
        with self._lock:
            return list(self._devices.values())

    def ready_devices(self) -> List[DeviceSession]:
        """Devices that have completed handshake (have buffer + metadata)."""
        with self._lock:
            return [s for s in self._devices.values()
                    if s.buffer is not None and s.metadata is not None]

    def get_device(self, device_path: str) -> Optional[DeviceSession]:
        with self._lock:
            return self._devices.get(device_path)

    # ------------------------------------------------------------------ #
    #  Recording                                                           #
    # ------------------------------------------------------------------ #

    def start_recording_all(self, save_paths: Dict[str, str]) -> Dict[str, str]:
        """Begin recording on every ready device that has a save path entry.

        Defensively de-duplicates save paths: if two devices were assigned the
        same file (e.g. by an upstream bug in filename generation), the second
        one gets a ``__N`` suffix appended before the extension. Two device
        loggers writing to the same file produces a corrupt CSV — interleaved
        byte-level writes, mid-row column-count shifts — that's effectively
        unrecoverable, so we refuse to set it up.

        Returns:
            {device_path: actual_save_path} for each device that started.
        """
        import os as _os

        used_paths: set[str] = set()
        actual: Dict[str, str] = {}
        for session in self.ready_devices():
            target = save_paths.get(session.device_path)
            if not target:
                continue
            # If two devices were given identical save paths, suffix the later
            # ones with __2, __3, ... before the extension.
            normalized = _os.path.abspath(target)
            if normalized in used_paths:
                stem, ext = _os.path.splitext(target)
                n = 2
                while _os.path.abspath(f"{stem}__{n}{ext}") in used_paths:
                    n += 1
                target = f"{stem}__{n}{ext}"
                normalized = _os.path.abspath(target)
            used_paths.add(normalized)

            self._ensure_logger(session)
            path = session.logger.start_recording(target)   # type: ignore[union-attr]
            actual[session.device_path] = path
            self.recording_changed.emit(session.device_path, True)
        return actual

    def stop_recording_all(self) -> None:
        for session in self.ready_devices():
            if session.logger is not None and session.logger.is_recording():
                session.logger.stop_recording()
                self.recording_changed.emit(session.device_path, False)

    def is_any_recording(self) -> bool:
        for s in self.list_active():
            if s.logger is not None and s.logger.is_recording():
                return True
        return False

    def _ensure_logger(self, session: DeviceSession) -> None:
        if session.logger is not None or session.metadata is None:
            return

        # The injected PC_Time_s column is always channel 0.  The device's
        # own channels follow in their declared order.
        channels: List[ChannelSpec] = [
            ChannelSpec(0, PC_TIME_COLUMN_NAME, "s", "float64")
        ]
        for c in session.metadata.channels:
            channels.append(ChannelSpec(
                channel_id=c.channel_id,
                name=c.name or f"ch{c.channel_id}",
                unit=c.unit or "",
                type_label=c.label or "",
            ))

        logger = DataLogger(
            channels=channels,
            output_dir="",          # unused; full path comes from start_recording
            fmt="csv",
            parent=self,
        )
        log_q = make_bounded_queue()
        logger.set_queue(log_q)
        session.streamer.set_logging_queue(log_q)

        device_path = session.device_path
        logger.rows_written.connect(
            lambda total, path, p=device_path: self.recording_rows.emit(p, total, path)
        )
        logger.backlog_warning.connect(
            lambda depth, p=device_path: self.recording_backlog.emit(p, depth)
        )
        logger.samples_dropped.connect(
            lambda dropped, p=device_path: self.recording_dropped.emit(p, dropped)
        )
        logger.error.connect(
            lambda msg, p=device_path: self.recording_error.emit(p, msg)
        )

        logger.start()
        session.logger = logger

    # ------------------------------------------------------------------ #
    #  Streamer slot handlers                                              #
    # ------------------------------------------------------------------ #

    @Slot(str, object)
    def _on_metadata(self, device_path: str, metadata: DeviceMetadataV2) -> None:
        """Allocate the ring buffer (with the +1 timestamp column) and
        publish device_ready."""
        publish: Optional[DeviceMetadataV2] = None
        with self._lock:
            session = self._devices.get(device_path)
            if session is None:
                return
            session.metadata = metadata
            session.buffer = MultiChannelRingBuffer(
                BUFFER_CAPACITY, metadata.num_channels + 1
            )
            session.streamer.attach_buffer(session.buffer)
            publish = metadata
        if publish is not None:
            self.device_ready.emit(device_path, publish)

    @Slot(str, str)
    def _on_failed(self, device_path: str, error: str) -> None:
        with self._lock:
            self._devices.pop(device_path, None)
        self.device_failed.emit(device_path, error)
        self._maybe_end_session()

    @Slot(str, str)
    def _on_stopped(self, device_path: str, reason: str) -> None:
        # Stream ended (user disconnect, IO error, etc.).  If a recording
        # was in flight, finalise it cleanly.  The DEVICE row stays in the
        # registry until remove_device is called explicitly — that's the
        # MainWindow's job in response to disconnect / port_removed.
        with self._lock:
            session = self._devices.get(device_path)
        if session is None:
            return
        if session.logger is not None and session.logger.is_recording():
            session.logger.stop_recording()
            self.recording_changed.emit(device_path, False)
