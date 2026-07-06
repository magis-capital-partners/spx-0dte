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
        equity += net
        peak = max(peak, equity)
        dd_pct = ((peak - equity) / peak * 100.0) if peak > 0 else 0.0
        ret_pct = net / account_equity * 100.0
        daily.append({
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
        })

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


def build_p3_strategy_guide(account_equity: float, hist: Optional[dict] = None) -> dict:
    """Plain-language strategy guide for the dashboard."""
    h = (hist or {}).get("headline") or {}
    ts = (hist or {}).get("trade_stats") or {}
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
        "results": [
            {"label": "Trading days (OOS)", "value": str(h.get("days", "805"))},
            {"label": "Date range", "value": f"{(hist or {}).get('first_oos_date', '2019-04-15')} → {(hist or {}).get('last_oos_date', '2026-07-02')}"},
            {"label": "Net P&L", "value": f"${h.get('net_pnl', 12420309.63):,.0f}"},
            {"label": "Ending equity", "value": f"${h.get('ending_equity', 25420309.63):,.0f}"},
            {"label": "CAGR (compounded)", "value": f"{h.get('cagr_pct', 23.36):.2f}%"},
            {"label": "Sharpe (daily)", "value": f"{h.get('sharpe', 1.67):.2f}"},
            {"label": "Sortino", "value": f"{h.get('sortino', 1.86):.2f}"},
            {"label": "Max drawdown", "value": f"{h.get('max_drawdown_pct', 11.17):.2f}%"},
            {"label": "Worst day", "value": f"{h.get('worst_day_pct', -4.94):.2f}% equity ({fmt_dollar(h.get('worst_day', -642633))})"},
            {"label": "Winning days", "value": f"{(h.get('day_win_rate', 0.6497) * 100):.1f}%"},
            {"label": "Total spread trades", "value": f"{h.get('trades', 13612):,}"},
            {"label": "Stop rate (trades)", "value": f"{(h.get('stop_rate', 0.2236) * 100):.1f}%"},
            {"label": "Spread win rate", "value": f"{(ts.get('win_rate', 0.7471) * 100):.1f}%"},
            {"label": "Avg P&L per trade", "value": f"${ts.get('expectancy_per_trade', 912.45):,.0f}"},
        ],
    }


def fmt_dollar(n: object) -> str:
    v = safe_float(n)
    return f"${v:,.0f}" if v >= 0 else f"-${abs(v):,.0f}"


def build_live(live_dir: Path, account_equity: float) -> dict:
    days: Dict[str, dict] = {}
    if not live_dir.exists():
        return {"days": {}}
    for day_path in sorted(live_dir.iterdir()):
        fills_file = day_path / "fills.jsonl"
        if not fills_file.exists():
            continue
        d = day_path.name
        entries = []
        for line in fills_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        trades = [e for e in entries if e.get("event") == "entry"]
        days[d] = {
            "date": d,
            "entries": trades,
            "flattened": any(e.get("event") == "daily_loss_flatten" for e in entries),
            "gross_credit_sold": round(sum(e.get("credit", 0) * e.get("contracts", 0) * 100 for e in trades), 2),
        }
    return {"days": days}


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
        "p3_trend1_skew075=data/dashboard_runs/p3_trend1_skew075:#1 Trend + Skew gates",
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
    blob = {
        "generated_at": datetime.now().isoformat(),
        "account_equity": args.account_equity,
        "primary_run_id": primary_id,
        "runs": runs,
        "mbh_benchmark": {"monthly": parse_mbh_benchmark(Path(args.mbh_returns))},
        "live": build_live(Path(args.live_dir), args.account_equity) if args.include_live else {"days": {}},
        "live_enabled": args.include_live,
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
    size_kb = out_path.stat().st_size / 1024
    print(f"wrote {out_path} ({size_kb:.0f} KB, {len(runs)} runs)")


if __name__ == "__main__":
    main()
