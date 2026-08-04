"""Deterministic calendar-based sizing overlays for backtest and live parity."""
from __future__ import annotations

import calendar
from datetime import date, timedelta

from mbh_simulator import SignalSnapshot, StrategyConfig


def is_monthly_opex(day: date) -> bool:
    """Traditional monthly equity-options expiration: third Friday."""
    return day.weekday() == 4 and 15 <= day.day <= 21


def is_last_weekday_of_month(day: date) -> bool:
    """Calendar approximation; exchange holidays are handled by supplied session set."""
    if day.weekday() >= 5:
        return False
    nxt = day + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt.month != day.month


class CalendarAdjustedPolicy:
    """Apply OPEX/month-end multipliers on top of an existing policy.

    ``observed_sessions`` makes the last *observed* session exact for historical
    studies, including exchange holidays and data availability gaps.
    """
    def __init__(self, base_policy, observed_sessions: set[str] | None = None,
                 opex_multiplier: float = 2.0, month_end_multiplier: float = 0.5) -> None:
        self.base_policy = base_policy
        self.observed_sessions = observed_sessions or set()
        self.opex_multiplier = opex_multiplier
        self.month_end_multiplier = month_end_multiplier

    def contracts(self, signal: SignalSnapshot | None, config: StrategyConfig) -> int:
        contracts = self.base_policy.contracts(signal, config)
        if signal is None or contracts <= 0:
            return contracts
        day = signal.timestamp.date()
        key = day.isoformat()
        mult = 1.0
        if is_monthly_opex(day):
            mult *= self.opex_multiplier
        if self.observed_sessions:
            later = any(x[:7] == key[:7] and x > key for x in self.observed_sessions)
            if not later:
                mult *= self.month_end_multiplier
        elif is_last_weekday_of_month(day):
            mult *= self.month_end_multiplier
        return max(0, round(contracts * mult))
