"""Slack webhook alerts for live safety events.

Webhook URL from env ``SPX_SLACK_WEBHOOK_URL``. No-op when unset or disabled.

Delivery is asynchronous by default. ``notify_slack`` performs a blocking HTTP
POST and must never be called from the executor loop: a slow webhook stalls
stop management for up to ``timeout_sec`` while the book is short 0DTE gamma.
The loop uses ``maybe_notify_safety_event`` / ``notify_slack_async``, which
hand the message to a daemon worker thread and return in microseconds.

Standalone monitors that have no risk to manage (``watchdog.py``) may still
call ``notify_slack`` directly.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time as _time
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
    "entry_fault",
    "entry_poll_error",
    "stop_unconfirmed",
    "watchdog_alert",
})

# Bounded so a Slack outage cannot grow the executor's memory without limit.
# 128 safety events in one session already means the day has gone badly wrong.
_QUEUE_MAX = 128

_queue: "queue.Queue" = queue.Queue(maxsize=_QUEUE_MAX)
_worker: Optional[threading.Thread] = None
_worker_lock = threading.Lock()
# Guards _pending / _dropped and wakes flush() when the backlog drains.
_state_cv = threading.Condition()
_pending = 0
_dropped = 0


def slack_webhook_url() -> str:
    return (os.environ.get("SPX_SLACK_WEBHOOK_URL") or "").strip()


def notify_slack(
    text: str,
    *,
    enabled: bool = True,
    webhook_url: Optional[str] = None,
    timeout_sec: float = 5.0,
) -> bool:
    """Post a simple Slack message, blocking until delivered or timed out.

    Blocks for up to ``timeout_sec``. Never call this from the executor loop —
    use ``notify_slack_async``.
    """
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


def _drain_forever() -> None:
    global _pending
    while True:
        item = _queue.get()
        try:
            if item is None:  # shutdown sentinel
                return
            text, url, timeout_sec = item
            notify_slack(
                text, enabled=True, webhook_url=url, timeout_sec=timeout_sec,
            )
        except Exception:
            # A notifier must never take the session down.
            pass
        finally:
            with _state_cv:
                _pending -= 1
                _state_cv.notify_all()


def _ensure_worker() -> None:
    global _worker
    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            return
        # Daemon: a queued alert must never keep the process alive at shutdown.
        # Call flush() before exit to deliver what is still queued.
        _worker = threading.Thread(
            target=_drain_forever, name="slack-notify", daemon=True,
        )
        _worker.start()


def notify_slack_async(
    text: str,
    *,
    enabled: bool = True,
    webhook_url: Optional[str] = None,
    timeout_sec: float = 5.0,
) -> bool:
    """Queue a Slack message for background delivery. Returns True if queued.

    Safe on the executor hot path: resolves the webhook URL, enqueues, returns.
    A full queue drops the message and bumps ``dropped_count()`` rather than
    blocking the caller.
    """
    if not enabled:
        return False
    url = (webhook_url if webhook_url is not None else slack_webhook_url()).strip()
    if not url:
        return False
    _ensure_worker()
    global _dropped, _pending
    with _state_cv:
        try:
            _queue.put_nowait((text, url, timeout_sec))
        except queue.Full:
            _dropped += 1
            return False
        _pending += 1
        return True


def flush(timeout_sec: float = 5.0) -> bool:
    """Wait for queued alerts to drain. True if the backlog cleared in time.

    Call before process exit so the final flatten / kill_switch page is not
    dropped with the daemon thread.
    """
    deadline = _time.monotonic() + max(timeout_sec, 0.0)
    with _state_cv:
        while _pending > 0:
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                return _pending == 0
            _state_cv.wait(timeout=remaining)
        return True


def dropped_count() -> int:
    """Alerts discarded because the queue was full (observability only)."""
    with _state_cv:
        return _dropped


def format_safety_message(event: str, payload: Mapping[str, Any]) -> str:
    detail = {k: v for k, v in payload.items() if k not in {"event", "ts"}}
    bits = ", ".join(f"{k}={v}" for k, v in list(detail.items())[:12])
    return f"[spx-0dte] {event}" + (f" — {bits}" if bits else "")


def maybe_notify_safety_event(
    event_name: str,
    payload: Mapping[str, Any],
    *,
    enabled: bool = True,
    blocking: bool = False,
) -> bool:
    """Page the operator for a safety event. Non-blocking unless asked.

    Returns True when the message was queued (or delivered, if ``blocking``).
    """
    if event_name not in SAFETY_EVENTS:
        return False
    message = format_safety_message(event_name, payload)
    if blocking:
        return notify_slack(message, enabled=enabled)
    return notify_slack_async(message, enabled=enabled)
