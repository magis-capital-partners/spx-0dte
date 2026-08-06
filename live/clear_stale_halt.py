"""Operator one-shot clear for sticky stale-data entry halts.

Create ``data/live/<date>/CLEAR_STALE_HALT`` then restart the executor.
Only clears stale-data halt reasons. Never clears flatten, daily-loss,
account, kill, or other governor state.

Both stale-data halts are clearable:
  ``stale_quotes``      — the option chain stopped updating.
  ``stale_underlying``  — the SPX spot stream stopped updating.

Neither carries a risk implication once the feed is healthy again, unlike a
P&L or flatten halt. ``stale_underlying`` was originally omitted here, which
left a tripped spot-stream halt with no operator path to resume: it is not in
the flatten family, is not spelled ``stale_quotes``, and is re-derived from
fills.jsonl on restart, so it latched for the rest of the session.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
LIVE_DIR = ROOT / "data" / "live"

CLEARABLE_STALE_REASONS = frozenset({"stale_quotes", "stale_underlying"})


def clear_stale_halt_path(today: str, *, live_dir: Path = LIVE_DIR) -> Path:
    return live_dir / today / "CLEAR_STALE_HALT"


def consume_clear_stale_halt(
    today: str,
    *,
    live_dir: Path = LIVE_DIR,
) -> Optional[Path]:
    """If the operator clear file exists, return its path and leave it in place.

    Caller decides whether clearing is safe, logs the event, then deletes.
    """
    path = clear_stale_halt_path(today, live_dir=live_dir)
    return path if path.is_file() else None


def filter_cleared_stale_reasons(halt_reasons: list[str] | tuple[str, ...] | set[str]) -> list[str]:
    """Return the subset of halt reasons this operator clear may remove."""
    return sorted(reason for reason in halt_reasons if reason in CLEARABLE_STALE_REASONS)
