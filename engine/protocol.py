"""Wire-format constants and metadata structures.

Mirror of the C/C++ firmware in `mcu_firmware/telemetry_handshake.ino`.
All multi-byte fields on the wire are little-endian, matching the native
byte order of every supported MCU (AVR, ARM Cortex-M, Xtensa/ESP32).

V1 types (MSG_METADATA 0x01) are preserved for backward compatibility.
V2 types (MSG_METADATA_V2 0x03) are the current standard — richer
fields including hardware UID, per-channel units, and declared ranges.
The desktop always works with DeviceMetadataV2 internally; V1 frames
are coerced via coerce_v1_to_v2().
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from typing import Tuple


# ---------------------------------------------------------------------------
# Frame markers
# ---------------------------------------------------------------------------
SYNC_1: int = 0xAA
SYNC_2: int = 0xBB
ENQ: int = 0x05

MSG_METADATA: int = 0x01       # V1 — legacy
MSG_TELEMETRY: int = 0x02      # unchanged across versions
MSG_METADATA_V2: int = 0x03   # V2 — current standard

METADATA_HEADER: bytes = bytes([SYNC_1, SYNC_2, MSG_METADATA])
TELEMETRY_HEADER: bytes = bytes([SYNC_1, SYNC_2, MSG_TELEMETRY])
METADATA_V2_HEADER: bytes = bytes([SYNC_1, SYNC_2, MSG_METADATA_V2])

# V1 field widths
DEVICE_ID_LEN: int = 16
CHANNEL_NAME_LEN: int = 16

# V2 field widths
DEVICE_UID_LEN: int = 32
DEVICE_NAME_LEN: int = 32
UNIT_LEN: int = 8

# V2 per-channel record size:
#   1 (channel_id) + 16 (name) + 8 (unit) + 1 (type_code) + 4 (range_min) + 4 (range_max)
V2_CHANNEL_RECORD_SIZE: int = 34

# V2 header payload before channels:
#   32 (uid) + 32 (name) + 1 (fw_major) + 1 (fw_minor) + 2 (fw_build) + 1 (num_ch)
V2_PRE_CHANNEL_SIZE: int = 69


# ---------------------------------------------------------------------------
# Type codes — shared between V1 and V2, must match the firmware defines
# ---------------------------------------------------------------------------
# type_code -> (struct fmt char, byte size, friendly label)
TYPE_TABLE: dict[int, tuple[str, int, str]] = {
    0x01: ("B", 1, "uint8"),
    0x02: ("H", 2, "uint16"),
    0x03: ("I", 4, "uint32"),
    0x04: ("b", 1, "int8"),
    0x05: ("h", 2, "int16"),
    0x06: ("i", 4, "int32"),
    0x07: ("f", 4, "float32"),
    0x08: ("d", 8, "float64"),
}


# ===========================================================================
# V1 structures (preserved for backward compatibility)
# ===========================================================================

@dataclass(frozen=True)
class ChannelSpec:
    name: str
    type_code: int
    struct_fmt: str
    size_bytes: int
    label: str


@dataclass(frozen=True)
class DeviceMetadata:
    device_id: str
    channels: Tuple[ChannelSpec, ...]

    @property
    def num_channels(self) -> int:
        return len(self.channels)

    @property
    def packet_size_bytes(self) -> int:
        return sum(c.size_bytes for c in self.channels)

    @property
    def struct_fmt(self) -> str:
        return "<" + "".join(c.struct_fmt for c in self.channels)


def parse_metadata_payload(payload: bytes) -> DeviceMetadata:
    """Parse bytes following the 0xAA 0xBB 0x01 V1 metadata header."""
    if len(payload) < DEVICE_ID_LEN + 1:
        raise ValueError("V1 metadata payload too short")

    device_id = (
        payload[:DEVICE_ID_LEN]
        .split(b"\x00", 1)[0]
        .decode("ascii", errors="replace")
        .strip()
    )
    num_ch = payload[DEVICE_ID_LEN]

    required = DEVICE_ID_LEN + 1 + num_ch * (CHANNEL_NAME_LEN + 1)
    if len(payload) < required:
        raise ValueError(
            f"V1 metadata truncated: need {required} bytes, got {len(payload)}"
        )

    channels = []
    offset = DEVICE_ID_LEN + 1
    for i in range(num_ch):
        name_bytes = payload[offset : offset + CHANNEL_NAME_LEN]
        name = (
            name_bytes.split(b"\x00", 1)[0]
            .decode("ascii", errors="replace")
            .strip()
        )
        type_code = payload[offset + CHANNEL_NAME_LEN]
        if type_code not in TYPE_TABLE:
            raise ValueError(f"unknown type code 0x{type_code:02X}")
        fmt, size, label = TYPE_TABLE[type_code]
        channels.append(
            ChannelSpec(
                name=name or f"ch{i}",
                type_code=type_code,
                struct_fmt=fmt,
                size_bytes=size,
                label=label,
            )
        )
        offset += CHANNEL_NAME_LEN + 1

    return DeviceMetadata(device_id=device_id, channels=tuple(channels))


# ===========================================================================
# V2 structures
# ===========================================================================

@dataclass(frozen=True)
class ChannelSpecV2:
    channel_id: int
    name: str
    unit: str
    type_code: int
    struct_fmt: str
    size_bytes: int
    label: str
    range_min: float
    range_max: float


@dataclass(frozen=True)
class DeviceMetadataV2:
    device_uid: str          # hardware-derived, persistent across replugs
    device_name: str         # firmware-set friendly name
    firmware_version: str    # "major.minor.build"
    channels: Tuple[ChannelSpecV2, ...]

    @property
    def num_channels(self) -> int:
        return len(self.channels)

    @property
    def packet_size_bytes(self) -> int:
        """Telemetry payload size (excludes the 3-byte sync header)."""
        return sum(c.size_bytes for c in self.channels)

    @property
    def struct_fmt(self) -> str:
        """Little-endian struct format string for one telemetry payload."""
        return "<" + "".join(c.struct_fmt for c in self.channels)


def parse_metadata_v2_payload(payload: bytes) -> DeviceMetadataV2:
    """Parse bytes following the 0xAA 0xBB 0x03 V2 metadata header.

    Layout (all offsets relative to start of payload, i.e. after the header):
        [32] device_uid       NUL-padded ASCII hex
        [32] device_name      NUL-padded ASCII
        [ 1] fw_major         uint8
        [ 1] fw_minor         uint8
        [ 2] fw_build         uint16 LE
        [ 1] num_channels     uint8
        Per channel (34 bytes):
          [1]  channel_id     uint8
          [16] name           NUL-padded ASCII
          [8]  unit           NUL-padded ASCII
          [1]  type_code      uint8
          [4]  range_min      float32 LE
          [4]  range_max      float32 LE
    """
    if len(payload) < V2_PRE_CHANNEL_SIZE:
        raise ValueError(
            f"V2 metadata payload too short: need {V2_PRE_CHANNEL_SIZE}, "
            f"got {len(payload)}"
        )

    uid_raw = payload[:DEVICE_UID_LEN].split(b"\x00", 1)[0]
    device_uid = uid_raw.decode("ascii", errors="replace").strip()

    name_raw = payload[DEVICE_UID_LEN : DEVICE_UID_LEN + DEVICE_NAME_LEN]
    device_name = (
        name_raw.split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()
    )

    fw_off = DEVICE_UID_LEN + DEVICE_NAME_LEN
    fw_major = payload[fw_off]
    fw_minor = payload[fw_off + 1]
    fw_build = struct.unpack_from("<H", payload, fw_off + 2)[0]
    firmware_version = f"{fw_major}.{fw_minor}.{fw_build}"

    num_ch = payload[fw_off + 4]

    required = V2_PRE_CHANNEL_SIZE + num_ch * V2_CHANNEL_RECORD_SIZE
    if len(payload) < required:
        raise ValueError(
            f"V2 metadata truncated: need {required} bytes, got {len(payload)}"
        )

    channels = []
    offset = V2_PRE_CHANNEL_SIZE
    for i in range(num_ch):
        rec = payload[offset : offset + V2_CHANNEL_RECORD_SIZE]
        channel_id = rec[0]
        name = rec[1:17].split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()
        unit = rec[17:25].split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()
        type_code = rec[25]
        if type_code not in TYPE_TABLE:
            raise ValueError(f"unknown type code 0x{type_code:02X} in channel {i}")
        fmt, size, label = TYPE_TABLE[type_code]
        range_min = struct.unpack_from("<f", rec, 26)[0]
        range_max = struct.unpack_from("<f", rec, 30)[0]
        channels.append(
            ChannelSpecV2(
                channel_id=channel_id,
                name=name or f"ch{i}",
                unit=unit,
                type_code=type_code,
                struct_fmt=fmt,
                size_bytes=size,
                label=label,
                range_min=float(range_min),
                range_max=float(range_max),
            )
        )
        offset += V2_CHANNEL_RECORD_SIZE

    return DeviceMetadataV2(
        device_uid=device_uid,
        device_name=device_name,
        firmware_version=firmware_version,
        channels=tuple(channels),
    )


def coerce_v1_to_v2(v1: DeviceMetadata) -> DeviceMetadataV2:
    """Wrap a V1 DeviceMetadata as V2 for uniform downstream handling.

    A pseudo-UID is derived by hashing the device_id string so that the
    same physical board (same device_id) always maps to the same registry
    entry, even without hardware UID support.
    """
    pseudo_uid = hashlib.sha256(v1.device_id.encode()).hexdigest()[:16]
    channels_v2 = tuple(
        ChannelSpecV2(
            channel_id=i,
            name=ch.name,
            unit="",
            type_code=ch.type_code,
            struct_fmt=ch.struct_fmt,
            size_bytes=ch.size_bytes,
            label=ch.label,
            range_min=float("nan"),
            range_max=float("nan"),
        )
        for i, ch in enumerate(v1.channels)
    )
    return DeviceMetadataV2(
        device_uid=pseudo_uid,
        device_name=v1.device_id,
        firmware_version="legacy",
        channels=channels_v2,
    )
