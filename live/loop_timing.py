"""Adaptive polling and tranche scheduling for the live executor loop."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional, Sequence

from live_config import LiveConfig
from mbh_simulator import OptionQuote, StrategyConfig, effective_entry_end, is_entry_time


def _entry_minutes(config: StrategyConfig, *, entry_end) -> List[int]:
    start = config.entry_start.hour * 60 + config.entry_start.minute
    end = entry_end.hour * 60 + entry_end.minute
    interval = config.entry_interval_minutes
    return list(range(start, end + 1, interval))


def next_entry_datetime(now: datetime, config: StrategyConfig) -> Optional[datetime]:
    """Next tranche entry clock time on today's calendar (may be now if on boundary)."""
    today = now.date()
    entry_end = effective_entry_end(now, config)
    for minute in _entry_minutes(config, entry_end=entry_end):
        hour, minute_of_hour = divmod(minute, 60)
        candidate = datetime(today.year, today.month, today.day, hour, minute_of_hour)
        if candidate >= now.replace(second=0, microsecond=0):
            if candidate.time() <= entry_end:
                return candidate
    return None


def seconds_until_next_tranche(now: datetime, config: StrategyConfig) -> Optional[float]:
    nxt = next_entry_datetime(now, config)
    if nxt is None:
        return None
    return max((nxt - now).total_seconds(), 0.0)


def any_near_stop(
    open_spreads: Sequence,
    lookup: dict,
    live: LiveConfig,
) -> bool:
    for spread in open_spreads:
        if spread.stopped or spread.closed:
            continue
        sq = lookup.get((spread.candidate.short_type, spread.candidate.short_strike))
        if sq is None or sq.ask <= 0 or spread.stop_price <= 0:
            continue
        threshold = spread.stop_price * live.stop_near_fraction
        if sq.ask >= threshold:
            return True
    return False


def adaptive_sleep_seconds(
    *,
    live: LiveConfig,
    now: datetime,
    open_spreads: Sequence,
    quotes: Sequence[OptionQuote],
    config: StrategyConfig,
) -> float:
    """Phase 3: fast when at risk, idle until next tranche when flat."""
    if not live.use_adaptive_polling:
        return live.poll_seconds

    lookup = {(q.option_type, q.strike): q for q in quotes}
    if any_near_stop(open_spreads, lookup, live):
        return live.poll_seconds_near_stop

    active = [s for s in open_spreads if not s.closed]
    if active:
        return live.poll_seconds_active

    secs = seconds_until_next_tranche(now, config)
    if secs is None:
        return live.poll_seconds_max_idle

    if secs <= live.pre_tranche_wake_seconds:
        return live.poll_seconds_pre_tranche

    idle = secs - live.pre_tranche_wake_seconds
    return min(max(idle, live.poll_seconds_pre_tranche), live.poll_seconds_max_idle)


def should_fire_tranche(
    now: datetime,
    config: StrategyConfig,
    traded_tranches: set,
) -> bool:
    key = (now.hour, now.minute)
    return is_entry_time(now, config) and key not in traded_tranches
