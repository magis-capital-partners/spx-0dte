"""Build the SPX 0DTE dashboard data blob from backtest runs + MBH benchmark + live fills.

Outputs dashboard/data/dashboard_data.json, consumed by the static index.html
(React SPA, served via GitHub Pages).

Usage:
  python dashboard/build_dashboard_data.py \\
    --run linear_decay_downsize=data/dashboard_runs/linear_decay_downsize:"3D + linear decay downsize"
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from statistics import mean, pstdev
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))
sys.path.insert(0, str(ROOT / "live"))

from execution_type import execution_type, execution_type_label

MONTHS = ["January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]
TRADING_DAYS = 252


def safe_float(value: object, default: float = 0.0) -> float:
    try:
        if value in {"", None}:
            return default
        return float(str(value).replace("%", "").replace(",", ""))
    except (TypeError, ValueError):
        return default


def safe_int(value: object, default: int = 0) -> int:
    try:
        if value in {"", None}:
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def read_rows(path: Path) -> List[dict]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def find_daily_csv(results_dir: Path) -> Optional[Path]:
    for name in ("daily_regime_validation.csv", "daily_summary.csv"):
        path = results_dir / name
        if path.exists() and path.stat().st_size > 0:
            return path
    return None


def credit_and_margin_by_date(trades: List[dict]) -> Tuple[Dict[str, float], Dict[str, float]]:
    credit: Dict[str, float] = {}
    margin: Dict[str, float] = {}
    for t in trades:
        if t.get("model") == "net_long_overlay":
            continue
        d = t.get("date") or (t.get("entry_time") or "")[:10]
        if not d:
            continue
        c = safe_float(t.get("entry_credit")) * safe_int(t.get("contracts")) * 100
        credit[d] = credit.get(d, 0.0) + c
        width = safe_float(t.get("spread_width"))
        entry = safe_float(t.get("entry_credit"))
        m = max(width - entry, 0.0) * safe_int(t.get("contracts")) * 100
        margin[d] = margin.get(d, 0.0) + m
    return credit, margin


def summarize_daily(
    rows: List[dict],
    account_equity: float,
    credit_by_date: Dict[str, float],
    margin_by_date: Dict[str, float],
    compound: bool = True,
) -> dict:
    days = len(rows)
    if days == 0:
        return {"days": 0}

    trades = sum(safe_int(r.get("trades")) for r in rows)
    stops = sum(safe_int(r.get("stopped_trades")) for r in rows)
    pnl = sum(safe_float(r.get("net_pnl")) for r in rows)
    credit = sum(credit_by_date.get(r.get("date", ""), safe_float(r.get("gross_credit_sold"))) for r in rows)
    margins = [margin_by_date.get(r.get("date", ""), safe_float(r.get("approx_spread_margin"))) for r in rows]
    max_margin = max(margins, default=0.0)
    avg_margin = mean(margins) if margins else 0.0

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
    ann_vol = std_daily * math.sqrt(TRADING_DAYS) if std_daily > 0 else 0.0
    ann_downside_vol = downside_std * math.sqrt(TRADING_DAYS) if downside_std > 0 else 0.0
    calmar = (cagr / max_drawdown) if max_drawdown > 0 else 0.0

    wins = [r for r in daily_returns if r > 0]
    losses = [r for r in daily_returns if r < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else 0.0

    trade_pnls = []
    positive_days = sum(1 for r in daily_returns if r > 0)
    halted_days = sum(1 for row in rows if str(row.get("halted")).lower() in {"true", "1"})

    return {
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
        "calmar": round(calmar, 4),
        "ann_vol_pct": round(ann_vol * 100.0, 4),
        "ann_downside_vol_pct": round(ann_downside_vol * 100.0, 4),
        "daily_return_mean_pct": round(mean_daily * 100.0, 4),
        "daily_return_std_pct": round(std_daily * 100.0, 4),
        "profit_factor": round(profit_factor, 4),
        "max_drawdown_pct": round(max_drawdown * 100.0, 4),
        "worst_day": round(worst_day_pnl, 2),
        "worst_day_pct_equity": round(worst_day_pnl / account_equity * 100.0, 4) if account_equity else 0.0,
        "best_day_pct_equity": round(max(safe_float(r.get("net_pnl")) for r in rows) / account_equity * 100.0, 4) if account_equity else 0.0,
        "win_rate_days": round(positive_days / days, 4) if days else 0.0,
        "gross_credit_sold": round(credit, 2),
        "avg_daily_credit": round(credit / days, 2) if days else 0.0,
        "avg_daily_credit_pct_equity": round(credit / days / account_equity * 100.0, 4) if days and account_equity else 0.0,
        "max_margin": round(max_margin, 2),
        "max_margin_pct_equity": round(max_margin / account_equity * 100.0, 4) if account_equity else 0.0,
        "avg_margin": round(avg_margin, 2),
        "avg_margin_pct_equity": round(avg_margin / account_equity * 100.0, 4) if account_equity else 0.0,
        "halted_days": halted_days,
        "spread_win_rate": None,
        "spread_expectancy": None,
    }


def enrich_trade_stats(summary: dict, trades: List[dict]) -> None:
    spread = [t for t in trades if t.get("model") != "net_long_overlay"]
    if not spread:
        return
    wins = sum(1 for t in spread if safe_float(t.get("net_pnl")) > 0)
    summary["spread_win_rate"] = round(wins / len(spread), 4)
    summary["spread_expectancy"] = round(sum(safe_float(t.get("net_pnl")) for t in spread) / len(spread), 2)


def parse_mbh_benchmark(path: Path) -> Dict[str, float]:
    monthly: Dict[str, float] = {}
    if not path.exists():
        return monthly
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        for row in reader:
            if not row:
                continue
            year_str = row[0].strip().strip('"')
            if not year_str.isdigit():
                continue
            year = int(year_str)
            for i, month in enumerate(MONTHS, start=1):
                if i < len(row):
                    cell = row[i].strip().strip('"')
                    if cell and cell != "NA":
                        monthly[f"{year:04d}-{i:02d}"] = round(safe_float(cell) / 100.0, 6)
    return monthly


def build_run(run_id: str, results_dir: Path, label: str, account_equity: float, meta: Optional[dict] = None) -> Optional[dict]:
    daily_path = find_daily_csv(results_dir)
    if not daily_path:
        print(f"  skip {run_id}: no daily file in {results_dir}")
        return None

    rows = read_rows(daily_path)
    trade_rows = read_rows(results_dir / "trades.csv")
    credit_by_date, margin_by_date = credit_and_margin_by_date(trade_rows)
    summary = summarize_daily(rows, account_equity, credit_by_date, margin_by_date, compound=True)
    if summary.get("days", 0) == 0:
        print(f"  skip {run_id}: empty daily rows")
        return None
    enrich_trade_stats(summary, trade_rows)

    daily: List[dict] = []
    cum = 0.0
    equity = account_equity
    peak = account_equity
    for row in rows:
        d = row.get("date", "")
        net = safe_float(row.get("net_pnl"))
        cum += net
        equity_open = safe_float(row.get("equity_open"), default=0.0)
        base_for_ret = equity_open if equity_open > 0 else account_equity
        equity += net
        peak = max(peak, equity)
        dd_pct = ((peak - equity) / peak * 100.0) if peak > 0 else 0.0
        ret_pct = net / base_for_ret * 100.0 if base_for_ret else 0.0
        day_row = {
            "date": d,
            "net_pnl": round(net, 2),
            "cum_pnl": round(cum, 2),
            "equity": round(equity, 2),
            "return_pct": round(ret_pct, 4),
            "drawdown_pct": round(dd_pct, 4),
            "trades": safe_int(row.get("trades")),
            "stopped": safe_int(row.get("stopped_trades")),
            "halted": str(row.get("halted")).lower() in {"true", "1"},
            "regime": row.get("regime", ""),
            "event_bucket": row.get("event_bucket", ""),
            "gross_credit_sold": round(credit_by_date.get(d, safe_float(row.get("gross_credit_sold"))), 2),
            "approx_spread_margin": round(margin_by_date.get(d, safe_float(row.get("approx_spread_margin"))), 2),
        }
        if equity_open > 0:
            day_row["equity_open"] = round(equity_open, 2)
            day_row["k"] = round(safe_float(row.get("k"), default=1.0), 4)
        daily.append(day_row)

    trades_by_date: Dict[str, List[dict]] = {}
    for t in trade_rows:
        d = t.get("date") or (t.get("entry_time") or "")[:10]
        trades_by_date.setdefault(d, []).append({
            "entry_time": t.get("entry_time"),
            "side": t.get("side"),
            "model": t.get("model"),
            "contracts": safe_int(t.get("contracts")),
            "short": t.get("short"),
            "long": t.get("long"),
            "entry_credit": safe_float(t.get("entry_credit")),
            "score": safe_float(t.get("candidate_score")),
            "stopped": str(t.get("stopped")).lower() in {"true", "1"},
            "exit_reason": t.get("exit_reason"),
            "net_pnl": round(safe_float(t.get("net_pnl")), 2),
            "short_delta": safe_float(t.get("short_delta")),
        })

    return {
        "id": run_id,
        "label": label,
        "meta": meta or {},
        "summary": summary,
        "daily": daily,
        "trades_by_date": trades_by_date,
    }


def _strategy_guide_results(hist: Optional[dict]) -> list:
    h = (hist or {}).get("headline") or {}
    ts = (hist or {}).get("trade_stats") or {}
    return [
        {"label": "Trading days (OOS)", "value": str(h.get("days", "—"))},
        {
            "label": "Date range",
            "value": f"{(hist or {}).get('first_oos_date', '—')} → {(hist or {}).get('last_oos_date', '—')}",
        },
        {"label": "Net P&L", "value": f"${h.get('net_pnl', 0):,.0f}"},
        {"label": "Ending equity", "value": f"${h.get('ending_equity', 0):,.0f}"},
        {"label": "CAGR (compounded)", "value": f"{h.get('cagr_pct', 0):.2f}%"},
        {"label": "Sharpe (daily)", "value": f"{h.get('sharpe', 0):.2f}"},
        {"label": "Sortino", "value": f"{h.get('sortino', 0):.2f}"},
        {"label": "Max drawdown", "value": f"{h.get('max_drawdown_pct', 0):.2f}%"},
        {
            "label": "Worst day",
            "value": (
                f"{h.get('worst_day_pct', 0):.2f}% equity "
                f"({fmt_dollar(h.get('worst_day', 0))})"
            ),
        },
        {"label": "Winning days", "value": f"{(h.get('day_win_rate', 0) * 100):.1f}%"},
        {"label": "Total spread trades", "value": f"{h.get('trades', 0):,}"},
        {"label": "Stop rate (trades)", "value": f"{(h.get('stop_rate', 0) * 100):.1f}%"},
        {"label": "Spread win rate", "value": f"{(ts.get('win_rate', 0) * 100):.1f}%"},
        {"label": "Avg P&L per trade", "value": f"${ts.get('expectancy_per_trade', 0):,.0f}"},
    ]


def build_p3_strategy_guide(account_equity: float, hist: Optional[dict] = None) -> dict:
    """Plain-language strategy guide for the dashboard."""
    eq = f"${account_equity:,.0f}"
    return {
        "title": "Trend + Skew Gates on 3D Flatten (linear decay sizing)",
        "subtitle": (
            "SPXW same-day vertical credit spreads · 15-minute tranches · "
            "wide wings · short-leg stops · two risk governors · selective entry filters"
        ),
        "sections": [
            {
                "title": "What this strategy does",
                "paragraphs": [
                    (
                        "Every trading day, the simulator sells risk-defined SPXW 0DTE vertical credit spreads "
                        "in 15-minute tranches from 9:32 AM to 3:30 PM Eastern. Each tranche looks for a "
                        "short option near 15–25 delta (target 20 delta) and buys a farther-out long wing to cap loss. "
                        "If the short leg moves against us, we stop out that spread only and keep the long wing until "
                        "settlement — we do not stop the protective long."
                    ),
                    (
                        "This is the best-performing variant from the 2026 improvement-plan tests. It uses the same "
                        "validated “3D flatten” risk shell and linear-decay sizing as our prior baseline, but turns "
                        "two entry filters back on. Those filters skip new bear-call spreads (short calls / bearish "
                        "call-side structures) when the market is in a strong uptrend or when call skew is unusually "
                        "elevated — the conditions that caused most of the baseline’s weak periods."
                    ),
                ],
            },
            {
                "title": "Spread structure and stops",
                "bullets": [
                    "Instrument: SPXW options expiring the same session (0DTE).",
                    "Put spreads: 200-point wide wings (short strike + long strike 200 pts lower).",
                    "Call spreads: 75-point wide wings (short strike + long strike 75 pts higher).",
                    "Short-leg stop: exit when the short option reaches 3.0× the entry credit (e.g. sold for $1.00 → stop at $3.00).",
                    "Stop confirmation: price must breach the stop level on 2 consecutive 1-minute bars before firing (reduces whipsaw).",
                    "Fees: $0.79 per contract side, included in P&L.",
                ],
            },
            {
                "title": "Daily risk governors",
                "bullets": [
                    "Halt new entries when the day’s mark-to-market loss reaches −2.25% of account equity "
                    f"(≈ −${account_equity * 0.0225:,.0f} on {eq} starting equity).",
                    "Force-flatten all open positions when the day’s loss reaches −3.5% of equity "
                    f"(≈ −${account_equity * 0.035:,.0f}). This is deeper than the entry halt so normal "
                    "intraday volatility does not automatically flatten everything.",
                    "No cap on stops per side in this config (same-side stop cooldown disabled).",
                ],
            },
            {
                "title": "Entry gates (what changed vs the old baseline)",
                "bullets": [
                    "Bear-call filter — trend: skip the tranche if trend_score > 1.0 (market trending up strongly; "
                    "selling calls into a rally is penalized).",
                    "Bear-call filter — skew: skip if skew_z > 0.75 (call skew unusually rich vs recent history).",
                    "All other unconditional baseline gates remain off (no score threshold, no danger halts).",
                    "Bear puts are not filtered by these two gates — only the call / bear-call side is gated.",
                ],
            },
            {
                "title": "Position sizing (linear_decay_downsize)",
                "paragraphs": [
                    (
                        "Base size is 31 contracts per tranche at the 10:30–11:29 window. Earlier tranches sell "
                        "more premium; later tranches sell less as gamma and settlement risk rise."
                    ),
                ],
                "bullets": [
                    "9:32–10:29 → 1.25× base → 39 contracts",
                    "10:30–11:29 → 1.00× base → 31 contracts",
                    "11:30–12:29 → 0.85× base → 26 contracts",
                    "12:30–13:29 → 0.60× base → 19 contracts",
                    "13:30–14:29 → 0.45× base → 14 contracts",
                    "14:30–15:30 → 0.25× base → 8 contracts",
                ],
            },
            {
                "title": "Which days are traded",
                "bullets": [
                    "Before April 2022: Monday, Wednesday, Friday only (SPXW was not listed Tue/Thu).",
                    "April 2022 onward: all weekdays when SPXW trades.",
                    "Metrics (CAGR, Sharpe, drawdown) use only eligible trading days — skipped calendar days "
                    "are excluded from the equity path, not counted as zero-P&L days.",
                ],
            },
            {
                "title": "Backtest window and assumptions",
                "bullets": [
                    f"Starting equity: {eq} (compounded daily — profits roll into the next session).",
                    f"Out-of-sample start: {(hist or {}).get('first_oos_date', '2019-04-15')} "
                    f"(first ~60 sessions used for signal baseline warm-up).",
                    "Data: historical SPXW 1-minute quotes + reconstructed signal fields from ThetaData.",
                    "Simulated fills at mid / model prices — not a guarantee of live execution.",
                ],
            },
        ],
        "results": _strategy_guide_results(hist),
    }


def build_p3_poststop_strategy_guide(account_equity: float, hist: Optional[dict] = None) -> dict:
    """Plain-language strategy guide for the Wave 2 production optimal + IC overlay."""
    eq = f"${account_equity:,.0f}"
    return {
        "title": "Production — Put Wing 150 + FOMC Cutoff + Iron Condor",
        "subtitle": (
            "Vertical stack (put 150 / call 75, skew 0.65, flatten −3.25%, FOMC 13:30) "
            "plus short IC overlay: 10 contracts @ $13M baseline, ~0.16Δ, 50pt wings, VIX≥15, once/day"
        ),
        "sections": [
            {
                "title": "What this strategy does",
                "paragraphs": [
                    (
                        "Primary live/dashboard profile: SPXW 0DTE vertical credit spreads in 15-minute "
                        "tranches, asymmetric wings (put 150 / call 75), 3.0× short-leg stops with 2-bar "
                        "confirmation, daily loss halt at −2.25%, flatten at −3.25%, bear-call gates "
                        "(trend_score > 1.0 or skew_z > 0.65), linear_decay_downsize sizing, 120-minute "
                        "same-side post-stop cooldown, and FOMC no-new-entries after 13:30."
                    ),
                    (
                        "Iron condor overlay (July 2026 production A/B): once per day at/after 10:00, "
                        "sell a short ~0.16Δ put credit + call credit with 50-point wings when same-day VIX "
                        "open ≥ 15. Target size is 10 contracts at the $13M / 31-lot vertical baseline "
                        "(fraction 10/31; +25% vs prior 8-lot), so any global size multiplier on baseline "
                        "contracts scales the IC the same way as verticals. Losses are bounded by wing width − credit."
                    ),
                    (
                        "VIX regime controls for verticals: skip the entire session when same-day VIX open > 35; "
                        "on elevated days (VIX 25–35) multiply contract count by 1.25× on top of the "
                        "time-of-day schedule (capped at 48 contracts per tranche at the 31-contract baseline). "
                        "IC is additionally skipped when VIX open < 15 (fee drag in low vol). "
                        "VIX-conditioned put-wing widening is off (retired after it cut CAGR ~0.7pp)."
                    ),
                ],
            },
            {
                "title": "How the post-stop cooldown works (step by step)",
                "bullets": [
                    "A vertical stops when its short option trades at 3.0× entry credit for 2 consecutive 1-minute bars.",
                    "On stop, the simulator records the trade's side: bear_call or bull_put.",
                    "It sets side_stop_cooldown_until[side] = stop_timestamp + 120 minutes.",
                    "New entries on that side are blocked until the cooldown expires; the opposite side is unaffected.",
                    "Global stop cooldown remains 0 — we do not pause both sides.",
                ],
            },
            {
                "title": "Spread structure, gates, and sizing",
                "bullets": [
                    "Vertical puts: 150-point wings. Vertical calls: 75-point wings.",
                    "IC overlay: ~0.16Δ shorts, 50-point wings, 10 contracts @ flat 31-lot baseline (scales with size).",
                    "IC entry: first eligible tranche ≥ 10:00 when VIX open ≥ 15; max one IC structure per day.",
                    "FOMC: no new entries after 13:30 (open risk still managed).",
                    "Bear-call gate — trend: skip when trend_score > 1.0; skew: skip when skew_z > 0.65.",
                    "Halt new entries at −2.25% daily MTM; flatten all open at −3.25%.",
                    "Vertical sizing (linear_decay_downsize): 9:32–10:29 → 39 ctr (48 peak elevated), … 14:30–15:30 → 8 ctr.",
                ],
            },
            {
                "title": "Backtest window and assumptions",
                "bullets": [
                    f"Starting equity: {eq} (compounded daily).",
                    f"OOS start: {(hist or {}).get('first_oos_date', '2019-04-15')} after 40-session signal baseline warm-up.",
                    "Eligible Mon/Wed (pre-Apr 2022) then all weekdays; metrics on eligible days only.",
                    "Data: local SPXW 1-minute quotes + reconstructed signals (ThetaData history).",
                    "Compare to Trend BC 0.85 on the Overview cumulative P&L chart for a tighter risk-shape variant.",
                ],
            },
        ],
        "results": _strategy_guide_results(hist),
    }


def build_p3_compounding_strategy_guide(account_equity: float, hist: Optional[dict] = None) -> dict:
    """Strategy guide for equity-proportional compounding sizing."""
    eq = f"${account_equity:,.0f}"
    peak_k = ((hist or {}).get("compounding") or {}).get("peak_k")
    peak_txt = f"{peak_k:.2f}×" if peak_k else "path-dependent"
    return {
        "title": "Compounding f=1 — Size Tracks Equity",
        "subtitle": (
            "Same production stack as the primary run, but each day's baseline contracts "
            "and account equity scale with prior-day ending equity (k = E_t / E_0)"
        ),
        "sections": [
            {
                "title": "What differs from production",
                "paragraphs": [
                    (
                        "Production trades a fixed 31-lot baseline every day against a constant "
                        f"{eq} notional. This comparison run keeps every gate, wing, VIX policy, "
                        "IC overlay, and time-of-day schedule identical — but multiplies "
                        "baseline_contracts, account_equity, and the VIX tranche cap by "
                        "k = equity_open / E_0 at the start of each day."
                    ),
                    (
                        "Because every risk governor is expressed as a percent of account equity, "
                        "scaling contracts and equity together leaves the percentage risk profile "
                        "unchanged. The result is leverage-through-time: CAGR and max drawdown "
                        "rise together while Calmar stays roughly flat. Peak size on this path "
                        f"reached {peak_txt}."
                    ),
                ],
            },
            {
                "title": "How to read this vs production",
                "bullets": [
                    "Primary production line remains the live / fixed-lot deployment target.",
                    "Use this run on the Overview chart to see what reinvesting P&L into size would have done.",
                    "Max drawdown here is the stationary DD risk of the strategy; production's smaller DD is flattered by not resizing.",
                    "No market-impact model — large peak tranche sizes assume fills at the same mid as the 31-lot book.",
                ],
            },
            {
                "title": "Shared structure (same as production)",
                "bullets": [
                    "Put 150 / call 75 · skew 0.65 · flatten −3.25% · FOMC 13:30 · 120min cooldown.",
                    "IC overlay: ~0.16Δ, 50pt wings, size fraction 10/31 (scales with k).",
                    f"Starting equity: {eq} · same eligible-calendar OOS window as primary run.",
                ],
            },
        ],
        "results": _strategy_guide_results(hist),
    }


def build_p3_trend_bc_085_strategy_guide(account_equity: float, hist: Optional[dict] = None) -> dict:
    """Strategy guide for Wave 2 trend-gate comparison run."""
    eq = f"${account_equity:,.0f}"
    return {
        "title": "Trend BC 0.85 Gate (Wave 2 Risk-Shape Variant)",
        "subtitle": (
            "Same as production optimal (put wing 150) but skips bear calls when trend_score > 0.85 "
            "(production uses 1.0)"
        ),
        "sections": [
            {
                "title": "What differs from production optimal",
                "paragraphs": [
                    (
                        "This comparison run uses the full Wave 2 production stack — put wing 150, skew 0.65, "
                        "flatten −3.25%, 120min cooldown, VIX sizing — with one tighter entry filter: "
                        "candidate_max_adverse_trend = 0.85 instead of 1.0."
                    ),
                    (
                        "Bear calls in mildly positive trend buckets were a major source of weak days in "
                        "attribution analysis. Tightening the trend gate skips more call-side entries on "
                        "up-drift days. In Wave 2 backtests this improved max drawdown and worst day with "
                        "CAGR roughly flat vs the production optimal line."
                    ),
                ],
            },
            {
                "title": "When to compare this vs production optimal",
                "bullets": [
                    "Use production optimal (primary) for live deployment and headline CAGR/Calmar.",
                    "Use this variant on the Overview chart to see the risk/return trade-off of tighter trend filtering.",
                    "Not promoted to live — kept as a dashboard benchmark only unless a future combo test wins.",
                ],
            },
            {
                "title": "Shared structure (same as production optimal)",
                "bullets": [
                    "Put spreads: 150-point wings. Call spreads: 75-point wings.",
                    "Skew gate 0.65 · flatten −3.25% · halt −2.25% · 120min same-side cooldown.",
                    f"Starting equity: {eq} · same eligible-calendar OOS window as primary run.",
                ],
            },
        ],
        "results": _strategy_guide_results(hist),
    }


def fmt_dollar(n: object) -> str:
    v = safe_float(n)
    return f"${v:,.0f}" if v >= 0 else f"-${abs(v):,.0f}"


def build_live(live_dir: Path, account_equity: float) -> dict:
    """Embed paper/live session fills for Daily drill-down comparison."""
    del account_equity  # reserved for future live return_pct scaling
    days: Dict[str, dict] = {}
    history: List[dict] = []
    if not live_dir.exists():
        return {"days": {}}
    for day_path in sorted(live_dir.iterdir()):
        if not day_path.is_dir():
            continue
        fills_file = day_path / "fills.jsonl"
        if not fills_file.exists():
            continue
        d = day_path.name
        events = []
        for line in fills_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        trades = [e for e in events if e.get("event") == "entry"]
        stops = [e for e in events if e.get("event") == "stop"]
        rejected = [e for e in events if e.get("event") == "order_rejected"]
        session_ends = [e for e in events if e.get("event") == "session_end"]
        stopped_keys = {
            (s.get("side"), s.get("short_strike"), s.get("long_strike"))
            for s in stops
        }
        entry_rows = []
        for e in trades:
            key = (e.get("side"), e.get("short_strike"), e.get("long_strike"))
            entry_rows.append(
                {
                    "ts": e.get("ts"),
                    "side": e.get("side"),
                    "short_strike": e.get("short_strike"),
                    "long_strike": e.get("long_strike"),
                    "contracts": e.get("contracts"),
                    "credit": e.get("credit"),
                    "natural_credit": e.get("natural_credit"),
                    "fill_slippage": e.get("fill_slippage"),
                    "score": e.get("score"),
                    "stopped": key in stopped_keys,
                    "dry": bool(e.get("dry")),
                }
            )
        credit_from_entries = round(
            sum(float(e.get("credit") or 0) * float(e.get("contracts") or 0) * 100 for e in trades),
            2,
        )
        gross = credit_from_entries
        marked = None
        if session_ends:
            if session_ends[-1].get("gross_credit_sold") is not None:
                gross = float(session_ends[-1]["gross_credit_sold"])
            if session_ends[-1].get("marked_pnl") is not None:
                marked = float(session_ends[-1]["marked_pnl"])
        starts = [event for event in events if event.get("event") == "session_start"]
        latest_start = starts[-1] if starts else {}
        execution = execution_type(
            latest_start.get("mode"), latest_start.get("execution_type"),
        )
        execution_source = "recorded" if latest_start.get("execution_type") else "backfilled_from_mode"

        reconcile = None
        recon_path = day_path / "reconcile.json"
        if recon_path.exists():
            try:
                raw = json.loads(recon_path.read_text(encoding="utf-8"))
                reconcile = {
                    "diff_paper_scale": raw.get("diff_paper_scale"),
                    "diff_normalized_13m": raw.get("diff_normalized_13m"),
                    "backtest_paper_scale": {
                        k: raw.get("backtest_paper_scale", {}).get(k)
                        for k in ("available", "entries", "contracts", "stops", "net_pnl", "bear_call_pct")
                    },
                    "backtest_normalized_13m": {
                        k: raw.get("backtest_normalized_13m", {}).get(k)
                        for k in ("available", "entries", "contracts", "stops", "net_pnl", "bear_call_pct")
                    },
                }
            except json.JSONDecodeError:
                reconcile = None

        days[d] = {
            "date": d,
            "mode": latest_start.get("mode"),
            "execution_type": execution,
            "execution_label": execution_type_label(execution),
            "execution_type_source": execution_source,
            "entries": entry_rows,
            "stops": len(stops),
            "order_rejected": len(rejected),
            "flattened": any(e.get("event") == "flatten" for e in events),
            "halted": any(e.get("event") == "halt_entries" for e in events),
            "gross_credit_sold": gross,
            "marked_pnl": marked,
            "reconcile": reconcile,
        }
        if marked is not None:
            history.append({
                "date": d,
                "execution_type": execution,
                "execution_label": execution_type_label(execution),
                "marked_pnl": marked,
                "entries": len(entry_rows),
                "stops": len(stops),
            })
    totals: Dict[str, dict] = {}
    for row in history:
        bucket = totals.setdefault(row["execution_type"], {
            "execution_type": row["execution_type"],
            "execution_label": row["execution_label"], "sessions": 0,
            "marked_pnl": 0.0,
        })
        bucket["sessions"] += 1
        bucket["marked_pnl"] = round(bucket["marked_pnl"] + row["marked_pnl"], 2)
    return {"days": days, "history": history, "totals": totals}


def _compact_market_factors(report: dict, preset_id: str, equity: float) -> dict:
    """Dashboard-sized slice of run_full_analysis output."""
    rolling = report.get("rolling") or {}
    rolling_126 = (rolling.get("126") or {}) if isinstance(rolling, dict) else {}
    return {
        "preset": preset_id,
        "account_equity": equity,
        "n_days": report.get("n_days"),
        "date_start": report.get("date_start"),
        "date_end": report.get("date_end"),
        "labels": report.get("labels"),
        "headline": report.get("headline"),
        "correlation": report.get("correlation"),
        "single_factor": report.get("single_factor"),
        "multi_factor": report.get("multi_factor"),
        "partial_correlation": report.get("partial_correlation"),
        "rolling_126": rolling_126,
        "upside_downside": report.get("upside_downside"),
        "capture": report.get("capture"),
        "hedge_ratios": report.get("hedge_ratios"),
        "tracking_vs_spx": report.get("tracking_vs_spx"),
        "vix_regimes": report.get("vix_regimes"),
        "era_split": report.get("era_split"),
        "tail_comovement_spx": report.get("tail_comovement_spx"),
        "pca": report.get("pca"),
    }


def build_market_factors(preset_id: str, equity: float, results_dir: Path) -> Optional[dict]:
    """Strategy vs SPX/IXIC/RUT factor stats for the dashboard Market factors tab."""
    summary_path = results_dir / "daily_summary.csv"
    if not summary_path.exists():
        return None
    try:
        from index_daily import (  # noqa: WPS433
            CALENDAR_DIR,
            close_to_close_returns,
            csv_path_for_symbol,
            load_index_daily,
        )
        from market_factor_analysis import (  # noqa: WPS433
            build_return_panel,
            load_daily_summary_csv,
            run_full_analysis,
            strategy_returns_from_daily,
        )
        from vix_daily import DEFAULT_VIX_CSV, load_vix_daily  # noqa: WPS433
    except ImportError as exc:
        print(f"  market_factors skipped ({exc})")
        return None

    rows = load_daily_summary_csv(summary_path)
    strategy_rets = strategy_returns_from_daily(rows, equity)
    index_rets: dict = {}
    for symbol, key in (("^GSPC", "spx"), ("^IXIC", "ixic"), ("^RUT", "rut")):
        path = csv_path_for_symbol(symbol, CALENDAR_DIR)
        by_date = load_index_daily(path)
        if not by_date:
            print(f"  market_factors skipped (missing {path})")
            return None
        index_rets[key] = close_to_close_returns(by_date)

    vix_map = {d: row.open for d, row in load_vix_daily(DEFAULT_VIX_CSV).items()}
    panel = build_return_panel(strategy_rets, index_rets, vix_open_by_date=vix_map)
    if panel.n < 30:
        print(f"  market_factors skipped (only {panel.n} aligned days)")
        return None
    report = run_full_analysis(panel)
    compact = _compact_market_factors(report, preset_id, equity)
    h = compact.get("headline") or {}
    print(
        f"  market_factors {preset_id}: beta_spx={h.get('beta_spx')} "
        f"corr_spx={h.get('corr_spx')} n={compact.get('n_days')}"
    )
    return compact


def main() -> None:
    parser = argparse.ArgumentParser(description="Build SPX 0DTE dashboard data blob.")
    parser.add_argument("--run", action="append", default=[],
                        help="run spec id=results_dir:label (repeatable).")
    parser.add_argument("--account-equity", type=float, default=13_000_000)
    parser.add_argument("--mbh-returns", default=str(ROOT / "data" / "mbh_returns" / "All_Time_Net_Returns.csv"))
    parser.add_argument("--live-dir", default=str(ROOT / "data" / "live"))
    parser.add_argument(
        "--include-live",
        action="store_true",
        help="Embed live/paper fills from --live-dir (default: omit live data).",
    )
    parser.add_argument("--primary-run-id", default="", help="Default focus run (default: first --run).")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent / "data" / "dashboard_data.json"))
    args = parser.parse_args()

    default_runs = [
        "p3_poststop_cooldown_120=data/dashboard_runs/p3_poststop_cooldown_120:Production — OPEX 2× + month-end 0.5× + FOMC 13:30 + IC10 Δ0.16 (VIX≥15)",
        "p3_poststop_compounding_f1=data/dashboard_runs/p3_poststop_compounding_f1:Compounding f=1 — size tracks equity",
    ]
    specs = args.run or default_runs

    runs = []
    for spec in specs:
        id_part, rest = spec.split("=", 1)
        dir_part, _, label = rest.partition(":")
        results_dir = (ROOT / dir_part).resolve() if not Path(dir_part).is_absolute() else Path(dir_part)
        meta: dict = {}
        hist: Optional[dict] = None
        summary_path = results_dir / "summary.json"
        if summary_path.exists():
            try:
                hist = json.loads(summary_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                hist = None
        if id_part == "p3_trend1_skew075":
            meta = {
                "description": (
                    "Best backtest variant: 3D flatten substrate + linear decay sizing, with bear-call "
                    "entry gates re-enabled (skip when trend_score > 1.0 or skew_z > 0.75)."
                ),
                "gates": "candidate_max_adverse_trend=1.0 · candidate_max_adverse_skew=0.75",
                "sizing_schedule": (
                    "09:32-10:29 1.25x (39), 10:30-11:29 1.0x (31), 11:30-12:29 0.85x (26), "
                    "12:30-13:29 0.6x (19), 13:30-14:29 0.45x (14), 14:30-15:30 0.25x (8)"
                ),
                "credit_cap_pct": 50.0,
                "strategy_guide": build_p3_strategy_guide(args.account_equity, hist),
            }
        elif id_part == "p3_poststop_cooldown_120":
            meta = {
                "description": (
                    "Production stack: put wing 150 / call 75, skew 0.65, flatten −3.25%, FOMC 13:30 cutoff, "
                    "skip session VIX>35, 1.25× upscale VIX 25–35, plus short IC overlay "
                    "(10 contracts @ $13M/31-lot baseline, ~0.16Δ, 50pt wings, VIX≥15, once/day). "
                    "VIX put-wing widen disabled. Calendar sizing: 2x on monthly OPEX; 0.5x on the last observed month-end session."
                ),
                "gates": (
                    "trend 1.0 · skew 0.65 · put wing 150 · flatten −3.25% · FOMC 13:30 · "
                    "same_side_stop_cooldown_minutes=120 · skip session VIX>35 · "
                    "IC: VIX≥15 · 50pt wings · Δ0.16 · 10/31 size fraction · 1×/day · no VIX put-widen"
                ),
                "sizing_schedule": (
                    "linear_decay_downsize + VIX elevated 1.25× (25–35): "
                    "09:32-10:29 1.25x (39→48 peak elevated) … 14:30-15:30 0.25x (8); "
                    "IC size = round(vertical_base × 10/31)"
                ),
                "credit_cap_pct": 50.0,
                "strategy_guide": build_p3_poststop_strategy_guide(args.account_equity, hist),
            }
        elif id_part == "p3_poststop_compounding_f1":
            peak_k = ((hist or {}).get("compounding") or {}).get("peak_k")
            peak_txt = f"{peak_k:.2f}×" if peak_k else "path-dependent"
            meta = {
                "description": (
                    "Production stack with true equity compounding: each day scales "
                    "baseline_contracts, account_equity, and the VIX tranche cap by "
                    f"k = equity_open / $13M (peak k ≈ {peak_txt}). Same gates/IC/TOD as production. "
                    "Dashboard comparison — not the live fixed-lot deployment."
                ),
                "gates": (
                    "same as production · compounding k=E_t/E_0 on contracts + equity + tranche cap"
                ),
                "sizing_schedule": (
                    "linear_decay_downsize × k + VIX elevated 1.25× (25–35); "
                    "tranche cap = round(48 × k); IC = round(vertical_base × 10/31)"
                ),
                "credit_cap_pct": 50.0,
                "strategy_guide": build_p3_compounding_strategy_guide(args.account_equity, hist),
            }
        elif id_part == "p3_trend_bc_085":
            meta = {
                "description": (
                    "Wave 2 risk-shape variant: same as production optimal but candidate_max_adverse_trend=0.85 "
                    "(skip bear calls on milder uptrends). Dashboard comparison only — not live."
                ),
                "gates": (
                    "trend 0.85 · skew 0.65 · put wing 150 · flatten −3.25% · "
                    "same_side_stop_cooldown_minutes=120 · skip session VIX>35"
                ),
                "sizing_schedule": (
                    "linear_decay_downsize + VIX elevated 1.25× (25–35) — same as production optimal"
                ),
                "credit_cap_pct": 50.0,
                "strategy_guide": build_p3_trend_bc_085_strategy_guide(args.account_equity, hist),
            }
        run = build_run(id_part, results_dir, label or id_part, args.account_equity, meta)
        if run:
            daily = run.get("daily") or []
            if daily:
                run["meta"]["date_range"] = f"{daily[0]['date']} → {daily[-1]['date']}"
                run["meta"]["oos_days"] = len(daily)
            if hist:
                run["meta"]["note"] = (
                    f"Expiration-era calendar backtest · eligible metrics · "
                    f"{hist.get('first_oos_date', '')} OOS start · "
                    f"{hist.get('headline', {}).get('cagr_pct', '')}% CAGR (eligible path)"
                )
                for era in hist.get("era_summaries") or []:
                    if era.get("era"):
                        run["meta"].setdefault("era_summaries", []).append(era)
            runs.append(run)
            s = run["summary"]
            print(f"  added {id_part}: CAGR {s.get('cagr_pct')}% Sharpe {s.get('sharpe')} "
                  f"maxDD {s.get('max_drawdown_pct')}% ({len(run['daily'])} days)")

    primary_id = args.primary_run_id or (runs[0]["id"] if runs else None)
    primary_run = next((r for r in runs if r["id"] == primary_id), runs[0] if runs else None)
    market_factors = None
    if primary_run:
        primary_dir = (ROOT / "data" / "dashboard_runs" / primary_run["id"]).resolve()
        if not primary_dir.exists():
            for spec in specs:
                id_part, rest = spec.split("=", 1)
                if id_part == primary_run["id"]:
                    dir_part, _, _ = rest.partition(":")
                    primary_dir = (ROOT / dir_part).resolve() if not Path(dir_part).is_absolute() else Path(dir_part)
                    break
        market_factors = build_market_factors(primary_run["id"], args.account_equity, primary_dir)

    blob = {
        "generated_at": datetime.now().isoformat(),
        "account_equity": args.account_equity,
        "primary_run_id": primary_id,
        "runs": runs,
        "mbh_benchmark": {"monthly": parse_mbh_benchmark(Path(args.mbh_returns))},
        "live": build_live(Path(args.live_dir), args.account_equity) if args.include_live else {"days": {}},
        "live_enabled": args.include_live,
        "market_factors": market_factors,
        "mbh_targets": {
            "cagr_pct": [30, 40],
            "win_rate_pct": 65,
            "worst_day_pct": [-5, -4],
            "sharpe": 2.5,
            "daily_credit_pct": 1.5,
            "margin_pct": 40,
        },
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(blob, separators=(",", ":")), encoding="utf-8")
    stamp_path = out_path.parent / "build_stamp.txt"
    stamp_path.write_text(blob["generated_at"] + "\n", encoding="utf-8")
    size_kb = out_path.stat().st_size / 1024
    print(f"wrote {out_path} ({size_kb:.0f} KB, {len(runs)} runs)")


if __name__ == "__main__":
    main()
