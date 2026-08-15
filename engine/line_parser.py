"""Plain-text serial format detector and line parser.

Any MCU that prints lines of text — JSON, key:value, or CSV — is supported
without custom DAQ firmware.  The software reads the first few lines, detects
the format, infers channel names, and streams data normally.

Supported formats (checked in priority order):
  json     {"voltage": 3.14, "current": 1.2}
  labeled  Voltage:3.14  Current:1.2       (or =, or tab-separated Arduino style)
  csv      3.14,1.2,25.3                   (optional header line for names)
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import List, Optional, Tuple

from engine.protocol import ChannelSpecV2, DeviceMetadataV2


# Regex for labeled format: captures "key:value" and "key=value" pairs,
# including scientific notation values.
_LABELED_RE = re.compile(
    r'([A-Za-z_]\w*)\s*[:=]\s*([+-]?\d+\.?\d*(?:[eE][+-]?\d+)?)'
)

_MAX_SNIFF_LINES = 10   # scan at most this many lines before deciding


def detect_format(lines: List[str]) -> Tuple[str, List[str]]:
    """Scan lines and return (format_name, channel_names).

    format_name is one of: 'json' | 'labeled' | 'csv' | 'unknown'.
    channel_names are inferred from the first matching line.
    """
    non_empty = [l.strip() for l in lines if l.strip()][:_MAX_SNIFF_LINES]

    prev_line: Optional[str] = None

    for line in non_empty:
        # ── JSON ─────────────────────────────────────────────────────────────
        if line.startswith('{'):
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    numeric_keys = [
                        k for k, v in obj.items()
                        if isinstance(v, (int, float))
                    ]
                    if numeric_keys:
                        return 'json', numeric_keys
            except (json.JSONDecodeError, ValueError):
                pass

        # ── Labeled key:value or key=value ───────────────────────────────────
        matches = _LABELED_RE.findall(line)
        if matches:
            return 'labeled', [m[0] for m in matches]

        # ── CSV (comma or tab separated) ─────────────────────────────────────
        for sep in (',', '\t'):
            parts = [p.strip() for p in line.split(sep) if p.strip()]
            if len(parts) < 1:
                continue
            try:
                [float(p) for p in parts]
                # All parts are numeric — check if previous line was a header.
                if prev_line is not None:
                    header_parts = [p.strip() for p in prev_line.split(sep)
                                    if p.strip()]
                    if len(header_parts) == len(parts):
                        # Previous line matches column count and is non-numeric.
                        try:
                            [float(h) for h in header_parts]
                        except ValueError:
                            return 'csv', header_parts  # valid header
                return 'csv', [f"ch{i}" for i in range(len(parts))]
            except ValueError:
                pass

        prev_line = line

    return 'unknown', []


def parse_line(
    line: str, fmt: str, channel_names: List[str]
) -> Optional[List[float]]:
    """Parse one text line in the already-detected format.

    Returns a list of floats in channel_names order, or None if the line
    doesn't match (debug messages, headers, empty lines — silently skipped).
    """
    line = line.strip()
    if not line:
        return None

    if fmt == 'json':
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                values = []
                for k in channel_names:
                    v = obj.get(k)
                    if not isinstance(v, (int, float)):
                        return None
                    values.append(float(v))
                return values
        except Exception:
            return None

    if fmt == 'labeled':
        matches = dict(_LABELED_RE.findall(line))
        if not matches:
            return None
        try:
            return [float(matches[k]) for k in channel_names]
        except (KeyError, ValueError):
            return None

    if fmt == 'csv':
        for sep in (',', '\t'):
            parts = [p.strip() for p in line.split(sep) if p.strip()]
            if len(parts) == len(channel_names):
                try:
                    return [float(p) for p in parts]
                except ValueError:
                    pass
        return None

    return None


def build_synthetic_metadata(
    device_path: str,
    channel_names: List[str],
    fmt: str,
) -> DeviceMetadataV2:
    """Build a DeviceMetadataV2 from inferred channel names.

    The UID is derived from the device path so it is stable across sessions
    for the same connection string.
    """
    uid = hashlib.sha256(device_path.encode()).hexdigest()[:32]
    channels = tuple(
        ChannelSpecV2(
            channel_id=i,
            name=name[:16],
            unit="",
            type_code=0x08,       # float64
            struct_fmt="d",
            size_bytes=8,
            label="float64",
            range_min=float("nan"),
            range_max=float("nan"),
        )
        for i, name in enumerate(channel_names)
    )
    return DeviceMetadataV2(
        device_uid=uid,
        device_name=f"Auto ({fmt})"[:32],
        firmware_version="auto-parse",
        channels=channels,
    )
