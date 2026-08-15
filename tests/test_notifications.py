"""Tests for engine/notifications.py — Slack/Teams alarm webhooks.

All HTTP calls are mocked; no real network requests are made.
The daemon threads are given 200 ms to fire before assertions.
"""

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from engine.notifications import send_alarm_webhook, send_test_webhook


def _drain(seconds: float = 0.2) -> None:
    """Give daemon threads time to execute before checking assertions."""
    time.sleep(seconds)


# ── send_alarm_webhook ────────────────────────────────────────────────────────

def test_empty_url_is_noop():
    with patch("urllib.request.urlopen") as mock_open:
        send_alarm_webhook("", "Overheat", "temp", 95.0, 1.0)
        _drain()
        mock_open.assert_not_called()


def test_non_http_url_is_noop():
    with patch("urllib.request.urlopen") as mock_open:
        send_alarm_webhook("ftp://bad", "Alarm", "ch", 1.0, 0.0)
        _drain()
        mock_open.assert_not_called()


def test_posts_to_correct_url():
    with patch("urllib.request.urlopen") as mock_open:
        url = "https://hooks.slack.com/services/TTEST/BTEST/token"
        send_alarm_webhook(url, "Overheat", "temperature", 95.5, 100.0)
        _drain()
        mock_open.assert_called_once()
        req = mock_open.call_args[0][0]
        assert req.full_url == url


def test_payload_contains_alarm_name():
    captured = []

    def fake_open(req, timeout=None):
        captured.append(json.loads(req.data.decode()))

    with patch("urllib.request.urlopen", side_effect=fake_open):
        send_alarm_webhook(
            "https://hooks.slack.com/test",
            "MaxTemp", "temperature", 95.0, 100.0
        )
        _drain()

    assert captured, "No POST was made"
    assert "MaxTemp" in captured[0]["text"]


def test_payload_contains_channel_and_value():
    captured = []

    def fake_open(req, timeout=None):
        captured.append(json.loads(req.data.decode()))

    with patch("urllib.request.urlopen", side_effect=fake_open):
        send_alarm_webhook(
            "https://hooks.slack.com/test",
            "VoltageSpike", "voltage_mv", 3450.0, 0.0
        )
        _drain()

    assert "voltage_mv" in captured[0]["text"]
    assert "3450" in captured[0]["text"]


def test_payload_contains_device_name():
    captured = []

    def fake_open(req, timeout=None):
        captured.append(json.loads(req.data.decode()))

    with patch("urllib.request.urlopen", side_effect=fake_open):
        send_alarm_webhook(
            "https://hooks.slack.com/test",
            "Alarm", "ch", 1.0, 0.0, device_name="MotorESP32"
        )
        _drain()

    assert "MotorESP32" in captured[0]["text"]


def test_no_device_name_omitted_gracefully():
    captured = []

    def fake_open(req, timeout=None):
        captured.append(json.loads(req.data.decode()))

    with patch("urllib.request.urlopen", side_effect=fake_open):
        send_alarm_webhook("https://hooks.slack.com/test", "Alarm", "ch", 1.0, 0.0)
        _drain()

    assert captured  # did POST
    # No crash from missing device_name


def test_teams_compatible_payload_has_text_field():
    """Teams incoming webhooks render the 'text' field; blocks are optional."""
    captured = []

    def fake_open(req, timeout=None):
        captured.append(json.loads(req.data.decode()))

    with patch("urllib.request.urlopen", side_effect=fake_open):
        send_alarm_webhook(
            "https://yourorg.webhook.office.com/webhookb2/xxx",
            "Alarm", "ch", 1.0, 0.0
        )
        _drain()

    assert "text" in captured[0]
    assert "blocks" in captured[0]


def test_network_failure_is_silently_swallowed():
    """A failed POST must never raise — test session must continue."""
    with patch("urllib.request.urlopen", side_effect=OSError("network down")):
        # Should NOT raise
        send_alarm_webhook("https://hooks.slack.com/test", "Alarm", "ch", 1.0, 0.0)
        _drain()
    # Test passes if we reach here without exception


def test_timeout_failure_is_silently_swallowed():
    import urllib.error
    with patch("urllib.request.urlopen",
               side_effect=urllib.error.URLError("timed out")):
        send_alarm_webhook("https://hooks.slack.com/test", "Alarm", "ch", 1.0, 0.0)
        _drain()


def test_multiple_alarms_each_post_independently():
    call_count = [0]

    def fake_open(req, timeout=None):
        call_count[0] += 1

    with patch("urllib.request.urlopen", side_effect=fake_open):
        send_alarm_webhook("https://hooks.slack.com/test", "A1", "ch1", 1.0, 0.0)
        send_alarm_webhook("https://hooks.slack.com/test", "A2", "ch2", 2.0, 1.0)
        send_alarm_webhook("https://hooks.slack.com/test", "A3", "ch3", 3.0, 2.0)
        _drain(0.4)

    assert call_count[0] == 3


# ── send_test_webhook ─────────────────────────────────────────────────────────

def test_test_webhook_posts():
    with patch("urllib.request.urlopen") as mock_open:
        send_test_webhook("https://hooks.slack.com/test", "ESP32")
        _drain()
        mock_open.assert_called_once()


def test_test_webhook_empty_url_noop():
    with patch("urllib.request.urlopen") as mock_open:
        send_test_webhook("")
        _drain()
        mock_open.assert_not_called()


def test_test_webhook_payload_has_success_text():
    captured = []

    def fake_open(req, timeout=None):
        captured.append(json.loads(req.data.decode()))

    with patch("urllib.request.urlopen", side_effect=fake_open):
        send_test_webhook("https://hooks.slack.com/test")
        _drain()

    assert captured
    assert "connected" in captured[0]["text"].lower()
