"""Alarm webhook notifications — Slack / Microsoft Teams.

Sends a fire-and-forget POST using stdlib urllib only (no third-party deps).
A failed POST is silently swallowed so it never blocks or crashes the UI.

Incoming webhook setup
----------------------
Slack : Workspace Settings → Integrations → Incoming Webhooks → Add
Teams : Channel → Connectors → Incoming Webhook → Configure
        Copy the generated URL and paste it into Settings → Alarms → Webhook URL.

Both services accept the same payload format used here (Slack block-kit).
Teams renders the ``text`` field as plain markdown; it ignores ``blocks``.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from typing import Optional


def _post(url: str, payload: dict) -> None:
    """Blocking POST — run this on a daemon thread, never on the UI thread."""
    try:
        data = json.dumps(payload).encode("utf-8")
        req  = urllib.request.Request(
            url,
            data    = data,
            headers = {"Content-Type": "application/json"},
            method  = "POST",
        )
        urllib.request.urlopen(req, timeout=4)
    except Exception:
        pass   # swallow — a failed notification must never affect the test session


def send_alarm_webhook(
    url: str,
    alarm_name: str,
    channel_name: str,
    value: float,
    timestamp: float,
    device_name: str = "",
) -> None:
    """POST an alarm notification to a Slack or Teams incoming webhook URL.

    Non-blocking: spawns a daemon thread and returns immediately.
    Safe to call from any Qt slot.
    """
    if not url or not url.startswith("http"):
        return

    device_str = f"  ·  device: *{device_name}*" if device_name else ""
    text = (
        f":rotating_light:  *Alarm fired: {alarm_name}*\n"
        f"Channel `{channel_name}` = *{value:.4g}*{device_str}"
    )
    payload: dict = {
        # ``text`` is the Teams-compatible field; Slack also uses it as fallback.
        "text": text,
        # Slack block-kit for richer rendering on Slack clients.
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": text},
            }
        ],
    }
    threading.Thread(target=_post, args=(url, payload), daemon=True).start()


def send_test_webhook(url: str, device_name: str = "") -> None:
    """Send a test message to verify the webhook URL is working."""
    if not url or not url.startswith("http"):
        return
    device_str = f"  ·  device: *{device_name}*" if device_name else ""
    text = f":white_check_mark:  Multi-Protocol Bus Analyzer webhook connected successfully{device_str}"
    payload = {
        "text": text,
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
    }
    threading.Thread(target=_post, args=(url, payload), daemon=True).start()
