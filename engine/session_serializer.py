"""Session configuration serializer.

Saves the user's visual workspace (plot layout, channel colours, axis
configs, thresholds, rolling-window settings) to a JSON file alongside
any recorded CSV/Parquet telemetry, enabling full post-analysis replay.

JSON schema v1
--------------
{
  "version": 1,
  "saved_at": "<ISO-8601>",
  "plots": [
    {
      "id":               "plot_0",
      "title":            "Plot 1",
      "x_device":         null,        // null → use PC_Time_s
      "x_channel":        null,
      "rolling_window_s": 0.0,         // 0 → show all data
      "y_axes": [
        {"label":"Temp (C)","min":null,"max":null,"locked":false,"color":"#cfcfcf"}
      ],
      "channels": [
        {
          "device":     "COM3",
          "channel":    "temperature",
          "y_axis":     0,
          "color":      "#4FC3F7",
          "line_style": "solid",        // solid | dash | dot | dashdot
          "line_width": 1.2
        }
      ],
      "thresholds": [
        {"id":"t0","value":85.0,"color":"#ff4444","label":"Max Temp"}
      ]
    }
  ]
}
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 5          # v5 — session folder: data_files map + session_folder flag

_DEFAULT_Y_AXIS: Dict[str, Any] = {
    "label": "",
    "min": None,
    "max": None,
    "locked": False,
    "color": "#cfcfcf",
}


def default_plot_config(plot_id: str = "plot_0", title: str = "Plot 1") -> dict:
    """Return a minimal valid plot-config dict with one unlocked Y-axis."""
    return {
        "id": plot_id,
        "title": title,
        "x_device": None,
        "x_channel": None,
        "rolling_window_s": 0.0,
        "y_axes": [dict(_DEFAULT_Y_AXIS)],
        "channels": [],
        "thresholds": [],
        "overlay_path": None, # per-plot historical overlay
        "alarms": [], # logic alarms
        "x_markers": [], # vertical time markers
        "analysis_tags": [], # LinearRegionItem tags
    }


def default_analysis_config() -> dict:
    """Return a minimal valid analysis-config dict."""
    return {
        "saved_formulas":    [],
        "oscilloscope":      {},
        "last_dsp_function": "mean",
    }


def save_session_config(
    plots_config: List[dict],
    path: str,
    *,
    device_uid: Optional[str] = None,
    device_name: Optional[str] = None,
    transport: Optional[str] = None,
    analysis: Optional[dict] = None,
    session_folder: bool = False,
    data_files: Optional[Dict[str, str]] = None,
) -> str:
    """Write *plots_config* to *path* as JSON.  Returns the resolved path.

    ``session_folder=True`` marks this as part of a unified session folder.
    ``data_files`` maps device_path → relative CSV filename within the folder.
    """
    abs_path = os.path.abspath(path)
    parent = os.path.dirname(abs_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    doc = {
        "version":        SCHEMA_VERSION,
        "saved_at":       datetime.now(tz=timezone.utc).isoformat(),
        "session_folder": session_folder,
        "data_files":     data_files or {},
        "device_uid":     device_uid,
        "device_name":    device_name,
        "transport":      transport,
        "plots":          plots_config,
        "analysis":       analysis if analysis is not None else default_analysis_config(),
    }
    with open(abs_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
    return abs_path


def load_session_config(path: str) -> dict:
    """Load a session config JSON.

    Accepts v1, v2, and v3 — older files are
    upgraded transparently in-memory so existing recordings keep working.
    """
    with open(path, "r", encoding="utf-8") as f:
        doc = json.load(f)
    ver = doc.get("version")
    if ver not in (1, 2, 3, 4, SCHEMA_VERSION):
        raise ValueError(
            f"Unsupported session config version {ver!r}; "
            f"expected 1–{SCHEMA_VERSION}"
        )
    # v1 → v2
    if ver == 1:
        doc["device_uid"]  = None
        doc["device_name"] = None
        doc["transport"]   = None
        for p in doc.get("plots", []):
            p.setdefault("overlay_path", None)
        ver = 2
    # v2 → v3
    if ver == 2:
        for p in doc.get("plots", []):
            p.setdefault("alarms",        [])
            p.setdefault("x_markers",     [])
            p.setdefault("analysis_tags", [])
        ver = 3
    # v3 → v4
    if ver == 3:
        doc.setdefault("analysis", default_analysis_config())
        ver = 4
    # v4 → v5
    if ver == 4:
        doc.setdefault("session_folder", False)
        doc.setdefault("data_files",     {})
        ver = 5
    doc["version"] = SCHEMA_VERSION
    return doc
