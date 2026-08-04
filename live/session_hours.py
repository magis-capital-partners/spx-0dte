"""Shared regular-session cutoffs for local monitoring helpers."""
from __future__ import annotations

from datetime import datetime, time
from typing import Optional

MONITOR_STOP_TIME = time(16, 1)


def parse_monitor_stop_time(value: str) -> Optional[time]:
    """Parse ``HH:MM`` or accept ``off`` to keep a helper running."""
    raw = str(value or "").strip().lower()
    if raw in {"", "off", "none", "disabled"}:
        return None
    try:
        return datetime.strptime(raw, "%H:%M").time()
    except ValueError as exc:
        raise ValueError("stop time must be HH:MM or 'off'") from exc


def monitor_stop_reached(
    *,
    now: Optional[datetime] = None,
    stop_at: Optional[time] = MONITOR_STOP_TIME,
) -> bool:
    """Return whether a local session monitor should stop for the day."""
    return bool(stop_at is not None and (now or datetime.now()).time() >= stop_at)
