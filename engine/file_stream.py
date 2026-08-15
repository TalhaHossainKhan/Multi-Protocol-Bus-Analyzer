"""Live-file ingestion worker.

Treats an actively-appending CSV/TSV or Parquet file as a live hardware
stream.  Conforms to the IngestionWorker contract (same signals + methods
as TelemetryStreamer, TcpStreamer, BleStreamer …) so SessionManager can
host it with zero special-casing.

Flow
----
1.  ``run()`` opens the file and reads *only* the header row, then pauses.
2.  ``headers_detected(device_path, col_names)`` fires → UI shows the
    ColumnMappingDialog.
3.  Worker blocks on ``_mapping_event`` (up to HEADER_WAIT_TIMEOUT_S).
4.  UI calls ``set_column_mapping(selected_cols)`` to unblock the worker.
5.  Worker synthesises a ``DeviceMetadataV2`` from the chosen columns and
    emits ``metadata_ready``.  SessionManager's slot (connected with
    Qt.DirectConnection) allocates the ring buffer synchronously before
    emit() returns — the same race-free contract used by all binary workers.
6.  Worker enters the tail loop: every POLL_INTERVAL_S it checks for new
    bytes / row groups and pushes samples into the ring buffer and the
    optional logging queue.

File-format detection
---------------------
``.parquet`` / ``.pq`` extension → pyarrow row-group tail.
Everything else               → UTF-8 text CSV/TSV tail (delimiter
                                auto-detected via csv.Sniffer).

OS file-locking (CRITICAL)
--------------------------
All file reads are wrapped in try/except.  PermissionError / IOError cause
a RETRY_SLEEP_S back-off and a retry rather than crashing the thread.
Partial lines (no trailing ``\\n``) are accumulated in a ``leftover`` buffer
and not parsed until a complete line arrives.
"""

from __future__ import annotations

import csv as _csv_mod
import hashlib
import io
import os
import queue
import threading
import time
from typing import Callable, List, Optional

from PySide6.QtCore import QThread, Signal

from .buffer import MultiChannelRingBuffer
from .protocol import ChannelSpecV2, DeviceMetadataV2

# ── Tuning ─────────────────────────────────────────────────────────────────
FILE_STOP_TIMEOUT_MS  = 2_000
HEADER_WAIT_TIMEOUT_S = 120.0   # max wait for the user's column selection
POLL_INTERVAL_S       = 0.05    # tail-loop tick rate (20 Hz)
RETRY_SLEEP_S         = 0.01    # back-off on PermissionError / IOError

# ── Type constants for synthesised float64 channels ────────────────────────
_FLOAT64_CODE  = 0x08
_FLOAT64_FMT   = "d"
_FLOAT64_BYTES = 8
_FLOAT64_LABEL = "float64"

# ── Parquet availability check ──────────────────────────────────────────────
try:
    import pyarrow.parquet as _pq   # type: ignore
    _PARQUET_OK = True
except ImportError:
    _pq = None                      # type: ignore
    _PARQUET_OK = False


# ── Public helper ──────────────────────────────────────────────────────────

def file_device_path(file_path: str) -> str:
    """Return the canonical device-path key for a live-file source."""
    return f"file://{os.path.abspath(file_path)}"


# ── Worker ─────────────────────────────────────────────────────────────────

