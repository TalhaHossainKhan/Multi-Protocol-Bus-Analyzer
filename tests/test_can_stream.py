"""CAN bus streamer self-tests — virtual interface only, no hardware required.

Run with:
    python tests/test_can_stream.py
or:
    python -m pytest tests/test_can_stream.py -v

All tests use python-can's built-in virtual bus.  Two Bus objects on the same
channel name exchange messages in-process, so no USB adapter is needed.

Tests:
  1. Raw mode — CanStreamer receives frames without a DBC file.
  2. DBC decode — signals are decoded and match expected values.
  3. Bad DBC path — handshake_failed is emitted, streamer exits cleanly.
  4. Stop/cleanup — stream_stopped("user_stop") emitted when stop() called.
  5. Device path format — can_device_path() returns the expected URI.
  6. SessionManager integration — add_can_device() allocates buffer and wires signals.
  7. Connection screen — CAN Bus tab is present as the 4th tab (index 3 of 5).
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Dependency check ─────────────────────────────────────────────────────────

try:
    import can as _can
    import cantools as _cantools
except ImportError as _e:
    print(f"SKIP: {_e}\nInstall with:  pip install python-can cantools")
    sys.exit(0)

from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication(sys.argv)

from engine.can_stream import CanStreamer, can_device_path
from engine.buffer import MultiChannelRingBuffer

# Unique channel prefix so parallel test runs don't collide.
_CH = f"daq_test_{os.getpid()}"

_MINIMAL_DBC = """\
VERSION ""
NS_ :
BS_:
BU_:
BO_ 256 TestMsg: 4 Vector__XXX
 SG_ Voltage : 0|16@1+ (0.1,0) [0|6553.5] "V" Vector__XXX
 SG_ Current : 16|16@1+ (0.01,0) [0|655.35] "A" Vector__XXX
"""


# ── Helpers ──────────────────────────────────────────────────────────────────

def _send_later(channel: str, messages: list, delay: float = 0.12) -> threading.Thread:
    """Send CAN frames on the virtual bus from a background thread."""
    def _worker():
        time.sleep(delay)
        bus = _can.Bus(interface="virtual", channel=channel)
        for msg in messages:
            bus.send(msg)
        bus.shutdown()

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return t


def _wait_flag(flag: list, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while not flag and time.monotonic() < deadline:
        _app.processEvents()
        time.sleep(0.02)
    return bool(flag)


# ── Tests ────────────────────────────────────────────────────────────────────

def test_1_raw_mode():
    """CanStreamer receives frames on virtual bus without a DBC file."""
    meta_received: list = []
    stopped: list = []

    streamer = CanStreamer(interface="virtual", channel=_CH + "_raw",
                           dbc_path=None, bitrate=500_000)
    streamer.metadata_ready.connect(lambda _p, m: meta_received.append(m))
    streamer.stream_stopped.connect(lambda _p, r: stopped.append(r))
    streamer.start()

    frames = [
        _can.Message(arbitration_id=0x100, data=[i, 0, 0, 0], is_extended_id=False)
        for i in range(5)
    ]
    sender = _send_later(_CH + "_raw", frames)

    assert _wait_flag(meta_received), "metadata_ready not emitted"

    time.sleep(0.3)
    streamer.stop()
    _wait_flag(stopped, timeout=3.0)   # process Qt events so queued signal arrives
    streamer.wait(1000)
    sender.join(timeout=2)

    assert stopped, "stream_stopped not emitted"
    assert streamer._buffer is not None, "ring buffer was not created"
    print("PASS  test_1_raw_mode")


def test_2_dbc_signal_decode():
    """CanStreamer decodes DBC signals and ring buffer rows match signal order."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".dbc", delete=False) as f:
        f.write(_MINIMAL_DBC)
        dbc_path = f.name

    try:
        meta_received: list = []
        streamer = CanStreamer(interface="virtual", channel=_CH + "_dbc",
                               dbc_path=dbc_path, bitrate=500_000)
        streamer.metadata_ready.connect(lambda _p, m: meta_received.append(m))
        streamer.start()

        assert _wait_flag(meta_received), "metadata_ready not emitted"
        meta = meta_received[0]
        ch_names = [ch.name for ch in meta.channels]
        assert "Voltage" in ch_names, f"Expected Voltage channel, got: {ch_names}"
        assert "Current" in ch_names, f"Expected Current channel, got: {ch_names}"

        # Encode Voltage=12.5 V (raw=125) and Current=3.50 A (raw=350).
        raw_v, raw_i = 125, 350
        data = [
            raw_v & 0xFF, (raw_v >> 8) & 0xFF,
            raw_i & 0xFF, (raw_i >> 8) & 0xFF,
        ]
        msg = _can.Message(arbitration_id=256, data=data, is_extended_id=False)
        sender = _send_later(_CH + "_dbc", [msg])

        time.sleep(0.4)
        streamer.stop()
        streamer.wait(3000)
        sender.join(timeout=2)

        buf = streamer._buffer
        assert buf is not None
        snapshot = buf.read_latest(10)
        assert snapshot.shape[1] > 0, "No samples in ring buffer after DBC decode"

        # Row 0 = Voltage, row 1 = Current (DBC signal order, no timestamp injected
        # because no session_clock was set).
        v_idx = ch_names.index("Voltage")
        i_idx = ch_names.index("Current")
        last_v = snapshot[v_idx, -1]
        last_i = snapshot[i_idx, -1]
        assert abs(last_v - 12.5) < 0.05, f"Voltage mismatch: {last_v}"
        assert abs(last_i - 3.50) < 0.05, f"Current mismatch: {last_i}"

        print("PASS  test_2_dbc_signal_decode")
    finally:
        os.unlink(dbc_path)


