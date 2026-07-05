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
    parser.add_argument("--primary-run-id", default="", help="Default focus run (default: first --run).")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent / "data" / "dashboard_data.json"))
    args = parser.parse_args()

    default_runs = [
        "p3_trend1_skew075=data/dashboard_runs/p3_trend1_skew075:#1 Trend + Skew gates",
        "linear_decay_downsize=data/dashboard_runs/linear_decay_downsize:Baseline 3D + linear decay",
    ]
    specs = args.run or default_runs

    runs = []
    for spec in specs:
        id_part, rest = spec.split("=", 1)
        dir_part, _, label = rest.partition(":")
        results_dir = (ROOT / dir_part).resolve() if not Path(dir_part).is_absolute() else Path(dir_part)
        meta = {}
        if id_part == "linear_decay_downsize":
            meta = {
                "description": (
                    "3D_flatten_3.5 substrate (wide wings 200/75, 3x stop + 2-bar confirm, "
                    "halt -2.25%, flatten -3.5%) with time-of-day sizing: sell more early, less late. "
                    "Mon/Wed/Fri only before Apr 2022; all weekdays thereafter. Unconditional gates off."
                ),
                "sizing_schedule": (
                    "09:32-10:29 1.25x (39), 10:30-11:29 1.0x (31), 11:30-12:29 0.85x (26), "
                    "12:30-13:29 0.6x (19), 13:30-14:29 0.45x (14), 14:30-15:30 0.25x (8)"
                ),
                "credit_cap_pct": 50.0,
                "mbh_credit_target_pct": 1.5,
            }
        elif id_part == "p3_trend1_skew075":
            meta = {
                "description": (
                    "Improvement-plan #1: same 3D substrate + linear_decay_downsize sizing, but re-enables "
                    "entry gates disabled in the unconditional baseline — blocks bear_calls when trend_score > 1.0 "
                    "or skew_z > 0.75 (adverse uptrend / elevated call skew)."
                ),
                "gates": (
                    "candidate_max_adverse_trend=1.0 · candidate_max_adverse_skew=0.75"
                ),
                "sizing_schedule": (
                    "09:32-10:29 1.25x, 10:30-11:29 1.0x, 11:30-12:29 0.85x, "
                    "12:30-13:29 0.6x, 13:30-14:29 0.45x, 14:30-15:30 0.25x"
                ),
                "credit_cap_pct": 50.0,
            }
        run = build_run(id_part, results_dir, label or id_part, args.account_equity, meta)
        if run:
            daily = run.get("daily") or []
            if daily:
                run["meta"]["date_range"] = f"{daily[0]['date']} → {daily[-1]['date']}"
                run["meta"]["oos_days"] = len(daily)
            summary_path = results_dir / "summary.json"
            if summary_path.exists():
                try:
                    hist = json.loads(summary_path.read_text(encoding="utf-8"))
                    run["meta"]["note"] = (
                        f"Expiration-era calendar backtest · eligible metrics · "
                        f"{hist.get('first_oos_date', '')} OOS start · "
                        f"{hist.get('headline', {}).get('cagr_pct', '')}% CAGR (eligible path)"
                    )
                    for era in hist.get("era_summaries") or []:
                        if era.get("era"):
                            run["meta"].setdefault("era_summaries", []).append(era)
                except json.JSONDecodeError:
                    pass
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
        "live": build_live(Path(args.live_dir), args.account_equity),
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