class LiveFileWorker(QThread):
    """QThread that tails a CSV/TSV or Parquet file as a live data stream.

    Exposes the same signal / method surface as TelemetryStreamer so that
    SessionManager._register_worker() accepts it without modification.

    Extra signal
    ------------
    ``headers_detected(device_path, col_names)`` — fired after the header
    row is read and *before* ``metadata_ready``.  The UI must respond by
    calling ``set_column_mapping(selected_cols)`` to unblock the worker.
    If the user cancels, call ``abort()`` instead.
    """

    # ── Standard IngestionWorker signals ──────────────────────────────────
    metadata_ready   = Signal(str, object)   # device_path, DeviceMetadataV2
    handshake_failed = Signal(str, str)      # device_path, error_message
    stream_stopped   = Signal(str, str)      # device_path, reason

    # ── File-specific signal ───────────────────────────────────────────────
    headers_detected = Signal(str, list)     # device_path, [col_name, …]

    def __init__(self, file_path: str, parent=None) -> None:
        super().__init__(parent)
        self._file_path   = os.path.abspath(file_path)
        self._device_path = file_device_path(file_path)
        self._running     = False

        # Injected by SessionManager before start().
        self._session_clock: Optional[Callable[[], float]] = None
        self._external_buffer: Optional[MultiChannelRingBuffer] = None
        self._buffer: Optional[MultiChannelRingBuffer] = None
        self._logging_queue: Optional[queue.Queue] = None
        self._producer_dropped = 0

        # Set by UI via set_column_mapping() after headers_detected fires.
        self._col_names: List[str] = []
        self._mapping_event = threading.Event()

        # CSV delimiter discovered during header read; default to comma.
        self._delimiter = ","

    # ── IngestionWorker contract ───────────────────────────────────────────

    def set_session_clock(self, clock_fn: Optional[Callable[[], float]]) -> None:
        self._session_clock = clock_fn

    def attach_buffer(self, buffer: MultiChannelRingBuffer) -> None:
        self._external_buffer = buffer
        self._buffer = buffer

    def set_logging_queue(self, q: Optional[queue.Queue]) -> None:
        self._logging_queue = q

    def abort(self) -> None:
        """Stop the worker and unblock any pending header-wait."""
        self._running = False
        self._mapping_event.set()

    @property
    def producer_dropped(self) -> int:
        return self._producer_dropped

    # ── File-worker–specific API (called from the UI / main thread) ────────

    def set_column_mapping(self, col_names: List[str]) -> None:
        """Unblock run() after the user confirms column selection.

        Must be called while the worker is blocked in run() waiting for
        _mapping_event.  Safe to call from any thread.
        """
        self._col_names = list(col_names)
        self._mapping_event.set()

    # ── QThread entry point ────────────────────────────────────────────────

    def run(self) -> None:
        ext = os.path.splitext(self._file_path)[1].lower()
        is_parquet = ext in (".parquet", ".pq") and _PARQUET_OK

        # ── 1. Read the header row ────────────────────────────────────────
        try:
            if is_parquet:
                all_headers = self._read_headers_parquet()
            else:
                all_headers, self._delimiter = self._read_headers_csv()
        except Exception as exc:
            self.handshake_failed.emit(
                self._device_path, f"Cannot read file headers: {exc}"
            )
            return

        if not all_headers:
            self.handshake_failed.emit(
                self._device_path, "File has no header row or is empty."
            )
            return

        # ── 2. Ask the UI for column selection ────────────────────────────
        # Emitting from a worker thread → queued delivery to main thread.
        # The worker blocks on _mapping_event while the UI shows the modal.
        self.headers_detected.emit(self._device_path, list(all_headers))
        self._mapping_event.wait(timeout=HEADER_WAIT_TIMEOUT_S)

        if not self._col_names:
            self.handshake_failed.emit(
                self._device_path,
                "No columns selected (dialog timed out or was cancelled).",
            )
            return

        # ── 3. Synthesise DeviceMetadataV2 and emit metadata_ready ────────
        # metadata_ready is connected with Qt.DirectConnection in SessionManager
        # so attach_buffer() fires synchronously inside emit() before we continue.
        metadata = self._build_metadata(self._col_names)
        self.metadata_ready.emit(self._device_path, metadata)

        # ── 4. Use SessionManager-allocated buffer or fall back locally ───
        if self._external_buffer is not None:
            self._buffer = self._external_buffer
        else:
            from .telemetry_stream import BUFFER_CAPACITY
            self._buffer = MultiChannelRingBuffer(
                BUFFER_CAPACITY, len(self._col_names) + 1
            )

        # ── 5. Tail the file ──────────────────────────────────────────────
        self._running = True
        try:
            if is_parquet:
                reason = self._tail_parquet()
            else:
                reason = self._tail_csv(all_headers)
        except Exception as exc:
            reason = f"io_error: {exc}"

        self.stream_stopped.emit(self._device_path, reason)

    # ── Header readers ─────────────────────────────────────────────────────

    def _read_headers_csv(self):
        """Return ``(header_list, delimiter)``."""
        with open(self._file_path, "r", newline="", encoding="utf-8-sig") as f:
            sample = f.read(8192)

        try:
            dialect = _csv_mod.Sniffer().sniff(sample, delimiters=",\t|;")
            delim = dialect.delimiter
        except _csv_mod.Error:
            delim = ","

        reader = _csv_mod.reader(io.StringIO(sample), delimiter=delim)
        raw = next(reader, [])
        headers = [h.strip() for h in raw if h.strip()]
        return headers, delim

    def _read_headers_parquet(self) -> List[str]:
        assert _pq is not None
        pf = _pq.ParquetFile(self._file_path)
        return list(pf.schema_arrow.names)

    # ── Metadata synthesis ─────────────────────────────────────────────────

    def _build_metadata(self, col_names: List[str]) -> DeviceMetadataV2:
        uid = hashlib.sha256(self._file_path.encode()).hexdigest()[:32]
        name = os.path.basename(self._file_path)[:32]
        channels = tuple(
            ChannelSpecV2(
                channel_id=i,
                name=col[:16],
                unit="",
                type_code=_FLOAT64_CODE,
                struct_fmt=_FLOAT64_FMT,
                size_bytes=_FLOAT64_BYTES,
                label=_FLOAT64_LABEL,
                range_min=float("nan"),
                range_max=float("nan"),
            )
            for i, col in enumerate(col_names)
        )
        return DeviceMetadataV2(
            device_uid=uid,
            device_name=name,
            firmware_version="file",
            channels=channels,
        )

    # ── CSV / TSV tail loop ────────────────────────────────────────────────

    def _tail_csv(self, all_headers: List[str]) -> str:
        """Tail a CSV/TSV file, pushing newly appended rows into the buffer."""
        # Map selected column names → their index positions in the full header.
        header_index = {h: i for i, h in enumerate(all_headers)}
        lower_map = {h.lower(): i for i, h in enumerate(all_headers)}
        col_indices: List[int] = []
        for name in self._col_names:
            if name in header_index:
                col_indices.append(header_index[name])
            else:
                col_indices.append(lower_map.get(name.lower(), 0))

        # Seek past the header row to record the start-of-data byte offset.
        try:
            with open(self._file_path, "rb") as f:
                f.readline()                 # skip header
                eof_pos = f.tell()
        except Exception as exc:
            return f"io_error: {exc}"

        leftover = b""
        buf = self._buffer
        push = buf.push if buf is not None else (lambda _: None)
        clock = self._session_clock
        delim = self._delimiter

        while self._running:
            time.sleep(POLL_INTERVAL_S)

            try:
                cur_size = os.path.getsize(self._file_path)
            except OSError:
                continue

            if cur_size <= eof_pos:
                continue

            # Read new bytes with OS-locking retry.
            raw = b""
            for _ in range(5):
                try:
                    with open(self._file_path, "rb") as f:
                        f.seek(eof_pos)
                        raw = f.read()
                    eof_pos += len(raw)
                    break
                except (PermissionError, IOError):
                    time.sleep(RETRY_SLEEP_S)

            if not raw:
                continue

            # Accumulate with any incomplete line fragment from last tick.
            raw = leftover + raw

            # Hold back the last chunk if it has no trailing newline —
            # the write is still in progress.
            if raw.endswith(b"\n"):
                leftover = b""
                lines = raw.split(b"\n")
            else:
                parts = raw.split(b"\n")
                leftover = parts[-1]
                lines = parts[:-1]

            for raw_line in lines:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    text = line.decode("utf-8", errors="replace")
                    row_parts = next(
                        _csv_mod.reader([text], delimiter=delim)
                    )
                    sample = tuple(
                        float(row_parts[idx])
                        for idx in col_indices
                        if idx < len(row_parts)
                    )
                    if len(sample) != len(col_indices):
                        continue   # incomplete row — skip

                    pc_ts = clock() if clock else 0.0
                    augmented = (pc_ts, *sample)
                    push(augmented)

                    log_q = self._logging_queue
                    if log_q is not None:
                        try:
                            log_q.put_nowait(augmented)
                        except queue.Full:
                            self._producer_dropped += 1

                except (ValueError, IndexError, StopIteration,
                        UnicodeDecodeError):
                    continue   # skip malformed / partially-written lines

        return "user_stop"

    # ── Parquet tail loop ──────────────────────────────────────────────────

    def _tail_parquet(self) -> str:
        """Tail a Parquet file by watching for newly written row groups."""
        assert _pq is not None and self._buffer is not None

        push = self._buffer.push
        clock = self._session_clock

        try:
            pf = _pq.ParquetFile(self._file_path)
            known_groups = pf.num_row_groups
        except Exception as exc:
            return f"io_error: {exc}"

        while self._running:
            time.sleep(POLL_INTERVAL_S)

            try:
                pf2 = _pq.ParquetFile(self._file_path)
                new_groups = pf2.num_row_groups
            except (PermissionError, IOError):
                time.sleep(RETRY_SLEEP_S)
                continue
            except Exception:
                continue

            if new_groups <= known_groups:
                continue

            for grp_idx in range(known_groups, new_groups):
                try:
                    table = pf2.read_row_group(grp_idx, columns=self._col_names)
                except Exception:
                    continue

                for row_idx in range(table.num_rows):
                    try:
                        sample = tuple(
                            float(table.column(col)[row_idx].as_py())
                            for col in self._col_names
                        )
                        pc_ts = clock() if clock else 0.0
                        augmented = (pc_ts, *sample)
                        push(augmented)

                        log_q = self._logging_queue
                        if log_q is not None:
                            try:
                                log_q.put_nowait(augmented)
                            except queue.Full:
                                self._producer_dropped += 1
                    except Exception:
                        continue

            known_groups = new_groups

        return "user_stop"
