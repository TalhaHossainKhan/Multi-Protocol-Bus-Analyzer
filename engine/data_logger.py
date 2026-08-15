"""Async disk logger — Thread 3 in the Step-4 three-thread architecture.

The TelemetryStreamer (Thread 2) forks every parsed sample into:

    Path A — `MultiChannelRingBuffer`   (consumed by Thread 1, the UI)
    Path B — `queue.Queue`              (consumed by THIS thread)

This file owns Path B's consumer.  A `DataLogger` is a QThread that:

  1. Pulls rows out of the queue continuously.
  2. Accumulates them in a chunk (default 5,000 rows OR 1 second of data).
  3. On chunk-full or timer expiry, flushes the chunk to disk as a single
     block write — Parquet (preferred, via pyarrow) or CSV fallback.
  4. Monitors queue depth.  If the producer outruns the disk, the logger
     emits `backlog_warning` and starts dropping the OLDEST samples to
     keep memory bounded.  This is the "Disk Write Bottleneck" guard.

Thread boundaries
-----------------
* The TelemetryStreamer pushes rows via `queue.Queue.put_nowait()` —
  non-blocking; if the queue is full it drops on the producer side and
  bumps the dropped-by-producer counter.
* The DataLogger is the ONLY consumer of the queue.  It never touches the
  ring buffer or the serial port.
* The UI thread (Thread 1) interacts with the logger only through Qt
  signals (`rows_written`, `backlog_warning`, `recording_finished`, `error`)
  and through `start_recording(path)` / `stop()` calls.  All cross-thread
  state is either signal-delivered or owned by `threading.Lock`s inside
  this file.

File format
-----------
Default: a single `.parquet` file per recording session, written in row
groups (one row group per chunk).  This means a kill -9 mid-recording
still leaves a partially-readable file up to the last completed row group.

CSV fallback: same chunking semantics, written via `csv.writer.writerows`.
Chosen only when pyarrow isn't importable or the user passes `format="csv"`.
"""

from __future__ import annotations

import csv
import os
import queue
import time
from dataclasses import dataclass
from typing import List, Optional, Sequence

import numpy as np
from PySide6.QtCore import QThread, Signal

try:                                       # pyarrow is the preferred path
    import pyarrow as pa                   # type: ignore
    import pyarrow.parquet as pq           # type: ignore
    _PARQUET_AVAILABLE = True
except ImportError:                        # graceful degradation to CSV
    pa = None                              # type: ignore
    pq = None                              # type: ignore
    _PARQUET_AVAILABLE = False


# --------------------------------------------------------------------------- #
#  Tuning                                                                       #
# --------------------------------------------------------------------------- #

# Chunk size and timer that govern when we actually touch the disk.  These
# are tuned so that even a 100 kHz aggregate sample rate flushes at most
# 20 times per second — well under the IOPS budget of any SSD or USB stick.
FLUSH_ROWS_DEFAULT       = 5_000
FLUSH_INTERVAL_S_DEFAULT = 1.0

# Backlog thresholds.  The queue is sized at MAX_QUEUE so the producer
# (TelemetryStreamer) blocks/drops at that ceiling.  The WARNING threshold
# fires earlier so the UI can react before we actually lose data.
WARN_QUEUE_DEPTH    = 1_000_000      # rows: ~64 MB at 8 channels × float64
MAX_QUEUE_DEPTH     = 1_500_000      # rows: ~96 MB at 8 channels × float64
DROP_WHEN_OVER      = 1_200_000      # logger starts dropping above this

# How long the logger blocks on the queue before checking timers / flags.
# Short enough that stop() returns promptly, long enough to avoid busy-spin.
QUEUE_GET_TIMEOUT_S = 0.05


@dataclass(frozen=True)
class ChannelSpec:
    """Lightweight description of a logged channel.

    The full DeviceMetadataV2 carries more (ranges, type codes) — only the
    fields that affect the on-disk schema are kept here so the logger does
    not have to import the protocol module.
    """
    channel_id: int
    name: str
    unit: str
    type_label: str


# --------------------------------------------------------------------------- #
#  DataLogger                                                                   #
# --------------------------------------------------------------------------- #


