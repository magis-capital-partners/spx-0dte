"""Sizing policies for overnight Calmar improvement sweeps."""
from __future__ import annotations

from datetime import time
from typing import Deque, Optional, Sequence

from mbh_simulator import SignalSnapshot, StrategyConfig
from profiles import Schedule
from time_of_day_sizing_runner import TimeOfDaySizePolicy
from vix_sizing_policies import VixElevatedSkipPolicy, _apply_max_contracts, ddq_vix_scale


class VixElevatedSkipPolicyExt(VixElevatedSkipPolicy):
    """Extended VIX policy with optional morning-only upscale and low-VIX downsize."""

    def __init__(
        self,
        schedule: Schedule,
        *,
        elevated_scale: float = 1.25,
        elevated_min: float = 25.0,
        elevated_max: float = 35.0,
        skip_above: float = 35.0,
        max_contracts: Optional[int] = 48,
        elevated_morning_end: Optional[time] = None,
        low_vix_downsize_min: float = 0.0,
        low_vix_downsize_max: float = 0.0,
        low_vix_scale: float = 1.0,
        late_vix_off_threshold: float = 0.0,
        late_vix_off_after: Optional[time] = None,
    ) -> None:
        super().__init__(
            schedule,
            elevated_scale=elevated_scale,
            elevated_min=elevated_min,
            elevated_max=elevated_max,
            skip_above=skip_above,
            max_contracts=max_contracts,
        )
        self.elevated_morning_end = elevated_morning_end
        self.low_vix_downsize_min = low_vix_downsize_min
        self.low_vix_downsize_max = low_vix_downsize_max
        self.low_vix_scale = low_vix_scale
        self.late_vix_off_threshold = late_vix_off_threshold
        self.late_vix_off_after = late_vix_off_after

    def contracts(self, signal: Optional[SignalSnapshot], config: StrategyConfig) -> int:
        if signal is not None and signal.vix is not None:
            vix = signal.vix
            if self.late_vix_off_threshold > 0 and self.late_vix_off_after is not None:
                if vix > self.late_vix_off_threshold and signal.timestamp.time() >= self.late_vix_off_after:
                    return 0
        base = super().contracts(signal, config)
        if base <= 0 or signal is None or signal.vix is None:
            return base
        vix = signal.vix
        if self.elevated_morning_end is not None:
            if not (self.elevated_min <= vix <= self.elevated_max):
                pass
            elif signal.timestamp.time() >= self.elevated_morning_end:
                # Undo elevated upscale outside morning window (recompute from tod only).
                tod_only = TimeOfDaySizePolicy(self.schedule).contracts(signal, config)
                if vix > self.skip_above:
                    return 0
                return _apply_max_contracts(tod_only, self.max_contracts)
        if (
            self.low_vix_scale != 1.0
            and self.low_vix_downsize_max > self.low_vix_downsize_min
            and self.low_vix_downsize_min <= vix < self.low_vix_downsize_max
        ):
            return max(0, round(base * self.low_vix_scale))
        return base


class DdqVixTodPolicy(TimeOfDaySizePolicy):
    """Time-of-day + DDQ VIX tiers (skip <12, downsize low, half >35)."""

    def __init__(self, schedule: Schedule, *, max_contracts: Optional[int] = 48) -> None:
        super().__init__(schedule)
        self.max_contracts = max_contracts

    def contracts(self, signal: Optional[SignalSnapshot], config: StrategyConfig) -> int:
        base = super().contracts(signal, config)
        if signal is None or signal.vix is None:
            return _apply_max_contracts(base, self.max_contracts)
        scaled = max(0, round(base * ddq_vix_scale(signal.vix)))
        return _apply_max_contracts(scaled, self.max_contracts)


class RegimeDownsizePolicy(TimeOfDaySizePolicy):
    """Scale size when trailing 5-day stop rate exceeds threshold."""

    def __init__(
        self,
        schedule: Schedule,
        trailing_stop: Deque[float],
        *,
        threshold: float = 0.25,
        scale: float = 0.5,
        max_contracts: Optional[int] = 48,
    ) -> None:
        super().__init__(schedule)
        self.trailing_stop = trailing_stop
        self.threshold = threshold
        self.scale = scale
        self.max_contracts = max_contracts

    def contracts(self, signal: Optional[SignalSnapshot], config: StrategyConfig) -> int:
        base = super().contracts(signal, config)
        if len(self.trailing_stop) >= 5:
            avg = sum(self.trailing_stop) / len(self.trailing_stop)
            if avg > self.threshold:
                base = max(0, round(base * self.scale))
        return _apply_max_contracts(base, self.max_contracts)


class PriorDayLossSkipPolicy:
    """Wrap a policy; skip entries after a large prior-day loss."""

    def __init__(self, inner, *, prior_day_pnl: float, account_equity: float, loss_pct: float) -> None:
        self.inner = inner
        self.prior_day_pnl = prior_day_pnl
        self.account_equity = account_equity
        self.loss_pct = loss_pct

    def contracts(self, signal: Optional[SignalSnapshot], config: StrategyConfig) -> int:
        if self.prior_day_pnl <= -self.account_equity * self.loss_pct:
            return 0
        return self.inner.contracts(signal, config)


class WeekdaySkipPolicy:
    """Wrap a policy; skip entries on selected weekdays (0=Mon .. 6=Sun)."""

    def __init__(self, inner, *, skip_weekdays: Sequence[int]) -> None:
        self.inner = inner
        self.skip_weekdays = set(skip_weekdays)

    def contracts(self, signal: Optional[SignalSnapshot], config: StrategyConfig) -> int:
        if signal is not None and signal.timestamp.weekday() in self.skip_weekdays:
            return 0
        return self.inner.contracts(signal, config)
