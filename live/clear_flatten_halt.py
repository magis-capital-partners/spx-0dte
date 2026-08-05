"""Operator one-shot clear for a recovered flatten/kill entry halt.

Create ``data/live/<date>/CLEAR_FLATTEN_HALT`` then restart the executor.

Exists for the case where the session was flattened by something other than a
real risk event — e.g. a watchdog KILL fired during a controlled restart — and
the operator wants to resume entries without rewriting the session audit log.

The executor only honours the file when it holds no open spreads *and* IB
reports no same-day SPXW risk; it fails closed otherwise. Only flatten-family
reasons are cleared, never P&L, account, stale-quote, or stop-count halts.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from session_recovery import FLATTEN_HALT_REASONS

ROOT = Path(__file__).resolve().parents[1]
LIVE_DIR = ROOT / "data" / "live"

CLEARABLE_FLATTEN_REASONS = FLATTEN_HALT_REASONS


def clear_flatten_halt_path(today: str, *, live_dir: Path = LIVE_DIR) -> Path:
    return live_dir / today / "CLEAR_FLATTEN_HALT"


def consume_clear_flatten_halt(
    today: str,
    *,
    live_dir: Path = LIVE_DIR,
) -> Optional[Path]:
    """If the operator clear file exists, return its path and leave it in place.

    Caller decides whether clearing is safe, logs the event, then deletes.
    """
    path = clear_flatten_halt_path(today, live_dir=live_dir)
    return path if path.is_file() else None


def filter_cleared_flatten_reasons(
    halt_reasons: list[str] | tuple[str, ...] | set[str],
) -> list[str]:
    """Return the subset of halt reasons this operator clear may remove."""
    return sorted(r for r in halt_reasons if r in CLEARABLE_FLATTEN_REASONS)
