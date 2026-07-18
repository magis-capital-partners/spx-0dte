"""IB disconnect detection and reconnect with backoff."""
from __future__ import annotations

import time as _time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional


@dataclass
class ReconnectOutcome:
    connected: bool
    attempts: int
    elapsed_seconds: float
    reason: str = ""


def ib_is_connected(ib: Any) -> bool:
    if ib is None:
        return False
    try:
        return bool(ib.isConnected())
    except Exception:
        return False


def reconnect_ib(
    ib: Any,
    *,
    host: str,
    port: int,
    client_id: int,
    max_seconds: float = 120.0,
    initial_backoff: float = 2.0,
    max_backoff: float = 30.0,
    on_attempt: Optional[Callable[[int, float], None]] = None,
    sleep_fn: Optional[Callable[[float], None]] = None,
) -> ReconnectOutcome:
    """Try to restore an IB socket connection with exponential backoff.

    Returns when connected or when ``max_seconds`` is exhausted.
    """
    sleep = sleep_fn or _time.sleep
    started = _time.time()
    attempts = 0
    backoff = max(0.5, initial_backoff)

    if ib_is_connected(ib):
        return ReconnectOutcome(connected=True, attempts=0, elapsed_seconds=0.0)

    while True:
        elapsed = _time.time() - started
        if elapsed >= max_seconds:
            return ReconnectOutcome(
                connected=False,
                attempts=attempts,
                elapsed_seconds=elapsed,
                reason="reconnect_budget_exhausted",
            )
        attempts += 1
        if on_attempt is not None:
            on_attempt(attempts, backoff)
        try:
            try:
                ib.disconnect()
            except Exception:
                pass
            sleep(0.25)
            ib.connect(host, port, clientId=client_id)
            if ib_is_connected(ib):
                return ReconnectOutcome(
                    connected=True,
                    attempts=attempts,
                    elapsed_seconds=_time.time() - started,
                )
        except Exception as exc:
            last_reason = repr(exc)
        else:
            last_reason = "connect_returned_disconnected"

        remaining = max_seconds - (_time.time() - started)
        if remaining <= 0:
            return ReconnectOutcome(
                connected=False,
                attempts=attempts,
                elapsed_seconds=_time.time() - started,
                reason=last_reason,
            )
        sleep(min(backoff, remaining, max_backoff))
        backoff = min(max_backoff, backoff * 2.0)


def format_reconnect_banner(outcome: ReconnectOutcome) -> str:
    ts = datetime.now().isoformat()
    if outcome.connected:
        return (
            f"[{ts}] IB reconnected after {outcome.attempts} attempt(s) "
            f"in {outcome.elapsed_seconds:.1f}s"
        )
    return (
        f"[{ts}] IB reconnect FAILED after {outcome.attempts} attempt(s) "
        f"in {outcome.elapsed_seconds:.1f}s ({outcome.reason})"
    )
