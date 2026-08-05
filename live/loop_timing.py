"""Adaptive polling and tranche scheduling for the live executor loop."""
from __future__ import annotations

from datetime import datetime, time as dt_time, timedelta
from typing import List, Optional, Sequence

from live_config import LiveConfig
from mbh_simulator import OptionQuote, StrategyConfig, effective_entry_end, is_entry_time


def _entry_minutes(config: StrategyConfig, *, entry_end) -> List[int]:
    start = config.entry_start.hour * 60 + config.entry_start.minute
    end = entry_end.hour * 60 + entry_end.minute
    interval = config.entry_interval_minutes
    return list(range(start, end + 1, interval))


def seconds_until_market_open(
    now: datetime,
    *,
    session_open: dt_time,
    lead_seconds: float = 0.0,
) -> float:
    """Seconds to idle before market data is worth starting; 0 once inside the window.

    Only today's open is considered, so a session launched after the close does
    not park the executor until tomorrow.
    """
    target = datetime.combine(now.date(), session_open) - timedelta(
        seconds=max(lead_seconds, 0.0)
    )
    return max((target - now).total_seconds(), 0.0)


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
    second = now.second + now.microsecond / 1_000_000.0
    sample_start = max(
        live.signal_sample_offset_seconds - live.signal_sample_window_seconds,
        0.0,
    )
    sample_deadline = (
        live.signal_sample_offset_seconds + live.signal_sample_max_wait_seconds
    )
    in_sample_window = sample_start <= second <= sample_deadline
    if in_sample_window:
        sample_cap = live.signal_sample_poll_seconds
    elif second < sample_start:
        sample_cap = max(sample_start - second, live.signal_sample_poll_seconds)
    else:
        sample_cap = max(60.0 - second + sample_start, live.signal_sample_poll_seconds)

    if not live.use_adaptive_polling:
        return min(live.poll_seconds, sample_cap)

    lookup = {(q.option_type, q.strike): q for q in quotes}
    if any_near_stop(open_spreads, lookup, live):
        return min(live.poll_seconds_near_stop, sample_cap)

    active = [s for s in open_spreads if not s.closed]
    if active:
        return min(live.poll_seconds_active, sample_cap)

    secs = seconds_until_next_tranche(now, config)
    if secs is None:
        return min(live.poll_seconds_max_idle, sample_cap)

    if secs <= live.pre_tranche_wake_seconds:
        return min(live.poll_seconds_pre_tranche, sample_cap)

    idle = secs - live.pre_tranche_wake_seconds
    return min(
        max(idle, live.poll_seconds_pre_tranche),
        live.poll_seconds_max_idle,
        sample_cap,
    )


def should_fire_tranche(
    now: datetime,
    config: StrategyConfig,
    traded_tranches: set,
) -> bool:
    key = (now.hour, now.minute)
    return is_entry_time(now, config) and key not in traded_tranches
