"""Portfolio statistics with optional traded-eligible-day metrics path."""
from __future__ import annotations

import math
from statistics import mean, pstdev
from typing import List, Literal, Sequence

TRADING_DAYS = 252
MetricsMode = Literal["all_rows", "eligible_only", "traded_only"]


def _safe_int(value: object, default: int = 0) -> int:
    try:
        if value in {"", None}:
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def filter_daily_rows(rows: Sequence[dict], mode: MetricsMode) -> List[dict]:
    if mode == "all_rows":
        return list(rows)
    if mode == "eligible_only":
        return [row for row in rows if str(row.get("eligible", "true")).lower() in {"true", "1"}]
    return [row for row in rows if _safe_int(row.get("trades")) > 0]


def portfolio_stats(
    daily_rows: Sequence[dict],
    account_equity: float,
    *,
    metrics_mode: MetricsMode = "all_rows",
) -> dict:
    """Compute compounded equity stats over a filtered daily path."""
    rows = filter_daily_rows(daily_rows, metrics_mode)
    days = len(rows)
    if days == 0:
        return {"days": 0, "metrics_mode": metrics_mode}

    equity = account_equity
    peak = account_equity
    max_dd = 0.0
    worst = 0.0
    rets: List[float] = []
    for row in rows:
        pnl = float(row["net_pnl"])
        worst = min(worst, pnl)
        rets.append(pnl / equity if equity else 0.0)
        equity += pnl
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)

    total_ret = equity / account_equity - 1.0
    years = days / TRADING_DAYS
    cagr = ((1 + total_ret) ** (1 / years) - 1.0) if years > 0 and total_ret > -1 else 0.0
    std = pstdev(rets) if len(rets) > 1 else 0.0
    sharpe = (mean(rets) / std) * math.sqrt(TRADING_DAYS) if std > 0 else 0.0
    downside = [r for r in rets if r < 0]
    downside_std = pstdev(downside) if len(downside) > 1 else 0.0
    sortino = (mean(rets) / downside_std) * math.sqrt(TRADING_DAYS) if downside_std > 0 else 0.0
    ann_vol = std * math.sqrt(TRADING_DAYS) if std > 0 else 0.0

    stops = sum(_safe_int(row.get("stopped_trades")) for row in rows)
    trades = sum(_safe_int(row.get("trades")) for row in rows)
    active_days = sum(1 for row in rows if _safe_int(row.get("trades")) > 0)

    return {
        "days": days,
        "active_days": active_days,
        "trades": trades,
        "stopped_trades": stops,
        "stop_rate": round(stops / trades, 4) if trades else 0.0,
        "net_pnl": round(equity - account_equity, 2),
        "ending_equity": round(equity, 2),
        "cagr_pct": round(cagr * 100, 2),
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "ann_vol_pct": round(ann_vol * 100, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "worst_day": round(worst, 2),
        "worst_day_pct": round(worst / account_equity * 100, 2) if account_equity else 0.0,
        "day_win_rate": round(sum(1 for r in rets if r > 0) / days, 4) if days else 0.0,
        "metrics_mode": metrics_mode,
    }
