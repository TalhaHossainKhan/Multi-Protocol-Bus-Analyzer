"""Tests for engine/session_folder.py."""

import json
import os
import tempfile
from pathlib import Path

import pytest

from engine.session_folder import SessionFolder, _safe


# ── _safe helper ──────────────────────────────────────────────────────────────

def test_safe_replaces_special_chars():
    assert "/" not in _safe("BLE/ESP32")
    assert " " not in _safe("My Device")
    assert ":" not in _safe("COM:3")


def test_safe_truncates():
    long_label = "a" * 100
    assert len(_safe(long_label)) <= 40


def test_safe_empty_fallback():
    assert _safe("") == "device"
    assert _safe("!!!") == "device"


# ── SessionFolder construction ────────────────────────────────────────────────

def test_creates_directory():
    with tempfile.TemporaryDirectory() as tmp:
        sf = SessionFolder(tmp, "MyESP32")
        assert sf.path.exists()
        assert sf.path.is_dir()


def test_folder_name_contains_label():
    with tempfile.TemporaryDirectory() as tmp:
        sf = SessionFolder(tmp, "MotorTest")
        assert "MotorTest" in sf.path.name


def test_folder_name_contains_timestamp():
    with tempfile.TemporaryDirectory() as tmp:
        sf = SessionFolder(tmp, "dev")
        # Timestamp format: YYYYMMDD_HHMMSS
        parts = sf.path.name.split("_")
        assert len(parts) >= 3
        assert parts[-2].isdigit()  # date part
        assert parts[-1].isdigit()  # time part


def test_special_chars_in_label_safe():
    with tempfile.TemporaryDirectory() as tmp:
        sf = SessionFolder(tmp, "BLE / ESP32 (rev2)")
        assert "/" not in sf.path.name
        assert "(" not in sf.path.name
        assert sf.path.exists()


# ── data_path ─────────────────────────────────────────────────────────────────

def test_data_path_inside_folder():
    with tempfile.TemporaryDirectory() as tmp:
        sf = SessionFolder(tmp, "dev")
        dp = sf.data_path("/dev/ttyUSB0", "ESP32")
        assert dp.parent == sf.path
        assert dp.name.startswith("data_")
        assert dp.name.endswith(".csv")


def test_data_path_different_devices_distinct():
    with tempfile.TemporaryDirectory() as tmp:
        sf = SessionFolder(tmp, "dev")
        p1 = sf.data_path("/dev/ttyUSB0", "ESP32")
        p2 = sf.data_path("/dev/ttyUSB1", "STM32")
        assert p1.name != p2.name


# ── config_path ───────────────────────────────────────────────────────────────

def test_config_path_is_json_inside_folder():
    with tempfile.TemporaryDirectory() as tmp:
        sf = SessionFolder(tmp, "dev")
        assert sf.config_path() == sf.path / "session_config.json"
        assert sf.config_path().suffix == ".json"


# ── is_session_folder ─────────────────────────────────────────────────────────

def test_is_session_folder_true():
    with tempfile.TemporaryDirectory() as tmp:
        sf = SessionFolder(tmp, "dev")
        sf.config_path().write_text(
            json.dumps({"version": 5, "plots": [], "analysis": {},
                        "session_folder": True, "data_files": {}})
        )
        assert SessionFolder.is_session_folder(str(sf.path))


def test_is_session_folder_false_no_json():
    with tempfile.TemporaryDirectory() as tmp:
        sf = SessionFolder(tmp, "dev")
        # No session_config.json written
        assert not SessionFolder.is_session_folder(str(sf.path))


def test_is_session_folder_false_nonexistent():
    assert not SessionFolder.is_session_folder("/tmp/nonexistent_daq_folder_xyz")


# ── load_folder ───────────────────────────────────────────────────────────────

def test_load_folder_returns_dict():
    with tempfile.TemporaryDirectory() as tmp:
        sf = SessionFolder(tmp, "dev")
        config = {
            "version": 5,
            "session_folder": True,
            "data_files": {"COM3": "data_dev.csv"},
            "device_uid": "abc123",
            "device_name": "ESP32",
            "transport": "usb",
            "plots": [{"id": "plot_0", "title": "Plot 1"}],
            "analysis": {},
            "saved_at": "2026-05-22T00:00:00+00:00",
        }
        sf.config_path().write_text(json.dumps(config))
        doc = SessionFolder.load_folder(str(sf.path))
        assert doc["session_folder"] is True
        assert doc["device_name"] == "ESP32"
        assert doc["data_files"] == {"COM3": "data_dev.csv"}
        assert len(doc["plots"]) == 1


def test_load_folder_raises_on_missing_json():
    with tempfile.TemporaryDirectory() as tmp:
        sf = SessionFolder(tmp, "dev")
        with pytest.raises(Exception):
            SessionFolder.load_folder(str(sf.path))


# ── default_root ──────────────────────────────────────────────────────────────

def test_default_root_exists_after_call():
    root = SessionFolder.default_root()
    assert os.path.isdir(root)
    assert "DAQ_Sessions" in root
