"""Auto-parse self-tests — no hardware required.

Tests the line_parser module and the full TelemetryStreamer pipeline using
a loopback serial port (serial.serial_for_url('loop://')).

Run with:
    python tests/test_auto_parse.py
or:
    python -m pytest tests/test_auto_parse.py -v
"""

from __future__ import annotations

import math
import os
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.line_parser import (
    build_synthetic_metadata,
    detect_format,
    parse_line,
)
from engine.protocol import DeviceMetadataV2


# ── helpers ───────────────────────────────────────────────────────────────────

def _check(condition: bool, msg: str) -> None:
    symbol = "✓" if condition else "✗ FAIL"
    print(f"  {symbol}  {msg}")
    if not condition:
        raise AssertionError(msg)


# ── 1. JSON format ────────────────────────────────────────────────────────────

def test_json_detect():
    lines = ['{"voltage": 3.14, "current": 1.2}',
             '{"voltage": 3.20, "current": 1.1}']
    fmt, ch = detect_format(lines)
    _check(fmt == "json", f"JSON detected (got '{fmt}')")
    _check(ch == ["voltage", "current"], f"JSON channel names {ch}")
    print("PASS  test_json_detect")


def test_json_parse():
    ch = ["voltage", "current"]
    vals = parse_line('{"voltage": 3.14, "current": 1.2}', "json", ch)
    _check(vals is not None, "JSON parse returned values")
    _check(abs(vals[0] - 3.14) < 1e-6, f"voltage {vals[0]}")
    _check(abs(vals[1] - 1.2)  < 1e-6, f"current {vals[1]}")
    # Garbage line returns None
    _check(parse_line("WARN: overflow", "json", ch) is None, "JSON garbage → None")
    print("PASS  test_json_parse")


# ── 2. Labeled key:value ──────────────────────────────────────────────────────

def test_labeled_colon_detect():
    lines = ["Voltage:3.14 Current:1.2", "Voltage:3.20 Current:1.1"]
    fmt, ch = detect_format(lines)
    _check(fmt == "labeled", f"labeled detected (got '{fmt}')")
    _check(ch == ["Voltage", "Current"], f"labeled channel names {ch}")
    print("PASS  test_labeled_colon_detect")


def test_labeled_equals_detect():
    lines = ["temp=25.3 humidity=65.0"]
    fmt, ch = detect_format(lines)
    _check(fmt == "labeled", f"labeled= detected (got '{fmt}')")
    _check(ch == ["temp", "humidity"], f"labels {ch}")
    print("PASS  test_labeled_equals_detect")


def test_arduino_tab_detect():
    """Arduino Serial Plotter format: key:value\tkey:value"""
    lines = ["Voltage:3.14\tCurrent:1.20\tTemp:25.3"]
    fmt, ch = detect_format(lines)
    _check(fmt == "labeled", f"Arduino tab labeled (got '{fmt}')")
    _check("Voltage" in ch and "Current" in ch and "Temp" in ch,
           f"tab channels {ch}")
    print("PASS  test_arduino_tab_detect")


def test_labeled_parse():
    ch = ["Voltage", "Current"]
    vals = parse_line("Voltage:3.14 Current:1.20", "labeled", ch)
    _check(vals is not None, "labeled parse returned values")
    _check(abs(vals[0] - 3.14) < 1e-6, f"Voltage {vals[0]}")
    _check(abs(vals[1] - 1.20) < 1e-6, f"Current {vals[1]}")
    # Missing key → None
    _check(parse_line("Voltage:3.14", "labeled", ch) is None,
           "labeled missing key → None")
    print("PASS  test_labeled_parse")


# ── 3. CSV with header ────────────────────────────────────────────────────────

def test_csv_header_detect():
    lines = ["voltage,current,temp", "3.14,1.2,25.3"]
    fmt, ch = detect_format(lines)
    _check(fmt == "csv", f"CSV+header detected (got '{fmt}')")
    _check(ch == ["voltage", "current", "temp"], f"CSV header names {ch}")
    print("PASS  test_csv_header_detect")


def test_csv_numeric_detect():
    lines = ["3.14,1.2,25.3", "3.20,1.1,25.1"]
    fmt, ch = detect_format(lines)
    _check(fmt == "csv", f"CSV numeric detected (got '{fmt}')")
    _check(ch == ["ch0", "ch1", "ch2"], f"CSV auto names {ch}")
    print("PASS  test_csv_numeric_detect")


def test_csv_parse():
    ch = ["ch0", "ch1", "ch2"]
    vals = parse_line("3.14,1.2,25.3", "csv", ch)
    _check(vals is not None, "CSV parse returned values")
    _check(abs(vals[0] - 3.14) < 1e-6, f"ch0 {vals[0]}")
    _check(abs(vals[2] - 25.3) < 1e-6, f"ch2 {vals[2]}")
    # Wrong column count → None
    _check(parse_line("3.14,1.2", "csv", ch) is None,
           "CSV column mismatch → None")
    print("PASS  test_csv_parse")


# ── 4. Debug/garbage lines ignored ───────────────────────────────────────────

def test_garbage_skipped():
    ch = ["Voltage", "Current"]
    garbage_lines = [
        "",
        "   ",
        "WARN: sensor timeout",
        "Connecting to WiFi...",
        "IP: 192.168.1.50",
        "12345",          # single number, not enough columns
    ]
    for g in garbage_lines:
        result = parse_line(g, "labeled", ch)
        _check(result is None, f"garbage skipped: {g!r}")
    print("PASS  test_garbage_skipped")


# ── 5. build_synthetic_metadata ──────────────────────────────────────────────

