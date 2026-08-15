"""BLE telemetry streamer — plain-text auto-detect.

Any MCU that sends lines of text over BLE GATT notifications works
automatically. No custom DAQ firmware or protocol is required.

UUIDs — Nordic UART Service (NUS), universally supported:
  Service  : 6e400001-b5a3-f393-e0a9-e50e24dcca9e
  TX/notify: 6e400003-b5a3-f393-e0a9-e50e24dcca9e  (peripheral → host)
  RX/write : 6e400002-b5a3-f393-e0a9-e50e24dcca9e  (host → peripheral)

The MCU sends newline-terminated text over the TX characteristic.
The streamer accumulates bytes, detects format from the first few complete
lines, synthesises DeviceMetadataV2, emits metadata_ready, then drains
each notification into the ring buffer.

Threading model: bleak is asyncio-based; we run a dedicated asyncio event
loop inside this QThread. All public methods remain synchronous.
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time
from typing import Callable, List, Optional

from PySide6.QtCore import QThread, Signal

try:
    from bleak import BleakClient, BleakScanner
    _BLEAK_AVAILABLE = True
except ImportError:
    BleakClient = None      # type: ignore
    BleakScanner = None     # type: ignore
    _BLEAK_AVAILABLE = False

from .buffer import MultiChannelRingBuffer
from .line_parser import build_synthetic_metadata, detect_format, parse_line
from .protocol import DeviceMetadataV2


# Nordic UART Service UUIDs.
NUS_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_TX_UUID      = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX_UUID      = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"

BLE_CONNECT_TIMEOUT_S  = 10.0
BLE_HANDSHAKE_TIMEOUT_S = 8.0
BUFFER_CAPACITY = 50_000
BLE_STOP_TIMEOUT_MS = 2_000

# Minimum accumulated bytes before we attempt format detection.
_SNIFF_MIN_LINES = 2


def ble_device_path(address: str) -> str:
    return f"ble://{address}"


class BleStreamer(QThread):
    """Ingestion worker for BLE MCUs outputting plain-text GATT notifications."""

    metadata_ready   = Signal(str, object)  # device_path, DeviceMetadataV2
    handshake_failed = Signal(str, str)     # device_path, error_msg
    stream_stopped   = Signal(str, str)     # device_path, reason

    def __init__(
        self,
        address: str,
        notify_uuid: str = NUS_TX_UUID,
        write_uuid: str  = NUS_RX_UUID,
        parent=None,
    ) -> None:
        super().__init__(parent)
        if not _BLEAK_AVAILABLE:
            raise RuntimeError("bleak is not installed — `pip install bleak`")

        self._address     = address
        self._notify_uuid = notify_uuid
        self._write_uuid  = write_uuid
        self._device_path = ble_device_path(address)

        # Asyncio bookkeeping.
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_async_evt: Optional[asyncio.Event] = None
        self._handshake_evt = threading.Event()
        self._stop_evt      = threading.Event()

        # Parse state — protected by _parse_lock (notifications arrive on the
        # asyncio thread; stop/attach_buffer come from the UI thread).
        self._parse_lock = threading.Lock()
        self._accum = bytearray()
        self._metadata: Optional[DeviceMetadataV2] = None

        # Set once text format is detected.
        self._text_mode    = False
        self._text_fmt     = ""
        self._text_ch_names: List[str] = []

        self._buffer:          Optional[MultiChannelRingBuffer] = None
        self._external_buffer: Optional[MultiChannelRingBuffer] = None
        self._logging_queue:   Optional[queue.Queue] = None
        self._producer_dropped = 0
        self._session_clock:   Optional[Callable[[], float]] = None

    # ── public accessors ──────────────────────────────────────────────────

    @property
    def device_path(self) -> str:
        return self._device_path

    @property
    def address(self) -> str:
        return self._address

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

    # ── stop ─────────────────────────────────────────────────────────────

    def stop(self) -> None:
        self._stop_evt.set()
        loop, evt = self._loop, self._stop_async_evt
        if loop is not None and evt is not None and loop.is_running():
            loop.call_soon_threadsafe(evt.set)

    def abort(self) -> None:
        self.stop()

    # ── QThread entry ─────────────────────────────────────────────────────

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            loop.run_until_complete(self._main_async())
        except Exception as exc:
            self.handshake_failed.emit(
                self._device_path, f"BLE loop crashed: {exc}"
            )
        finally:
            try:
                loop.close()
            except Exception:
                pass
            self._loop = None

    async def _main_async(self) -> None:
        self._stop_async_evt = asyncio.Event()
        try:
            async with BleakClient(
                self._address, timeout=BLE_CONNECT_TIMEOUT_S
            ) as client:
                if not client.is_connected:
                    self.handshake_failed.emit(
                        self._device_path, "GATT connect failed"
                    )
                    return

                try:
                    await client.start_notify(self._notify_uuid, self._on_notify)
                except Exception as exc:
                    self.handshake_failed.emit(
                        self._device_path, f"start_notify failed: {exc}"
                    )
                    return

                # Wait for text format to be detected from incoming notifications.
                meta_received = await self._wait_for_handshake()
                if not meta_received:
                    self.handshake_failed.emit(
                        self._device_path,
                        f"No data received within {BLE_HANDSHAKE_TIMEOUT_S:.0f}s.\n"
                        "Check that the MCU is sending newline-terminated text.",
                    )
                    return

                self.metadata_ready.emit(self._device_path, self._metadata)
                if self._external_buffer is None:
                    self._buffer = MultiChannelRingBuffer(
                        BUFFER_CAPACITY, self._metadata.num_channels
                    )

                await self._stop_async_evt.wait()

                try:
                    await client.stop_notify(self._notify_uuid)
                except Exception:
                    pass

            self.stream_stopped.emit(self._device_path, "user_stop")
        except Exception as exc:
            self.handshake_failed.emit(self._device_path, str(exc))

    async def _wait_for_handshake(self) -> bool:
        deadline = time.monotonic() + BLE_HANDSHAKE_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._handshake_evt.is_set():
                return True
            if self._stop_evt.is_set():
                return False
            await asyncio.sleep(0.05)
        return self._handshake_evt.is_set()

    # ── notification callback ─────────────────────────────────────────────

    def _on_notify(self, sender, data: bytearray) -> None:
        with self._parse_lock:
            self._accum.extend(data)
            if self._metadata is None:
                self._try_detect_text()
            elif self._text_mode:
                self._drain_text()

    def _try_detect_text(self) -> None:
        """Try to detect the text format from accumulated bytes.

        Called under _parse_lock while metadata is still unknown.
        Waits until at least _SNIFF_MIN_LINES complete lines are present.
        """
        text = self._accum.decode("utf-8", errors="replace")
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if len(lines) < _SNIFF_MIN_LINES:
            return

        fmt, ch_names = detect_format(lines)
        if fmt == "unknown":
            return

        self._text_mode     = True
        self._text_fmt      = fmt
        self._text_ch_names = ch_names
        self._metadata = build_synthetic_metadata(
            self._device_path, ch_names, fmt
        )

        # Keep only the bytes after the last complete newline — they belong
        # to the next (possibly partial) line.
        last_nl = self._accum.rfind(b"\n")
        if last_nl >= 0:
            self._accum = self._accum[last_nl + 1:]
        else:
            self._accum = bytearray()

        self._handshake_evt.set()

    def _drain_text(self) -> None:
        """Parse all complete lines in _accum and push samples.

        Called under _parse_lock while in text streaming mode.
        """
        push = self._buffer.push if self._buffer else None
        if push is None:
            return
        clock = self._session_clock
        prepend_ts = (
            clock is not None
            and self._buffer.num_channels == len(self._text_ch_names) + 1
        )

        while b"\n" in self._accum:
            raw, self._accum = self._accum.split(b"\n", 1)
            line = raw.decode("utf-8", errors="replace").strip()
            values = parse_line(line, self._text_fmt, self._text_ch_names)
            if values is None or len(values) != len(self._text_ch_names):
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

    # ── BLE scan helper ───────────────────────────────────────────────────

    @staticmethod
    async def scan(timeout: float = 5.0) -> list:
        """Return [(address, name, rssi), ...] for nearby BLE peripherals."""
        if not _BLEAK_AVAILABLE:
            return []
        devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
        return [
            (addr, dev.name or "(unknown)", adv.rssi)
            for addr, (dev, adv) in devices.items()
        ]