def test_3_bad_dbc_path():
    """CanStreamer emits handshake_failed for a nonexistent DBC path."""
    failures: list = []
    streamer = CanStreamer(interface="virtual", channel=_CH + "_baddbc",
                           dbc_path="/nonexistent/path/no_file.dbc",
                           bitrate=500_000)
    streamer.handshake_failed.connect(lambda _p, err: failures.append(err))
    streamer.start()
    _wait_flag(failures, timeout=3.0)  # process events so queued signal arrives
    streamer.wait(1000)

    assert failures, "handshake_failed was not emitted for missing DBC"
    print("PASS  test_3_bad_dbc_path")


def test_4_stop_clean():
    """CanStreamer stops cleanly and emits stream_stopped('user_stop')."""
    stopped: list = []
    meta_received: list = []

    streamer = CanStreamer(interface="virtual", channel=_CH + "_stop",
                           dbc_path=None, bitrate=500_000)
    streamer.metadata_ready.connect(lambda _p, m: meta_received.append(m))
    streamer.stream_stopped.connect(lambda _p, r: stopped.append(r))
    streamer.start()

    assert _wait_flag(meta_received), "metadata_ready not emitted"

    streamer.stop()
    _wait_flag(stopped, timeout=3.0)   # process events so queued signal arrives
    streamer.wait(1000)

    assert stopped, "stream_stopped not emitted"
    assert stopped[0] == "user_stop", f"Expected 'user_stop', got: {stopped[0]!r}"
    print("PASS  test_4_stop_clean")


def test_5_device_path_format():
    """can_device_path() returns the canonical URI."""
    assert can_device_path("pcan", "PCAN_USBBUS1") == "can://pcan/PCAN_USBBUS1"
    assert can_device_path("socketcan", "can0") == "can://socketcan/can0"
    assert can_device_path("virtual", "test") == "can://virtual/test"
    print("PASS  test_5_device_path_format")


def test_6_session_manager_integration():
    """SessionManager.add_can_device() starts CanStreamer and wires all signals."""
    from engine.session import SessionManager

    sm = SessionManager()
    meta_received: list = []
    sm.device_ready.connect(lambda path, meta: meta_received.append((path, meta)))

    session = sm.add_can_device(
        interface="virtual",
        channel=_CH + "_sm",
        dbc_path=None,
        bitrate=500_000,
    )

    assert _wait_flag(meta_received), "device_ready not emitted from SessionManager"
    path, meta = meta_received[0]
    assert path.startswith("can://virtual/"), f"Unexpected path: {path}"
    assert meta.num_channels >= 1

    sm.remove_device(path)
    print("PASS  test_6_session_manager_integration")


def test_7_connection_screen_can_tab():
    """ConnectionScreen renders with a 'CAN Bus' tab at index 3."""
    from engine.port_scanner import PortScanner
    from ui.connection_screen import ConnectionScreen

    scanner = PortScanner()
    screen = ConnectionScreen(scanner)

    tab_count = screen._tabs.count()
    tab_labels = [screen._tabs.tabText(i) for i in range(tab_count)]

    assert tab_count == 5, f"Expected 5 tabs, got {tab_count}: {tab_labels}"
    assert "CAN Bus" in tab_labels, f"CAN Bus tab not found: {tab_labels}"
    assert tab_labels.index("CAN Bus") == 3, "CAN Bus should be the 4th tab"

    # Switch to CAN tab — Connect button should enable immediately (virtual
    # is pre-selected with test_channel auto-filled).
    screen._tabs.setCurrentIndex(3)
    _app.processEvents()
    assert screen.btn_connect.isEnabled(), \
        "Connect button should be enabled after switching to CAN tab"

    screen.close()
    print("PASS  test_7_connection_screen_can_tab")


# ── Runner ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_1_raw_mode,
        test_2_dbc_signal_decode,
        test_3_bad_dbc_path,
        test_4_stop_clean,
        test_5_device_path_format,
        test_6_session_manager_integration,
        test_7_connection_screen_can_tab,
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
