"""DSP Analysis backend.

All computation runs on NumPy arrays on the calling thread (UI thread at
button-click time).  Heavy operations (FFT, spectrogram) are fast enough for
typical embedded-DAQ dataset sizes (< 1 M samples).  For longer datasets the
UI should offload to a QThread — that is the caller's responsibility.

Public API
----------
Scalar functions  → float  (max, min, mean, median, std, rms, peaks)
Array functions   → ndarray  (moving_average, correlation lags, histogram)
Spectral          → (freqs, mags) / (freqs, times, Sxx_dB)
Formula engine    → ndarray  via numexpr (never eval)
"""

from __future__ import annotations

import re
from typing import Dict, Optional, Tuple

import numpy as np

# ── Optional heavy dependencies (guarded) ────────────────────────────────────
try:
    import numexpr as ne
    _HAS_NUMEXPR = True
except ImportError:
    _HAS_NUMEXPR = False

try:
    from scipy.signal import find_peaks as _scipy_find_peaks
    from scipy.signal import spectrogram as _scipy_spectrogram
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def sanitize_name(name: str) -> str:
    """Convert a channel name to a valid Python identifier for formula use."""
    s = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    if s and s[0].isdigit():
        s = "_" + s
    return s or "_ch"


def estimate_sample_rate(x: np.ndarray) -> float:
    """Estimate sample rate (Hz) from a PC_Time_s array."""
    if len(x) < 2:
        return 1.0
    dt = (x[-1] - x[0]) / (len(x) - 1)
    return 1.0 / dt if dt > 0 else 1.0


def slice_by_range(
    x: np.ndarray,
    y: np.ndarray,
    mode: str,
    tag_start: Optional[float] = None,
    tag_end: Optional[float] = None,
    vb_xrange: Optional[Tuple[float, float]] = None,
) -> Tuple[np.ndarray, np.ndarray]:
    """Return (x, y) clipped to the requested data range."""
    if mode == "tag" and tag_start is not None and tag_end is not None:
        mask = (x >= tag_start) & (x <= tag_end)
        return x[mask], y[mask]
    if mode == "viewbox" and vb_xrange is not None:
        mask = (x >= vb_xrange[0]) & (x <= vb_xrange[1])
        return x[mask], y[mask]
    return x, y


# ═══════════════════════════════════════════════════════════════════════════════
# Scalar DSP functions
# ═══════════════════════════════════════════════════════════════════════════════

def compute_max(y: np.ndarray) -> float:
    return float(np.max(y)) if len(y) else float("nan")

def compute_min(y: np.ndarray) -> float:
    return float(np.min(y)) if len(y) else float("nan")

def compute_mean(y: np.ndarray) -> float:
    return float(np.mean(y)) if len(y) else float("nan")

def compute_median(y: np.ndarray) -> float:
    return float(np.median(y)) if len(y) else float("nan")

def compute_std(y: np.ndarray) -> float:
    return float(np.std(y)) if len(y) else float("nan")

def compute_rms(y: np.ndarray) -> float:
    return float(np.sqrt(np.mean(y ** 2))) if len(y) else float("nan")


def compute_peaks(
    y: np.ndarray, x: Optional[np.ndarray] = None
) -> Dict[str, float]:
    """Detect local maxima above the signal mean."""
    if not len(y):
        return {"peak_count": 0.0, "mean_peak_value": float("nan"),
                "max_peak_value": float("nan")}
    if _HAS_SCIPY:
        idx, _ = _scipy_find_peaks(y, height=float(np.mean(y)))
    else:
        idx = np.where((y[1:-1] > y[:-2]) & (y[1:-1] > y[2:]))[0] + 1
    vals = y[idx] if len(idx) else np.array([])
    return {
        "peak_count":     float(len(idx)),
        "mean_peak_value": float(np.mean(vals)) if len(vals) else float("nan"),
        "max_peak_value":  float(np.max(vals))  if len(vals) else float("nan"),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Array DSP functions (return arrays → plotted in a separate dock)
# ═══════════════════════════════════════════════════════════════════════════════

def compute_moving_average(y: np.ndarray, window: int = 10) -> np.ndarray:
    """Causal moving average; result same length as input."""
    window = max(1, min(window, len(y)))
    kernel = np.ones(window) / float(window)
    return np.convolve(y, kernel, mode="same")


def compute_correlation(
    y_a: np.ndarray, y_b: np.ndarray
) -> Dict[str, float]:
    """Pearson r + lag at peak of normalized cross-correlation."""
    n = min(len(y_a), len(y_b))
    if n < 2:
        return {"pearson_r": float("nan"), "lag_at_max_samples": 0.0}
    a, b = y_a[:n] - y_a[:n].mean(), y_b[:n] - y_b[:n].mean()
    r = float(np.corrcoef(y_a[:n], y_b[:n])[0, 1])
    xcorr = np.correlate(a, b, mode="full")
    lag = int(np.argmax(np.abs(xcorr))) - (n - 1)
    return {"pearson_r": r, "lag_at_max_samples": float(lag)}


def compute_histogram(
    y: np.ndarray, bins: int = 20
) -> Tuple[np.ndarray, np.ndarray]:
    """Return (bin_centers, counts) for bar/line plot."""
    if not len(y):
        return np.array([0.0]), np.array([0.0])
    counts, edges = np.histogram(y, bins=bins)
    centers = 0.5 * (edges[:-1] + edges[1:])
    return centers, counts.astype(float)


# ═══════════════════════════════════════════════════════════════════════════════
# Spectral analysis
# ═══════════════════════════════════════════════════════════════════════════════

def compute_fft(
    y: np.ndarray, sample_rate: float = 1.0
) -> Tuple[np.ndarray, np.ndarray]:
    """Return (freqs_hz, magnitudes).  Uses one-sided rfft."""
    if len(y) < 2:
        return np.array([0.0]), np.array([0.0])
    n = len(y)
    freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate)
    mags  = np.abs(np.fft.rfft(y)) * (2.0 / n)
    return freqs, mags


