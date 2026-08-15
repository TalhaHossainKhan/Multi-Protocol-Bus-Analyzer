"""Per-device telemetry streamer — plain-text auto-detect.

Any MCU that prints lines of text to the serial port works automatically.
No custom DAQ firmware or protocol is required.

Supported MCU output formats (auto-detected from the first few lines):
  JSON      {"voltage": 3.14, "current": 1.2}
  Labeled   Voltage:3.14  Current:1.2   (key:value or key=value, including
            Arduino Serial Plotter tab-separated style)
  CSV       3.14,1.2,25.3  (optional header line for channel names)

The streamer reads the first SNIFF_LINES complete lines, infers the format
and channel names, synthesises a DeviceMetadataV2, emits metadata_ready,
and then streams samples into the ring buffer at whatever rate the MCU sends.

All blocking I/O happens on this thread. The UI never touches the serial
port or parser state — it only receives signals and reads the ring buffer.
"""

from __future__ import annotations

import queue
import time
from typing import Callable, List, Optional

import serial
from PySide6.QtCore import QThread, Signal

from .buffer import MultiChannelRingBuffer
from .line_parser import build_synthetic_metadata, detect_format, parse_line
from .protocol import DeviceMetadataV2


# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------
DEFAULT_BAUD = 115_200
BUFFER_CAPACITY = 50_000
SERIAL_READ_TIMEOUT_S = 0.1
READ_CHUNK_BYTES = 1024
SNIFF_TIMEOUT_S = 4.0       # max seconds to wait for the first few lines
SNIFF_LINES = 5             # number of complete lines needed for detection
STREAMER_STOP_TIMEOUT_MS = 800


class TelemetryStreamer(QThread):
    """Ingestion worker for USB/serial MCUs outputting plain text."""

    metadata_ready   = Signal(str, object)  # device_path, DeviceMetadataV2
    handshake_failed = Signal(str, str)     # device_path, error_msg
    stream_stopped   = Signal(str, str)     # device_path, reason

    def __init__(
        self, device_path: str, baud: int = DEFAULT_BAUD, parent=None
    ) -> None:
        super().__init__(parent)
        self._device_path = device_path
        self._baud = baud
        self._running = False
        self._ser: Optional[serial.Serial] = None
        self._buffer: Optional[MultiChannelRingBuffer] = None
        self._metadata: Optional[DeviceMetadataV2] = None
        self._logging_queue: Optional[queue.Queue] = None
        self._producer_dropped = 0
        self._session_clock: Optional[Callable[[], float]] = None
        self._external_buffer: Optional[MultiChannelRingBuffer] = None

        # Set during _sniff_metadata(); used by _text_parse_loop().
        self._fmt: str = ""
        self._ch_names: List[str] = []
        self._leftover: bytes = b""

    # ── public accessors ──────────────────────────────────────────────────

    @property
    def device_path(self) -> str:
        return self._device_path

    @property
    def baud(self) -> int:
        return self._baud

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
        ser = self._ser
        if ser is None:
            return
        try:
            ser.cancel_read()
        except Exception:
            pass
        try:
            ser.close()
        except Exception:
            pass

    # ── thread entry ──────────────────────────────────────────────────────

    def run(self) -> None:
        # 1. Open the serial port.
        try:
            self._ser = serial.Serial(
                self._device_path,
                baudrate=self._baud,
                timeout=SERIAL_READ_TIMEOUT_S,
                write_timeout=1.0,
            )
        except (serial.SerialException, OSError) as exc:
            self.handshake_failed.emit(self._device_path, f"open failed: {exc}")
            return

        # 2. Sniff the first few lines to detect format and channel names.
        try:
            self._metadata = self._sniff_metadata()
        except Exception as exc:
            self.handshake_failed.emit(self._device_path, str(exc))
            self._safe_close()
            return

        # 3. Announce metadata so SessionManager can allocate the buffer.
        self.metadata_ready.emit(self._device_path, self._metadata)
        if self._external_buffer is not None:
            self._buffer = self._external_buffer
        else:
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
        """Read the first SNIFF_LINES complete lines and detect format."""
        assert self._ser is not None
        lines: List[str] = []
        raw = bytearray()
        deadline = time.monotonic() + SNIFF_TIMEOUT_S

        while time.monotonic() < deadline and len(lines) < SNIFF_LINES:
            try:
                chunk = self._ser.read(READ_CHUNK_BYTES)
            except (serial.SerialException, OSError) as exc:
                raise OSError(f"serial read failed: {exc}") from exc
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
                f"Received:\n  {sample}\n\n"
                "Make sure the MCU is sending data at the selected baud rate."
            )

        self._fmt = fmt
        self._ch_names = ch_names
        self._leftover = bytes(raw)
        return build_synthetic_metadata(self._device_path, ch_names, fmt)

    # ── streaming loop ────────────────────────────────────────────────────

    def _text_parse_loop(self) -> str:
        assert self._ser is not None
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
                chunk = self._ser.read(READ_CHUNK_BYTES)
            except (serial.SerialException, OSError) as exc:
                return f"io_error: {exc}"

            if chunk:
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
        if self._ser is None:
            return
        try:
            self._ser.close()
        except Exception:
            pass
        self._ser = None
