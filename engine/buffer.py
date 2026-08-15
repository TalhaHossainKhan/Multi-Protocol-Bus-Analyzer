"""Lock-protected single-producer / single-consumer ring buffers.

Step-4 layout note:
    `MultiChannelRingBuffer` is backed by a `(num_channels, capacity)`
    float64 array.  This row-major layout gives the UI thread *contiguous*
    per-channel slices (`buf[ch, lo:hi]`), which is what PyQtGraph wants
    in `curve.setData(y)` — no transposition and no extra copies on the
    hot 60 Hz path.

`RingBuffer`              — 1D scalar buffer (kept as a utility).
`MultiChannelRingBuffer`  — fixed-capacity N-channel buffer used by the
                            telemetry streamer.  Memory is allocated
                            once and stays flat indefinitely; once full,
                            new samples overwrite the oldest.
"""

from __future__ import annotations

import threading
from typing import Sequence, Tuple

import numpy as np


class RingBuffer:
    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._capacity = int(capacity)
        self._buf = np.zeros(self._capacity, dtype=np.float64)
        self._write_idx = 0
        self._count = 0
        self._total_written = 0
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def total_written(self) -> int:
        with self._lock:
            return self._total_written

    def write(self, data: np.ndarray) -> None:
        if data.size == 0:
            return
        if data.size >= self._capacity:
            data = data[-self._capacity:]
        n = data.size
        with self._lock:
            end = self._write_idx + n
            if end <= self._capacity:
                self._buf[self._write_idx:end] = data
            else:
                first = self._capacity - self._write_idx
                self._buf[self._write_idx:] = data[:first]
                self._buf[: n - first] = data[first:]
            self._write_idx = end % self._capacity
            self._count = min(self._capacity, self._count + n)
            self._total_written += n

    def read_latest(self, n: int) -> np.ndarray:
        with self._lock:
            n = min(n, self._count)
            if n == 0:
                return np.empty(0, dtype=np.float64)
            start = (self._write_idx - n) % self._capacity
            if start + n <= self._capacity:
                return self._buf[start:start + n].copy()
            out = np.empty(n, dtype=np.float64)
            first = self._capacity - start
            out[:first] = self._buf[start:]
            out[first:] = self._buf[: n - first]
            return out


class MultiChannelRingBuffer:
    """N-channel ring buffer backed by a `(num_channels, capacity)` float64 array.

    Producer  (TelemetryStreamer thread) calls `push(sample)`.
    Consumer  (UI thread, 60 Hz) calls `read_latest(n)` or `read_window()`.

    All access is mutex-protected.  Memory usage stays flat for the life of
    the buffer — once full, `push` overwrites the oldest column.

    The buffer also exposes:
        head     : column index where the NEXT sample will be written
        count    : number of valid samples currently held (≤ capacity)
        total_written : monotonic counter, never wraps (for diagnostics)

    `head` and `count` together let the UI thread compute the slice
    `[tail .. head)` that is in temporal order without locking on the
    inner array (we snapshot under the lock and then copy).
    """

    def __init__(self, capacity: int, num_channels: int) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if num_channels <= 0:
            raise ValueError("num_channels must be positive")
        self._capacity = int(capacity)
        self._num_channels = int(num_channels)
        # Row-major (C-order) so each per-channel row is a contiguous block.
        self._buf = np.zeros(
            (self._num_channels, self._capacity), dtype=np.float64, order="C"
        )
        self._write_idx = 0      # column where the next sample lands
        self._count = 0
        self._total_written = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    #  Read-only accessors                                                 #
    # ------------------------------------------------------------------ #

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def num_channels(self) -> int:
        return self._num_channels

    @property
    def total_written(self) -> int:
        with self._lock:
            return self._total_written

    def snapshot_state(self) -> Tuple[int, int, int]:
        """Atomically read (head, count, total_written)."""
        with self._lock:
            return self._write_idx, self._count, self._total_written

    # ------------------------------------------------------------------ #
    #  Producer side                                                       #
    # ------------------------------------------------------------------ #

    def push(self, sample: Sequence[float]) -> None:
        if len(sample) != self._num_channels:
            raise ValueError(
                f"sample has {len(sample)} values, expected {self._num_channels}"
            )
        with self._lock:
            # Per-channel write at the current head column.
            for ch in range(self._num_channels):
                self._buf[ch, self._write_idx] = sample[ch]
            self._write_idx = (self._write_idx + 1) % self._capacity
            if self._count < self._capacity:
                self._count += 1
            self._total_written += 1

    # ------------------------------------------------------------------ #
    #  Consumer side                                                       #
    # ------------------------------------------------------------------ #

    def read_latest(self, n: int) -> np.ndarray:
        """Return the most-recent `min(n, count)` samples in temporal order.

        Result shape: `(num_channels, n_returned)`.  Each row is contiguous,
        suitable for direct hand-off to `curve.setData(y_row)`.
        """
        with self._lock:
            n = min(n, self._count)
            if n == 0:
                return np.empty((self._num_channels, 0), dtype=np.float64)
            start = (self._write_idx - n) % self._capacity
            if start + n <= self._capacity:
                # Contiguous tail-to-head window — single block copy.
                return self._buf[:, start : start + n].copy()
            # Wraparound — copy in two pieces into a fresh contiguous array.
            out = np.empty((self._num_channels, n), dtype=np.float64)
            first = self._capacity - start
            out[:, :first] = self._buf[:, start:]
            out[:, first:] = self._buf[:, : n - first]
            return out

    def read_window(self) -> np.ndarray:
        """Return ALL currently-held samples in temporal order.

        Shape: `(num_channels, count)`.  Equivalent to `read_latest(capacity)`,
        kept as a name that better describes the semantic on the UI side.
        """
        return self.read_latest(self._capacity)