def compute_spectrogram(
    y: np.ndarray,
    sample_rate: float = 1.0,
    nperseg: int = 256,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (freqs, times, Sxx_dB) for waterfall / ImageItem display.

    Shape of Sxx_dB: (n_freqs, n_time_frames) — rows = frequency bins,
    columns = time frames.  Transposed from scipy convention so that an
    ImageItem(Sxx_dB) renders with freq on Y and time on X naturally.
    """
    nperseg = min(nperseg, max(4, len(y)))
    if _HAS_SCIPY:
        f, t, Sxx = _scipy_spectrogram(y, fs=sample_rate, nperseg=nperseg)
    else:
        hop = nperseg // 2
        frames: list = []
        for start in range(0, len(y) - nperseg + 1, hop):
            seg = y[start : start + nperseg]
            frames.append(np.abs(np.fft.rfft(seg)) * (2.0 / nperseg))
        if not frames:
            return np.array([0.0]), np.array([0.0]), np.zeros((1, 1))
        Sxx = np.column_stack(frames)   # (freq_bins, time_frames)
        f   = np.fft.rfftfreq(nperseg, d=1.0 / sample_rate)
        t   = np.arange(Sxx.shape[1]) * hop / sample_rate

    Sxx_dB = 10.0 * np.log10(np.maximum(Sxx, 1e-12))
    return f, t, Sxx_dB   # Sxx_dB shape: (n_freqs, n_time_frames)


# ═══════════════════════════════════════════════════════════════════════════════
# Custom formula engine
# ═══════════════════════════════════════════════════════════════════════════════

def evaluate_formula(
    formula: str,
    channel_data: Dict[str, np.ndarray],
) -> np.ndarray:
    """Evaluate *formula* using numexpr (never Python eval).

    *channel_data* maps sanitized variable names → numpy arrays.
    All arrays are truncated to the shortest length before evaluation.

    Raises
    ------
    RuntimeError  if numexpr is not installed.
    ValueError    if the formula string is invalid or evaluation fails.
    """
    if not _HAS_NUMEXPR:
        raise RuntimeError(
            "numexpr is not installed.  Run: pip install numexpr"
        )
    if not channel_data:
        raise ValueError("No channel data provided to formula engine.")

    min_len = min(len(v) for v in channel_data.values())
    if min_len == 0:
        raise ValueError("All channels are empty — no data to evaluate.")

    local_dict = {k: np.ascontiguousarray(v[:min_len], dtype=np.float64)
                  for k, v in channel_data.items()}
    try:
        result = ne.evaluate(formula, local_dict=local_dict)
    except Exception as exc:
        raise ValueError(f"Formula evaluation error: {exc}") from exc
    return np.asarray(result, dtype=np.float64)


# ═══════════════════════════════════════════════════════════════════════════════
# Oscilloscope trigger detection
# ═══════════════════════════════════════════════════════════════════════════════

def find_trigger(
    y: np.ndarray,
    threshold: float,
    edge: str = "rising",
    hysteresis: float = 0.0,
) -> Optional[int]:
    """Return the index of the first trigger crossing in *y*, or None.

    Parameters
    ----------
    y          : 1-D signal array
    threshold  : crossing level
    edge       : "rising" | "falling"
    hysteresis : deadband around threshold (avoids chatter near the level)
    """
    if len(y) < 2:
        return None
    hi = threshold + hysteresis
    lo = threshold - hysteresis
    if edge == "rising":
        for i in range(1, len(y)):
            if y[i - 1] < lo and y[i] >= hi:
                return i
    else:
        for i in range(1, len(y)):
            if y[i - 1] >= hi and y[i] < lo:
                return i
    return None
