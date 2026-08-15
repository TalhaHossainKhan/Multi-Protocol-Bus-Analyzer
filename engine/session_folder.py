"""Session folder — unified on-disk layout for one recording session.

Each recording creates a timestamped directory that holds:
  session_config.json  — plots, channels, alarms, analysis config + data file map
  data_<device>.csv    — raw telemetry for each connected device

Usage
-----
On record start:
    folder = SessionFolder("~/Documents/DAQ_Sessions", "MyESP32")
    csv_path = folder.data_path(device_path, device_label)

On record stop:
    save_session_config(..., session_folder=True, data_files={...},
                        path=str(folder.config_path()))

Post-analysis:
    if SessionFolder.is_session_folder(chosen_path):
        doc = SessionFolder.load_folder(chosen_path)
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

from engine.session_serializer import load_session_config


def _safe(label: str, max_len: int = 40) -> str:
    """Replace non-alphanumeric chars and truncate for use in file names."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", label)[:max_len].strip("_") or "device"


class SessionFolder:
    """Owns the on-disk layout of one recording session."""

    def __init__(self, parent_dir: str, device_label: str) -> None:
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"{_safe(device_label)}_{ts}"
        self.path = Path(parent_dir) / name
        self.path.mkdir(parents=True, exist_ok=True)

    def data_path(self, device_path: str, device_label: str, ext: str = ".csv") -> Path:
        """Return the data file path for one device inside this folder.

        The filename combines the user-facing label *and* a sanitized fragment
        of ``device_path`` so that two devices sharing a label (e.g. an MCU
        reachable via both USB and BLE, or two identical boards) get separate
        files instead of interleaving rows into one corrupt CSV.
        """
        suffix = _safe(device_path, max_len=24)
        return self.path / f"data_{_safe(device_label)}__{suffix}{ext}"

    def config_path(self) -> Path:
        return self.path / "session_config.json"

    # ── Static helpers ──────────────────────────────────────────────────────

    @staticmethod
    def is_session_folder(path: str) -> bool:
        """Return True if *path* contains a session_config.json."""
        return (Path(path) / "session_config.json").exists()

    @staticmethod
    def load_folder(folder_path: str) -> dict:
        """Load and return the session_config.json from *folder_path*."""
        return load_session_config(str(Path(folder_path) / "session_config.json"))

    @staticmethod
    def default_root() -> str:
        """Return (and create) the default DAQ sessions root directory."""
        root = os.path.join(os.path.expanduser("~"), "Documents", "DAQ_Sessions")
        os.makedirs(root, exist_ok=True)
        return root
