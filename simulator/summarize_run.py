from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from statistics import mean, pstdev
from typing import List

TRADING_DAYS = 252


def read_rows(path: Path) -> List[dict]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def safe_float(value: object, default: float = 0.0) -> float:
    try:
        if value in {"", None}:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value: object, default: int = 0) -> int:
    try:
        if value in {"", None}:
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def summarize(results_dir: Path, account_equity: float, compound: bool = True) -> dict:
    """Summarize a regime_validation daily output.

    Reports both the legacy simple annualized return (sum of daily P&L over
    starting equity, scaled to 252 trading days) and a properly compounded CAGR
    that reinvests daily P&L. With abundant margin the strategy compounds, so
    CAGR is the more honest headline number.
    """
    rows = read_rows(results_dir / "daily_regime_validation.csv")
    days = len(rows)
    if days == 0:
        return {"results_dir": str(results_dir), "days": 0}

    trades = sum(safe_int(row.get("trades")) for row in rows)
    stops = sum(safe_int(row.get("stopped_trades")) for row in rows)
    pnl = sum(safe_float(row.get("net_pnl")) for row in rows)
    credit = sum(safe_float(row.get("gross_credit_sold")) for row in rows)
    margins = [safe_float(row.get("approx_spread_margin")) for row in rows]
    max_margin = max(margins, default=0.0)
    avg_margin = mean(margins) if margins else 0.0

    # Daily returns. Optionally compound onto a running equity balance.
    daily_returns: List[float] = []
    equity = account_equity
    peak = account_equity
    max_drawdown = 0.0
    worst_day_pnl = 0.0
    for row in rows:
        day_pnl = safe_float(row.get("net_pnl"))
        worst_day_pnl = min(worst_day_pnl, day_pnl)
        base = equity if compound else account_equity
        ret = day_pnl / base if base else 0.0
        daily_returns.append(ret)
        if compound:
            equity += day_pnl
        else:
            equity = account_equity + sum(safe_float(r.get("net_pnl")) for r in rows[: len(daily_returns)])
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)

    total_return = (equity / account_equity) - 1.0 if compound else (pnl / account_equity)
    years = days / TRADING_DAYS
    if compound and total_return > -1.0 and years > 0:
        cagr = (1.0 + total_return) ** (1.0 / years) - 1.0
    else:
        cagr = total_return / years if years else 0.0

    simple_annualized = (pnl / account_equity) * (TRADING_DAYS / days) if account_equity and days else 0.0

    mean_daily = mean(daily_returns) if daily_returns else 0.0
    std_daily = pstdev(daily_returns) if len(daily_returns) > 1 else 0.0
    sharpe = (mean_daily / std_daily) * math.sqrt(TRADING_DAYS) if std_daily > 0 else 0.0
    downside = [r for r in daily_returns if r < 0]
    downside_std = pstdev(downside) if len(downside) > 1 else 0.0
    sortino = (mean_daily / downside_std) * math.sqrt(TRADING_DAYS) if downside_std > 0 else 0.0

    positive_days = sum(1 for r in daily_returns if r > 0)
    halted_days = sum(1 for row in rows if str(row.get("halted")) == "True")

    return {
        "results_dir": str(results_dir),
        "account_equity": account_equity,
        "days": days,
        "trades": trades,
        "stopped_trades": stops,
        "stop_rate": round(stops / trades, 6) if trades else 0.0,
        "net_pnl": round(pnl, 2),
        "ending_equity": round(equity, 2),
        "total_return_pct": round(total_return * 100.0, 4),
        "cagr_pct": round(cagr * 100.0, 4),
        "simple_annualized_pct": round(simple_annualized * 100.0, 4),
        "sharpe": round(sharpe, 4),
        "sortino": round(sortino, 4),
        "max_drawdown_pct": round(max_drawdown * 100.0, 4),
        "worst_day": round(worst_day_pnl, 2),
        "worst_day_pct_equity": round(worst_day_pnl / account_equity * 100.0, 4) if account_equity else 0.0,
        "win_rate_days": round(positive_days / days, 4) if days else 0.0,
        "gross_credit_sold": round(credit, 2),
        "avg_daily_credit": round(credit / days, 2) if days else 0.0,
        "avg_daily_credit_pct_equity": round(credit / days / account_equity * 100.0, 4) if days and account_equity else 0.0,
        "max_margin": round(max_margin, 2),
        "max_margin_pct_equity": round(max_margin / account_equity * 100.0, 4) if account_equity else 0.0,
        "avg_margin": round(avg_margin, 2),
        "halted_days": halted_days,
    }


def print_summary(summary: dict) -> None:
    if summary.get("days", 0) == 0:
        print(f"{summary['results_dir']}: no daily rows")
        return
    print(f"Run: {summary['results_dir']}")
    print(f"  Days / Trades / Stops      : {summary['days']} / {summary['trades']} / {summary['stopped_trades']} (stop rate {summary['stop_rate']:.1%})")
    print(f"  Net P&L / Ending equity    : ${summary['net_pnl']:,.0f} / ${summary['ending_equity']:,.0f}")
    print(f"  CAGR (compounded)          : {summary['cagr_pct']:.2f}%")
    print(f"  Simple annualized          : {summary['simple_annualized_pct']:.2f}%")
    print(f"  Sharpe / Sortino           : {summary['sharpe']:.2f} / {summary['sortino']:.2f}")
    print(f"  Max drawdown               : {summary['max_drawdown_pct']:.2f}%")
    print(f"  Worst day                  : ${summary['worst_day']:,.0f} ({summary['worst_day_pct_equity']:.2f}%)")
    print(f"  Day win rate               : {summary['win_rate_days']:.1%}")
    print(f"  Avg daily credit           : ${summary['avg_daily_credit']:,.0f} ({summary['avg_daily_credit_pct_equity']:.2f}% of equity)")
    print(f"  Max margin / equity        : ${summary['max_margin']:,.0f} ({summary['max_margin_pct_equity']:.2f}%)")
    print(f"  Halted days                : {summary['halted_days']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize a regime_validation run with compounded CAGR and risk stats.")
    parser.add_argument("results_dir")
    parser.add_argument("--account-equity", type=float, default=13_000_000)
    parser.add_argument("--no-compound", action="store_true", help="Report simple (non-compounded) daily returns.")
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()

    summary = summarize(Path(args.results_dir), args.account_equity, compound=not args.no_compound)
    print_summary(summary)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"wrote {args.json_out}")


if __name__ == "__main__":
    main()
