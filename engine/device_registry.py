"""Persistent device registry backed by a JSON file.

The registry lives at ~/.daq_platform/registry.json and is loaded once
at startup, then mutated and flushed to disk whenever a device connects,
disconnects, or is renamed. All I/O happens on the UI thread (the only
caller), so no locking is required.

Key design rules:
  * The registry is keyed by device_uid — the hardware-derived string
    that uniquely identifies a physical board across replugs and across
    machines if the same registry file is shared.
  * display_name is the user-assigned label (set via rename dialog).
    It starts as device_name (firmware-provided) and is the ONLY field
    the user's rename action changes.
  * device_name, firmware_version, and channels are refreshed from the
    MCU on every successful handshake (firmware may be updated).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DEFAULT_REGISTRY_PATH: Path = Path.home() / ".daq_platform" / "registry.json"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ChannelRecord:
    channel_id: int
    name: str
    unit: str
    type_label: str     # e.g. "float32", "uint32"
    range_min: float
    range_max: float


@dataclass
class DeviceRecord:
    uid: str
    device_name: str        # from firmware — updated on each connect
    display_name: str       # user-assigned; survives restarts and renames
    firmware_version: str
    channels: list[ChannelRecord]
    first_seen: str         # ISO-8601 UTC
    last_seen: str          # ISO-8601 UTC
    last_baud: int = 115_200


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class DeviceRegistry:
    """JSON-backed store for device profiles.

    Call `load()` once at startup. After that, every mutating method
    immediately flushes to disk — writes are small and infrequent (one
    per device connect/disconnect/rename) so synchronous I/O is fine.
    """

    def __init__(self, path: Path = DEFAULT_REGISTRY_PATH) -> None:
        self._path = path
        self._records: dict[str, DeviceRecord] = {}

    # ------------------------------------------------------------------ #
    #  Persistence                                                         #
    # ------------------------------------------------------------------ #

    def load(self) -> None:
        """Read the registry from disk. Safe to call even if the file
        does not exist yet — starts with an empty registry in that case.
        """
        if not self._path.exists():
            self._records = {}
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._records = {}
            for uid, blob in raw.get("devices", {}).items():
                try:
                    channels = [ChannelRecord(**c) for c in blob.get("channels", [])]
                    blob_clean = {k: v for k, v in blob.items() if k != "channels"}
                    self._records[uid] = DeviceRecord(**blob_clean, channels=channels)
                except (TypeError, KeyError):
                    # Skip corrupted entries rather than crashing.
                    pass
        except (json.JSONDecodeError, OSError):
            self._records = {}

    def save(self) -> None:
        """Flush the current state to disk, creating parent directories
        if necessary.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "devices": {
                uid: {
                    **{k: v for k, v in asdict(rec).items() if k != "channels"},
                    "channels": [asdict(c) for c in rec.channels],
                }
                for uid, rec in self._records.items()
            }
        }
        self._path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------ #
    #  Queries                                                             #
    # ------------------------------------------------------------------ #

    def lookup(self, uid: str) -> Optional[DeviceRecord]:
        return self._records.get(uid)

    def all_records(self) -> list[DeviceRecord]:
        return list(self._records.values())

    # ------------------------------------------------------------------ #
    #  Mutations (each flushes to disk)                                    #
    # ------------------------------------------------------------------ #

    def upsert(self, record: DeviceRecord) -> None:
        """Insert or update a record. If the device is already known,
        preserve its display_name and first_seen; refresh everything else.
        """
        existing = self._records.get(record.uid)
        if existing is not None:
            record = DeviceRecord(
                uid=record.uid,
                device_name=record.device_name,
                display_name=existing.display_name,   # preserved
                firmware_version=record.firmware_version,
                channels=record.channels,
                first_seen=existing.first_seen,        # preserved
                last_seen=record.last_seen,
                last_baud=record.last_baud,
            )
        self._records[record.uid] = record
        self.save()

    def update_seen(self, uid: str, baud: int) -> None:
        rec = self._records.get(uid)
        if rec is None:
            return
        rec.last_seen = _now_iso()
        rec.last_baud = baud
        self.save()

    def set_display_name(self, uid: str, name: str) -> None:
        rec = self._records.get(uid)
        if rec is None:
            return
        rec.display_name = name
        self.save()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
