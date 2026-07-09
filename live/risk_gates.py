"""Live risk gates that mirror selected backtest checks in ``mbh_simulator``."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, Mapping, MutableMapping, Protocol, Sequence

from mbh_simulator import StrategyConfig


class _StoppedSideSource(Protocol):
    @property
    def side(self) -> str:
        ...


def _stopped_side(item: object) -> str:
    side = getattr(item, "side", None)
    if isinstance(side, str):
        return side
    candidate = getattr(item, "candidate", None)
    if candidate is not None:
        cand_side = getattr(candidate, "side", None)
        if isinstance(cand_side, str):
            return cand_side
    raise TypeError(f"cannot resolve stopped side from {type(item)!r}")


def apply_side_stop_cooldowns(
    newly_stopped: Sequence[object],
    *,
    config: StrategyConfig,
    now: datetime,
    side_stop_cooldown_until: MutableMapping[str, datetime],
) -> None:
    """After stops fire, pause new entries on that side for ``same_side_stop_cooldown_minutes``."""
    minutes = config.same_side_stop_cooldown_minutes
    if minutes <= 0:
        return
    until = now + timedelta(minutes=minutes)
    for item in newly_stopped:
        side_stop_cooldown_until[_stopped_side(item)] = until


def side_stop_cooldown_block_reason(
    side: str,
    now: datetime,
    config: StrategyConfig,
    side_stop_cooldown_until: Mapping[str, datetime],
) -> str:
    if config.same_side_stop_cooldown_minutes <= 0:
        return ""
    until = side_stop_cooldown_until.get(side)
    if until is not None and now < until:
        return "side_stop_cooldown"
    return ""


def side_stop_cooldown_remaining_seconds(
    side: str,
    now: datetime,
    config: StrategyConfig,
    side_stop_cooldown_until: Mapping[str, datetime],
) -> int:
    if config.same_side_stop_cooldown_minutes <= 0:
        return 0
    until = side_stop_cooldown_until.get(side)
    if until is None or now >= until:
        return 0
    return max(0, int((until - now).total_seconds()))