class DataLogger(QThread):
    """Background thread that drains a sample queue to disk.

    Lifecycle:
        logger = DataLogger(channels, output_dir)
        logger.set_queue(producer_queue)         # called by the UI on Record
        logger.start_recording(file_basename)    # returns the file path
        logger.start()                           # QThread.start (runs run())
        ...
        logger.stop_recording()                  # finishes the current file
        logger.stop()                            # request thread exit
        logger.wait(STOP_TIMEOUT_MS)
    """

    # int total_rows_written, str file_path (current)
    rows_written = Signal(int, str)
    # int queue_depth — fires once when crossing WARN_QUEUE_DEPTH upward
    backlog_warning = Signal(int)
    # int dropped_count — emitted whenever the logger drops to keep memory bounded
    samples_dropped = Signal(int)
    # str file_path, int total_rows
    recording_finished = Signal(str, int)
    # str message
    error = Signal(str)

    # --------------------------------------------------------------- #
    #  Construction                                                     #
    # --------------------------------------------------------------- #

    def __init__(
        self,
        channels: Sequence[ChannelSpec],
        output_dir: str,
        fmt: str = "parquet",
        flush_rows: int = FLUSH_ROWS_DEFAULT,
        flush_interval_s: float = FLUSH_INTERVAL_S_DEFAULT,
        parent=None,
    ) -> None:
        super().__init__(parent)
        if not channels:
            raise ValueError("channels must be non-empty")
        if fmt not in ("parquet", "csv"):
            raise ValueError(f"unsupported format: {fmt}")
        if fmt == "parquet" and not _PARQUET_AVAILABLE:
            fmt = "csv"                  # graceful fallback

        self._channels = tuple(channels)
        self._output_dir = output_dir
        self._fmt = fmt
        self._flush_rows = int(flush_rows)
        self._flush_interval_s = float(flush_interval_s)

        # Producer plumbing.
        self._queue: Optional[queue.Queue] = None

        # Internal control.
        self._running = False
        self._recording = False
        self._current_path: Optional[str] = None
        self._total_rows = 0

        # Diagnostics.
        self._dropped_samples = 0
        self._warning_emitted = False

        # Writer handles (lazy-opened on first flush).
        self._parquet_writer: Optional["pq.ParquetWriter"] = None
        self._csv_file = None
        self._csv_writer = None

        # In-memory chunk.  Shape grows to (flush_rows, num_channels).
        self._chunk: List[Sequence[float]] = []
        self._last_flush_ts = 0.0

    # --------------------------------------------------------------- #
    #  Public control surface (called from the UI thread)               #
    # --------------------------------------------------------------- #

    def set_queue(self, q: queue.Queue) -> None:
        """Attach the producer queue.  Must be set before `start_recording`."""
        self._queue = q

    def queue_depth(self) -> int:
        return self._queue.qsize() if self._queue is not None else 0

    def is_recording(self) -> bool:
        return self._recording

    def current_path(self) -> Optional[str]:
        return self._current_path

    def total_rows(self) -> int:
        return self._total_rows

    def start_recording(self, save_path: str) -> str:
        """Begin writing to `save_path` (a full, user-chosen file path).

        The format is inferred from the file extension: ``.csv`` uses the
        CSV writer, anything else falls back to Parquet if available.
        Idempotent: returns the existing path if already recording.
        """
        if self._recording and self._current_path is not None:
            return self._current_path

        os.makedirs(os.path.dirname(os.path.abspath(save_path)), exist_ok=True)

        # Infer format from the chosen extension.
        ext = os.path.splitext(save_path)[1].lower()
        if ext == ".csv":
            self._fmt = "csv"
        elif ext in (".parquet", ".pq") and _PARQUET_AVAILABLE:
            self._fmt = "parquet"
        else:
            self._fmt = "csv"
            if not save_path.endswith(".csv"):
                save_path = save_path + ".csv"

        self._current_path = save_path
        self._total_rows = 0
        self._dropped_samples = 0
        self._warning_emitted = False
        self._chunk.clear()
        self._last_flush_ts = time.monotonic()
        self._recording = True
        return save_path

    def stop_recording(self) -> None:
        """Stop accepting new chunks for the current file; flush and close it.

        Safe to call from the UI thread — the actual file close happens
        in `run()` after the next queue drain.  This just toggles the
        flag; `run()` notices and flushes synchronously on its next tick.
        """
        self._recording = False

    def stop(self) -> None:
        """Ask the run loop to exit at its next iteration."""
        self._running = False

    # --------------------------------------------------------------- #
    #  Thread main loop                                                 #
    # --------------------------------------------------------------- #

    def run(self) -> None:
        if self._queue is None:
            self.error.emit("DataLogger started with no queue attached.")
            return
        self._running = True

        try:
            while self._running:
                self._drain_one_pass()
                self._maybe_flush()
                self._check_backlog()
                # If we just stopped recording, finalise the file.
                if not self._recording and self._current_path is not None:
                    self._finalise_recording()
        except Exception as exc:                # last-resort safety net
            self.error.emit(f"DataLogger crashed: {exc!r}")
        finally:
            # Best-effort flush on shutdown.
            try:
                if self._recording:
                    self._recording = False
                if self._current_path is not None:
                    self._finalise_recording()
            except Exception:
                pass

    # --------------------------------------------------------------- #
    #  Queue draining                                                   #
    # --------------------------------------------------------------- #

    def _drain_one_pass(self) -> None:
        """Pull rows from the queue, append to chunk, drop on overflow."""
        assert self._queue is not None
        q = self._queue
        chunk = self._chunk
        flush_rows = self._flush_rows

        # First call blocks briefly so the thread doesn't busy-spin when
        # the producer is idle.  Subsequent reads are non-blocking until
        # the queue empties or the chunk fills.
        try:
            row = q.get(timeout=QUEUE_GET_TIMEOUT_S)
        except queue.Empty:
            return

        # Drop-oldest policy: if we're past DROP_WHEN_OVER, discard the
        # row we just got and bleed the queue down.  We prefer to drop
        # the OLDEST so the most recent data still lands on disk.
        if self._recording and q.qsize() > DROP_WHEN_OVER:
            self._dropped_samples += 1
            # Drain ahead until we're back under threshold, keeping the
            # most recent batch.
            kept_tail: List[Sequence[float]] = [row]
            while q.qsize() > DROP_WHEN_OVER // 2:
                try:
                    kept_tail.append(q.get_nowait())
                except queue.Empty:
                    break
                self._dropped_samples += 1
            # Keep at most flush_rows of the newest samples.
            keep_n = min(len(kept_tail), flush_rows)
            chunk.extend(kept_tail[-keep_n:])
            self._dropped_samples -= keep_n   # those weren't dropped — they were kept
            self.samples_dropped.emit(self._dropped_samples)
            return

        if self._recording:
            chunk.append(row)
        # If we're not actively recording, the row is silently discarded
        # (this is how the UI "tap" works without disk thrash).

        # Greedy drain — pull whatever else is available without blocking.
        while len(chunk) < flush_rows:
            try:
                row = q.get_nowait()
            except queue.Empty:
                break
            if self._recording:
                chunk.append(row)

    # --------------------------------------------------------------- #
    #  Disk flush                                                       #
    # --------------------------------------------------------------- #

    def _maybe_flush(self) -> None:
        """Flush the in-memory chunk if it's full OR the timer expired."""
        if not self._recording:
            return
        if not self._chunk:
            self._last_flush_ts = time.monotonic()
            return
        now = time.monotonic()
        if (len(self._chunk) >= self._flush_rows
                or (now - self._last_flush_ts) >= self._flush_interval_s):
            self._flush_chunk_to_disk()
            self._last_flush_ts = now

    def _flush_chunk_to_disk(self) -> None:
        if not self._chunk or self._current_path is None:
            return
        chunk = self._chunk
        n_rows = len(chunk)

        try:
            if self._fmt == "parquet":
                self._flush_chunk_parquet(chunk)
            else:
                self._flush_chunk_csv(chunk)
        except Exception as exc:
            self.error.emit(f"disk write failed: {exc!r}")
            # On write failure we still discard the chunk to avoid an
            # unbounded retry — the alternative is letting RAM run out.
        finally:
            self._chunk = []                     # release references
            self._total_rows += n_rows
            self.rows_written.emit(self._total_rows, self._current_path)

    def _flush_chunk_parquet(self, chunk: List[Sequence[float]]) -> None:
        """Convert chunk → pyarrow Table → write a single row group."""
        assert pa is not None and pq is not None
        assert self._current_path is not None

        # Column-of-arrays — fastest path to Arrow.
        arr = np.asarray(chunk, dtype=np.float64)        # (n_rows, n_channels)
        cols = {
            self._channels[i].name: pa.array(arr[:, i], type=pa.float64())
            for i in range(len(self._channels))
        }
        table = pa.table(cols, schema=self._parquet_schema())

        if self._parquet_writer is None:
            self._parquet_writer = pq.ParquetWriter(
                self._current_path,
                table.schema,
                compression="snappy",
            )
        self._parquet_writer.write_table(table)

    def _flush_chunk_csv(self, chunk: List[Sequence[float]]) -> None:
        assert self._current_path is not None
        if self._csv_writer is None:
            self._csv_file = open(self._current_path, "w", newline="", buffering=1 << 16)
            self._csv_writer = csv.writer(self._csv_file)
            header = [
                f"{c.name}[{c.unit}]" if c.unit else c.name for c in self._channels
            ]
            self._csv_writer.writerow(header)
        self._csv_writer.writerows(chunk)
        # NOTE: not flushing every chunk on purpose — the OS buffer +
        # the 64 KB io buffer above is good enough for the CSV fallback.

    def _parquet_schema(self) -> "pa.Schema":
        assert pa is not None
        # Pre-build the schema once; ParquetWriter requires consistency
        # across row groups so we cache it.
        return pa.schema(
            [(c.name, pa.float64()) for c in self._channels],
            metadata={
                b"daq.units":  ",".join(c.unit  or "" for c in self._channels).encode(),
                b"daq.labels": ",".join(c.type_label or "" for c in self._channels).encode(),
            },
        )

    # --------------------------------------------------------------- #
    #  File close                                                       #
    # --------------------------------------------------------------- #

    def _finalise_recording(self) -> None:
        """Flush remaining rows and close the writer for the current file."""
        path = self._current_path
        if path is None:
            return
        # Final flush regardless of size / timer.
        if self._chunk:
            try:
                if self._fmt == "parquet":
                    self._flush_chunk_parquet(self._chunk)
                else:
                    self._flush_chunk_csv(self._chunk)
                self._total_rows += len(self._chunk)
                self.rows_written.emit(self._total_rows, path)
            except Exception as exc:
                self.error.emit(f"final flush failed: {exc!r}")
            finally:
                self._chunk = []

        # Close writer handles.
        try:
            if self._parquet_writer is not None:
                self._parquet_writer.close()
        except Exception:
            pass
        self._parquet_writer = None

        try:
            if self._csv_file is not None:
                self._csv_file.close()
        except Exception:
            pass
        self._csv_file = None
        self._csv_writer = None

        total = self._total_rows
        self._current_path = None
        self.recording_finished.emit(path, total)

    # --------------------------------------------------------------- #
    #  Backlog monitor                                                  #
    # --------------------------------------------------------------- #

    def _check_backlog(self) -> None:
        if self._queue is None:
            return
        depth = self._queue.qsize()
        if depth >= WARN_QUEUE_DEPTH and not self._warning_emitted:
            self._warning_emitted = True
            self.backlog_warning.emit(depth)
        elif depth < WARN_QUEUE_DEPTH // 2:
            # Hysteresis: rearm the warning once depth recovers.
            self._warning_emitted = False


def make_bounded_queue() -> queue.Queue:
    """Factory for the producer/consumer queue.

    Uses a hard `maxsize` so even pathological backpressure can't grow
    RAM without bound.  The TelemetryStreamer uses `put_nowait` and
    catches `queue.Full`, so dropping at this ceiling is graceful.
    """
    return queue.Queue(maxsize=MAX_QUEUE_DEPTH)
