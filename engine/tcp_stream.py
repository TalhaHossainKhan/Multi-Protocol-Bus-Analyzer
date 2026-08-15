"""TCP/WiFi telemetry streamer — plain-text auto-detect.

Any MCU that sends lines of text over a TCP socket works automatically.
No custom DAQ firmware or protocol is required.

Topology:
    MCU (ESP32) — WiFi TCP server, port 7777 ──◀── TCP client (this worker)

The desktop is the CLIENT. On start() we connect to (host, port), read the
first few lines of text, detect the format, synthesise DeviceMetadataV2,
emit metadata_ready, then stream samples until stopped.

Supported MCU output formats:
  JSON      {"voltage": 3.14, "current": 1.2}
  Labeled   Voltage:3.14\\tCurrent:1.2   (key:value or key=value)
  CSV       3.14,1.2,25.3  (optional header line for channel names)
"""

from __future__ import annotations

import queue
import socket
import time
from typing import Callable, List, Optional

from PySide6.QtCore import QThread, Signal

from .buffer import MultiChannelRingBuffer
from .line_parser import build_synthetic_metadata, detect_format, parse_line
from .protocol import DeviceMetadataV2


# ---------------------------------------------------------------------------
# Tuning
# ---------------------------------------------------------------------------
DEFAULT_TCP_PORT = 7777
TCP_CONNECT_TIMEOUT_S = 4.0
TCP_READ_TIMEOUT_S = 0.2
READ_CHUNK_BYTES = 4096
BUFFER_CAPACITY = 50_000
SNIFF_TIMEOUT_S = 5.0
SNIFF_LINES = 5
TCP_STOP_TIMEOUT_MS = 800


def tcp_device_path(host: str, port: int) -> str:
    return f"tcp://{host}:{port}"


class TcpStreamer(QThread):
    """Ingestion worker for WiFi/TCP MCUs outputting plain text."""

    metadata_ready   = Signal(str, object)  # device_path, DeviceMetadataV2
    handshake_failed = Signal(str, str)     # device_path, error_msg
    stream_stopped   = Signal(str, str)     # device_path, reason

    def __init__(
        self, host: str, port: int = DEFAULT_TCP_PORT, parent=None
    ) -> None:
        super().__init__(parent)
        self._host = host
        self._port = int(port)
        self._device_path = tcp_device_path(host, self._port)
        self._sock: Optional[socket.socket] = None
        self._running = False

        self._buffer: Optional[MultiChannelRingBuffer] = None
        self._external_buffer: Optional[MultiChannelRingBuffer] = None
        self._metadata: Optional[DeviceMetadataV2] = None
        self._logging_queue: Optional[queue.Queue] = None
        self._producer_dropped = 0
        self._session_clock: Optional[Callable[[], float]] = None

        self._fmt: str = ""
        self._ch_names: List[str] = []
        self._leftover: bytes = b""

    # ── public accessors ──────────────────────────────────────────────────

    @property
    def device_path(self) -> str:
        return self._device_path

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

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
        sock = self._sock
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    # ── thread entry ──────────────────────────────────────────────────────

    def run(self) -> None:
        # 1. Connect.
        try:
            self._sock = socket.create_connection(
                (self._host, self._port), timeout=TCP_CONNECT_TIMEOUT_S
            )
            self._sock.settimeout(TCP_READ_TIMEOUT_S)
        except (OSError, socket.timeout) as exc:
            self.handshake_failed.emit(
                self._device_path, f"connect failed: {exc}"
            )
            return

        # 2. Sniff format from first few lines.
        try:
            self._metadata = self._sniff_metadata()
        except Exception as exc:
            self.handshake_failed.emit(self._device_path, str(exc))
            self._safe_close()
            return

        # 3. Announce metadata.
        self.metadata_ready.emit(self._device_path, self._metadata)
        if self._external_buffer is None:
            self._buffer = MultiChannelRingBuffer(
                BUFFER_CAPACITY, self._metadata.num_channels
            )

        # 4. Stream until stopped.
        self._running = True
        reason = self._text_parse_loop()
        self._safe_close()
        self.stream_stopped.emit(self._device_path, reason)

    # ── format sniff ──────────────────────────────────────────────────────

    def _sniff_metadata(self) -> DeviceMetadataV2:
        assert self._sock is not None
        lines: List[str] = []
        raw = bytearray()
        deadline = time.monotonic() + SNIFF_TIMEOUT_S

        while time.monotonic() < deadline and len(lines) < SNIFF_LINES:
            try:
                chunk = self._sock.recv(READ_CHUNK_BYTES)
            except socket.timeout:
                chunk = b""
            except OSError as exc:
                raise ConnectionError(f"socket error: {exc}") from exc

            if chunk == b"":
                raise ConnectionError("peer closed connection before sending data")
            if chunk:
                raw.extend(chunk)

            while b"\n" in raw:
                line_b, raw = raw.split(b"\n", 1)
                line = line_b.decode("utf-8", errors="replace").strip()
                if line:
                    lines.append(line)

        fmt, ch_names = detect_format(lines)
        if fmt == "unknown":
            sample = "\n  ".join(lines[:3]) if lines else "(no data received)"
            raise ValueError(
                "Could not detect data format.\n"
                "Supported: JSON, key:value, CSV.\n"
                f"Received:\n  {sample}"
            )

        self._fmt = fmt
        self._ch_names = ch_names
        self._leftover = bytes(raw)
        return build_synthetic_metadata(self._device_path, ch_names, fmt)

    # ── streaming loop ────────────────────────────────────────────────────

    def _text_parse_loop(self) -> str:
        assert self._sock is not None
        assert self._buffer is not None

        buf = bytearray(self._leftover)
        push = self._buffer.push
        clock = self._session_clock
        prepend_ts = (
            clock is not None
            and self._buffer.num_channels == len(self._ch_names) + 1
        )

        while self._running:
            try:
                chunk = self._sock.recv(READ_CHUNK_BYTES)
            except socket.timeout:
                continue
            except OSError as exc:
                return f"io_error: {exc}"

            if chunk == b"":
                return "peer_closed"
            buf.extend(chunk)

            while b"\n" in buf:
                raw_line, buf = buf.split(b"\n", 1)
                line = raw_line.decode("utf-8", errors="replace").strip()
                values = parse_line(line, self._fmt, self._ch_names)
                if values is None or len(values) != len(self._ch_names):
                    continue
                sample = (clock(), *values) if prepend_ts else tuple(values)
                try:
                    push(sample)
                except ValueError:
                    continue
                log_q = self._logging_queue
                if log_q is not None:
                    try:
                        log_q.put_nowait(sample)
                    except queue.Full:
                        self._producer_dropped += 1

        return "user_stop"

    # ── cleanup ───────────────────────────────────────────────────────────

    def _safe_close(self) -> None:
        if self._sock is None:
            return
        try:
            self._sock.close()
        except OSError:
            pass
        self._sock = None
