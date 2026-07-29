"""Slack webhook alerts for live safety events.

Webhook URL from env ``SPX_SLACK_WEBHOOK_URL``. No-op when unset or disabled.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Mapping, Optional

# Events that should page the operator when Slack is configured.
SAFETY_EVENTS = frozenset({
    "halt_entries",
    "flatten",
    "flatten_incomplete",
    "flatten_audit",
    "ib_disconnected",
    "ib_reconnect",
    "kill_switch",
    "native_stop_rejected",
    "error_flatten",
    "entry_poll_error",
    "stop_unconfirmed",
    "watchdog_alert",
})


def slack_webhook_url() -> str:
    return (os.environ.get("SPX_SLACK_WEBHOOK_URL") or "").strip()


def notify_slack(
    text: str,
    *,
    enabled: bool = True,
    webhook_url: Optional[str] = None,
    timeout_sec: float = 5.0,
) -> bool:
    """Post a simple Slack message. Returns True if delivered."""
    if not enabled:
        return False
    url = (webhook_url if webhook_url is not None else slack_webhook_url()).strip()
    if not url:
        return False
    payload = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            return 200 <= int(getattr(resp, "status", 200) or 200) < 300
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False


def format_safety_message(event: str, payload: Mapping[str, Any]) -> str:
    detail = {k: v for k, v in payload.items() if k not in {"event", "ts"}}
    bits = ", ".join(f"{k}={v}" for k, v in list(detail.items())[:12])
    return f"[spx-0dte] {event}" + (f" — {bits}" if bits else "")


def maybe_notify_safety_event(
    event_name: str,
    payload: Mapping[str, Any],
    *,
    enabled: bool = True,
) -> bool:
    if event_name not in SAFETY_EVENTS:
        return False
    return notify_slack(format_safety_message(event_name, payload), enabled=enabled)