def test_build_metadata():
    meta = build_synthetic_metadata("usb:///dev/ttyUSB0", ["voltage", "current"], "labeled")
    _check(isinstance(meta, DeviceMetadataV2), "returns DeviceMetadataV2")
    _check(meta.num_channels == 2, f"num_channels == 2 (got {meta.num_channels})")
    _check(meta.firmware_version == "auto-parse", "firmware_version tag")
    _check(meta.device_name.startswith("Auto"), f"device_name {meta.device_name}")
    _check(meta.channels[0].name == "voltage", "channel 0 name")
    _check(meta.channels[1].name == "current", "channel 1 name")
    _check(math.isnan(meta.channels[0].range_min), "range_min is nan")
    print("PASS  test_build_metadata")


# ── 6. TelemetryStreamer loopback integration ─────────────────────────────────

def test_telemetry_streamer_loopback():
    """Full pipeline: TelemetryStreamer with a mock serial port."""
    import unittest.mock as _mock

    from PySide6.QtWidgets import QApplication
    from engine.telemetry_stream import TelemetryStreamer

    qapp = QApplication.instance() or QApplication(sys.argv)

    # Build a pre-loaded chunk list to feed into the mock serial.
    chunks = []
    for i in range(10):
        v = 3.3 + 0.1 * i
        line = f"Voltage:{v:.3f}\tCurrent:{1.0 + 0.05 * i:.3f}\n"
        chunks.append(line.encode())
    chunks.append(b"")   # sentinel — causes read to block (handled by sleep)

    class _MockSerial:
        def __init__(self, *a, **kw):
            self._chunks = list(chunks)
            self._idx = 0
        def read(self, n):
            if self._idx < len(self._chunks):
                chunk = self._chunks[self._idx]
                self._idx += 1
                if not chunk:
                    time.sleep(0.05)
                return chunk
            time.sleep(0.05)
            return b""
        def close(self): pass
        def cancel_read(self): pass
        def reset_input_buffer(self): pass
        def flush(self): pass
        def write(self, data): return len(data)

    meta_received: list = []
    stopped: list = []

    with _mock.patch("engine.telemetry_stream.serial.Serial", _MockSerial):
        streamer = TelemetryStreamer("/dev/mock", baud=115200)
        streamer.metadata_ready.connect(lambda p, m: meta_received.append(m))
        streamer.stream_stopped.connect(lambda p, r: stopped.append(r))
        streamer.start()

        deadline = time.monotonic() + 6.0
        while not meta_received and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.05)

        streamer.stop()
        _wait_signal(stopped, qapp, timeout=3.0)
        streamer.wait(2000)

    _check(bool(meta_received), "metadata_ready emitted")
    if meta_received:
        meta = meta_received[0]
        _check(meta.num_channels == 2, f"2 channels detected (got {meta.num_channels})")
        ch_names = [c.name for c in meta.channels]
        _check("Voltage" in ch_names, f"Voltage channel present: {ch_names}")
        _check("Current" in ch_names, f"Current channel present: {ch_names}")
    _check(bool(stopped), "stream_stopped emitted")
    print("PASS  test_telemetry_streamer_loopback")


def _wait_signal(flag: list, app, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not flag and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)


# ── 7. TCP streamer integration ───────────────────────────────────────────────

def test_tcp_streamer():
    """Full pipeline: TcpStreamer reads from a local mock TCP server."""
    import socket as _socket

    from PySide6.QtWidgets import QApplication
    from engine.tcp_stream import TcpStreamer

    qapp = QApplication.instance() or QApplication(sys.argv)

    # Start a mock TCP server that sends CSV data.
    server = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    server.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    host, port = server.getsockname()

    def _serve():
        try:
            conn, _ = server.accept()
            conn.settimeout(2.0)
            for i in range(20):
                d = 10.0 + i * 0.5
                line = f'{{"Distance_cm":{d:.2f}}}\n'
                conn.sendall(line.encode())
                time.sleep(0.05)
            conn.close()
        except Exception:
            pass
        finally:
            server.close()

    srv_thread = threading.Thread(target=_serve, daemon=True)
    srv_thread.start()

    meta_received: list = []
    stopped: list = []
    streamer = TcpStreamer(host, port)
    streamer.metadata_ready.connect(lambda p, m: meta_received.append(m))
    streamer.stream_stopped.connect(lambda p, r: stopped.append(r))
    streamer.start()

    deadline = time.monotonic() + 6.0
    while not meta_received and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.05)

    streamer.stop()
    _wait_signal(stopped, qapp, timeout=3.0)
    streamer.wait(2000)
    srv_thread.join(timeout=3)

    _check(bool(meta_received), "TCP metadata_ready emitted")
    if meta_received:
        meta = meta_received[0]
        ch_names = [c.name for c in meta.channels]
        _check("Distance_cm" in ch_names, f"Distance_cm channel: {ch_names}")
    _check(bool(stopped), "TCP stream_stopped emitted")
    print("PASS  test_tcp_streamer")


# ── runner ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_json_detect,
        test_json_parse,
        test_labeled_colon_detect,
        test_labeled_equals_detect,
        test_arduino_tab_detect,
        test_labeled_parse,
        test_csv_header_detect,
        test_csv_numeric_detect,
        test_csv_parse,
        test_garbage_skipped,
        test_build_metadata,
        test_telemetry_streamer_loopback,
        test_tcp_streamer,
    ]
    passed = failed = 0
    for fn in tests:
        try:
            fn()
            passed += 1
        except Exception as exc:
            print(f"FAIL  {fn.__name__}: {exc}")
            failed += 1

    print(f"\n{passed} passed, {failed} failed.")
    sys.exit(0 if failed == 0 else 1)
