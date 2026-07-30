"""Realized-vs-implied raw feature (pre z-score).

Definition (W0):
  - Build 1-minute log returns of spot from session open → now.
  - realized_daily ≈ σ_1m * sqrt(390)
  - implied_daily ≈ ATM IV / sqrt(252) when IV available; else straddle/spot
  - raw = realized_daily - implied_daily

Positive ⇒ realized running hot vs implied (worse for short vol).
Negative ⇒ implied rich vs realized so far (better for short vol).
"""
from __future__ import annotations

import math
from typing import Optional, Sequence


MIN_RETURNS = 5
SESSION_MINUTES = 390.0
TRADING_DAYS = 252.0


def realized_vs_implied_raw(
    spot_history: Sequence[float],
    *,
    spot: float,
    straddle: float,
    atm_iv: Optional[float] = None,
) -> float:
    if spot <= 0 or straddle <= 0 or len(spot_history) < MIN_RETURNS + 1:
        return 0.0

    rets = []
    prev = spot_history[0]
    for cur in spot_history[1:]:
        if prev > 0 and cur > 0:
            rets.append(math.log(cur / prev))
        prev = cur
    if len(rets) < MIN_RETURNS:
        return 0.0

    mean_r = sum(rets) / len(rets)
    var = sum((r - mean_r) ** 2 for r in rets) / max(len(rets) - 1, 1)
    sigma_1m = math.sqrt(max(var, 0.0))
    realized_daily = sigma_1m * math.sqrt(SESSION_MINUTES)

    if atm_iv is not None and math.isfinite(atm_iv) and atm_iv > 0.01:
        # Quotes store annualized IV as a decimal (e.g. 0.20).
        implied_daily = atm_iv / math.sqrt(TRADING_DAYS)
    else:
        # Fallback: ATM straddle / spot ≈ expected remaining absolute move.
        implied_daily = straddle / spot

    if not math.isfinite(realized_daily) or not math.isfinite(implied_daily):
        return 0.0
    return float(realized_daily - implied_daily)


def atm_iv_from_pair(call_iv: Optional[float], put_iv: Optional[float]) -> Optional[float]:
    vals = [v for v in (call_iv, put_iv) if v is not None and math.isfinite(v) and v > 0]
    if not vals:
        return None
    return sum(vals) / len(vals)
