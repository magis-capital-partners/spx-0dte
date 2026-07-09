"""VIX-aware sizing policies layered on time-of-day contract schedules."""
from __future__ import annotations

from typing import Optional

from mbh_simulator import DefaultSignalPolicy, SignalSnapshot, StrategyConfig
from time_of_day_sizing_runner import TimeOfDaySizePolicy
from profiles import Schedule


def ddq_vix_scale(vix: float) -> float:
    """DDQ-style tiers from DefaultSignalPolicy (no danger/model scaling)."""
    if vix < 12.0:
        return 0.0
    if vix < 15.0:
        return 0.8
    if vix < 16.0:
        return 0.9
    if vix > 35.0:
        return 0.5
    return 1.0


def _apply_max_contracts(contracts: int, max_contracts: Optional[int]) -> int:
    if max_contracts is None or max_contracts <= 0:
        return contracts
    return min(contracts, max_contracts)


class VixTimeOfDayPolicy(TimeOfDaySizePolicy):
    """Time-of-day sizing with optional VIX multiplier."""

    def __init__(self, schedule: Schedule, *, vix_mode: str = "none") -> None:
        super().__init__(schedule)
        self.vix_mode = vix_mode

    def contracts(self, signal: Optional[SignalSnapshot], config: StrategyConfig) -> int:
        base = super().contracts(signal, config)
        if signal is None or signal.vix is None:
            return base
        vix = signal.vix
        if self.vix_mode == "ddq":
            return max(0, round(base * ddq_vix_scale(vix)))
        if self.vix_mode == "skip_lt12":
            return 0 if vix < 12.0 else base
        if self.vix_mode == "skip_lt15":
            return 0 if vix < 15.0 else base
        if self.vix_mode == "skip_gt35":
            return 0 if vix > 35.0 else base
        if self.vix_mode == "half_lt15":
            return max(0, round(base * 0.5)) if vix < 15.0 else base
        if self.vix_mode == "half_lt17":
            return max(0, round(base * 0.5)) if vix < 17.0 else base
        if self.vix_mode == "tc_friction_lt15":
            # Thin premium + high fee share: skip lowest-VIX bucket entirely.
            return 0 if vix < 15.0 else base
        return base


class VixElevatedSkipPolicy(TimeOfDaySizePolicy):
    """Time-of-day sizing + skip VIX > threshold + optional elevated-band upscale.

    Matches live ``vix_session`` rules: no entries when VIX open > skip_above;
    multiply contracts by elevated_scale when skip_above >= VIX in [elevated_min, elevated_max].
    """

    def __init__(
        self,
        schedule: Schedule,
        *,
        elevated_min: float = 25.0,
        elevated_max: float = 35.0,
        elevated_scale: float = 1.25,
        skip_above: float = 35.0,
        max_contracts: Optional[int] = None,
    ) -> None:
        super().__init__(schedule)
        self.elevated_min = elevated_min
        self.elevated_max = elevated_max
        self.elevated_scale = elevated_scale
        self.skip_above = skip_above
        self.max_contracts = max_contracts

    def contracts(self, signal: Optional[SignalSnapshot], config: StrategyConfig) -> int:
        base = super().contracts(signal, config)
        if base <= 0:
            return 0
        if signal is not None and signal.vix is not None:
            vix = signal.vix
            if vix > self.skip_above:
                return 0
            if self.elevated_min <= vix <= self.elevated_max and self.elevated_scale != 1.0:
                base = max(0, round(base * self.elevated_scale))
        return _apply_max_contracts(base, self.max_contracts)


def build_production_vix_policy(
    schedule: Schedule,
    *,
    elevated_scale: float = 1.25,
    elevated_min: float = 25.0,
    elevated_max: float = 35.0,
    skip_above: float = 35.0,
    max_contracts: Optional[int] = 48,
) -> VixElevatedSkipPolicy:
    """Validated production policy: skip VIX>35, 1.25× upscale in 25–35 band."""
    return VixElevatedSkipPolicy(
        schedule,
        elevated_min=elevated_min,
        elevated_max=elevated_max,
        elevated_scale=elevated_scale,
        skip_above=skip_above,
        max_contracts=max_contracts,
    )


class VixDefaultPolicy(DefaultSignalPolicy):
    """Full DefaultSignalPolicy danger + DDQ VIX tiers on flat baseline."""

    def contracts(self, signal: Optional[SignalSnapshot], config: StrategyConfig) -> int:
        return super().contracts(signal, config)
