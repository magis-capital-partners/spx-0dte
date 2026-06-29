"""Side-by-side comparison of MBH's actual daily return series vs. our reconstruction.

This is the calibration-target tool: instead of asking "does the clone beat a
holdout", it asks "does the clone reproduce MBH's *shape*" -- deployment cadence,
daily volatility, win rate, and the month-by-month return path.

Inputs
------
- MBH daily returns: the flagship fund sheets (data/mbh_returns/2024.csv, 2025.csv).
  Column layout (0-indexed): col0=date (M/D/YYYY), col1=AUM, col2=daily return %%.
  The daily return is gross (compounds to the gross monthly figure); our
  reconstruction's return_on_equity is likewise gross of fund-level fees, so the
  two are apples-to-apples for shape.
- Reconstruction daily file: daily_regime_validation.csv with `return_on_equity`,
  `trades`, `net_pnl`, `gross_credit_sold` columns.

Outputs
-------
- <out-prefix>_summary.csv  : metric | mbh | recon table
- <out-prefix>_monthly.csv  : month | mbh_gross | recon | gap
- <out-prefix>.md           : human-readable side-by-side writeup

Usage
-----
  python simulator/mbh_daily_comparison.py \
    --recon-daily simulator/_tmp_robustness_summary/daily_regime_validation.csv \
    --year 2025 \
    --out-prefix data/mbh_vs_recon_2025
"""
from __future__ import annotations

import argparse
import csv
import math
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from statistics import mean, pstdev
from typing import Dict, List, Optional, Tuple

TRADING_DAYS = 252
MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
MBH_DAILY_RETURN_COL = 2  # 0-indexed column in the fund sheets


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def parse_pct(value: str) -> Optional[float]:
    value = (value or "").strip().strip('"')
    if not value or value.upper() == "NA":
        return None
    try:
        return float(value.replace("%", "").replace(",", "")) / 100.0
    except ValueError:
        return None


def parse_sheet_date(value: str) -> Optional[date]:
    value = (value or "").strip().strip('"')
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def read_mbh_daily(path: Path, year: Optional[int] = None) -> Dict[date, float]:
    """Return {date: daily_return_fraction} from a flagship fund sheet."""
    out: Dict[date, float] = {}
    if not path.exists():
        return out
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.reader(handle):
            if len(row) <= MBH_DAILY_RETURN_COL:
                continue
            d = parse_sheet_date(row[0])
            if d is None:
                continue
            if year is not None and d.year != year:
                continue
            ret = parse_pct(row[MBH_DAILY_RETURN_COL])
            if ret is None:
                continue
            out[d] = ret
    return out


def read_recon_daily(path: Path) -> List[dict]:
    """Return per-day reconstruction rows with date/return/trades parsed."""
    rows: List[dict] = []
    if not path.exists():
        return rows
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for raw in csv.DictReader(handle):
            d = parse_sheet_date(raw.get("date", ""))
            if d is None:
                continue
            rows.append(
                {
                    "date": d,
                    "return": float(raw.get("return_on_equity") or 0.0),
                    "trades": int(float(raw.get("trades") or 0)),
                    "net_pnl": float(raw.get("net_pnl") or 0.0),
                    "credit": float(raw.get("gross_credit_sold") or 0.0),
                }
            )
    rows.sort(key=lambda r: r["date"])
    return rows


# --------------------------------------------------------------------------- #
# Stats
# --------------------------------------------------------------------------- #
def compound(values) -> float:
    total = 1.0
    for v in values:
        total *= 1.0 + v
    return total - 1.0


def series_stats(returns: List[float]) -> dict:
    """Cadence / vol / win-rate stats for a daily return series."""
    n = len(returns)
    active = [r for r in returns if abs(r) > 1e-12]
    pos = [r for r in returns if r > 1e-12]
    neg = [r for r in returns if r < -1e-12]
    std = pstdev(returns) if n > 1 else 0.0
    active_std = pstdev(active) if len(active) > 1 else 0.0
    avg = mean(returns) if returns else 0.0
    total = compound(returns)
    years = n / TRADING_DAYS if n else 0.0
    if years > 0 and total > -1.0:
        ann_return = (1.0 + total) ** (1.0 / years) - 1.0
    else:
        ann_return = 0.0
    ann_vol = std * math.sqrt(TRADING_DAYS)
    sharpe = (avg / std) * math.sqrt(TRADING_DAYS) if std > 0 else 0.0
    return {
        "trading_days": n,
        "active_days": len(active),
        "active_pct": len(active) / n if n else 0.0,
        "positive_days": len(pos),
        "negative_days": len(neg),
        "win_rate_active": len(pos) / len(active) if active else 0.0,
        "win_rate_all": len(pos) / n if n else 0.0,
        "mean_daily": avg,
        "daily_vol": std,
        "active_day_vol": active_std,
        "ann_vol": ann_vol,
        "best_day": max(returns) if returns else 0.0,
        "worst_day": min(returns) if returns else 0.0,
        "total_return": total,
        "ann_return": ann_return,
        "sharpe": sharpe,
    }


def monthly_returns(dated_returns: Dict[date, float]) -> Dict[str, float]:
    grouped: Dict[str, List[float]] = defaultdict(list)
    for d, r in dated_returns.items():
        grouped[f"{d.year:04d}-{d.month:02d}"].append(r)
    return {k: compound(v) for k, v in grouped.items()}


def worst_best_month(monthly: Dict[str, float]) -> Tuple[Tuple[str, float], Tuple[str, float]]:
    if not monthly:
        return ("", 0.0), ("", 0.0)
    worst = min(monthly.items(), key=lambda kv: kv[1])
    best = max(monthly.items(), key=lambda kv: kv[1])
    return worst, best


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def fmt_pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def write_summary_csv(path: Path, mbh: dict, recon: dict, recon_extra: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    metrics = [
        ("trading_days", "Trading days in window", "int"),
        ("active_days", "Days with non-zero P&L", "int"),
        ("active_pct", "Deployment frequency (% of days active)", "pct"),
        ("days_with_trades", "Days with >=1 new trade", "int_extra"),
        ("days_with_trades_pct", "% of days with a trade", "pct_extra"),
        ("avg_trades_per_active_day", "Avg trades per active day", "num_extra"),
        ("mean_daily", "Mean daily return", "pct"),
        ("daily_vol", "Daily volatility (all days)", "pct"),
        ("active_day_vol", "Daily volatility (active days only)", "pct"),
        ("ann_vol", "Annualized volatility", "pct"),
        ("win_rate_active", "Win rate (active days)", "pct"),
        ("win_rate_all", "Win rate (all days)", "pct"),
        ("best_day", "Best day", "pct"),
        ("worst_day", "Worst day", "pct"),
        ("total_return", "Total return over window", "pct"),
        ("ann_return", "Annualized return", "pct"),
        ("sharpe", "Sharpe (daily, ann.)", "num"),
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["metric", "mbh", "reconstruction"])
        for key, label, kind in metrics:
            if kind.endswith("extra"):
                mbh_val = ""
                recon_val = recon_extra.get(key, "")
            else:
                mbh_val = mbh.get(key, "")
                recon_val = recon.get(key, "")
            writer.writerow([label, mbh_val, recon_val])


def write_monthly_csv(path: Path, months: List[str], mbh_m: Dict[str, float], recon_m: Dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["month", "mbh_gross", "reconstruction", "gap_recon_minus_mbh"])
        for m in months:
            mv = mbh_m.get(m, 0.0)
            rv = recon_m.get(m, 0.0)
            writer.writerow([m, round(mv, 6), round(rv, 6), round(rv - mv, 6)])


def build_markdown(
    year: int,
    recon_path: Path,
    mbh_path: Path,
    window: Tuple[date, date],
    mbh: dict,
    recon: dict,
    recon_extra: dict,
    months: List[str],
    mbh_m: Dict[str, float],
    recon_m: Dict[str, float],
) -> str:
    mbh_worst, mbh_best = worst_best_month(mbh_m)
    recon_worst, recon_best = worst_best_month({m: recon_m.get(m, 0.0) for m in months})

    lines: List[str] = []
    lines.append(f"# MBH Daily Series vs. Reconstruction ({year})")
    lines.append("")
    lines.append(f"- MBH daily source: `{mbh_path}` (column {MBH_DAILY_RETURN_COL} = daily gross return)")
    lines.append(f"- Reconstruction daily source: `{recon_path}` (`return_on_equity`)")
    lines.append(f"- Aligned window: `{window[0]}` -> `{window[1]}`")
    lines.append("")
    lines.append("## 1. Cadence, volatility & win rate (aligned window)")
    lines.append("")
    lines.append("| Metric | MBH | Reconstruction |")
    lines.append("|---|---:|---:|")
    lines.append(f"| Trading days in window | {mbh['trading_days']} | {recon['trading_days']} |")
    lines.append(f"| Days with non-zero P&L | {mbh['active_days']} | {recon['active_days']} |")
    lines.append(f"| **Deployment frequency** (active %) | **{fmt_pct(mbh['active_pct'])}** | **{fmt_pct(recon['active_pct'])}** |")
    lines.append(f"| Days with >=1 new trade | n/a (marks daily) | {recon_extra['days_with_trades']} |")
    lines.append(f"| % of days with a trade | n/a | {fmt_pct(recon_extra['days_with_trades_pct'])} |")
    lines.append(f"| Avg trades per active day | n/a | {recon_extra['avg_trades_per_active_day']:.2f} |")
    lines.append(f"| Mean daily return | {fmt_pct(mbh['mean_daily'])} | {fmt_pct(recon['mean_daily'])} |")
    lines.append(f"| Daily volatility (all days) | {fmt_pct(mbh['daily_vol'])} | {fmt_pct(recon['daily_vol'])} |")
    lines.append(f"| **Daily vol (active days only)** | **{fmt_pct(mbh['active_day_vol'])}** | **{fmt_pct(recon['active_day_vol'])}** |")
    lines.append(f"| Annualized volatility | {fmt_pct(mbh['ann_vol'])} | {fmt_pct(recon['ann_vol'])} |")
    lines.append(f"| **Win rate (active days)** | **{fmt_pct(mbh['win_rate_active'])}** | **{fmt_pct(recon['win_rate_active'])}** |")
    lines.append(f"| Win rate (all days) | {fmt_pct(mbh['win_rate_all'])} | {fmt_pct(recon['win_rate_all'])} |")
    lines.append(f"| Best day | {fmt_pct(mbh['best_day'])} | {fmt_pct(recon['best_day'])} |")
    lines.append(f"| Worst day | {fmt_pct(mbh['worst_day'])} | {fmt_pct(recon['worst_day'])} |")
    lines.append(f"| Total return (window) | {fmt_pct(mbh['total_return'])} | {fmt_pct(recon['total_return'])} |")
    lines.append(f"| Annualized return | {fmt_pct(mbh['ann_return'])} | {fmt_pct(recon['ann_return'])} |")
    lines.append(f"| Sharpe (ann.) | {mbh['sharpe']:.2f} | {recon['sharpe']:.2f} |")
    lines.append("")
    lines.append("## 2. Monthly returns")
    lines.append("")
    lines.append("| Month | MBH gross | Reconstruction | Gap (recon - MBH) |")
    lines.append("|---|---:|---:|---:|")
    for m in months:
        mv = mbh_m.get(m, 0.0)
        rv = recon_m.get(m, 0.0)
        lines.append(f"| {m} | {fmt_pct(mv)} | {fmt_pct(rv)} | {fmt_pct(rv - mv)} |")
    lines.append(f"| **Compounded** | **{fmt_pct(compound(mbh_m.get(m, 0.0) for m in months))}** | **{fmt_pct(compound(recon_m.get(m, 0.0) for m in months))}** | |")
    lines.append("")
    lines.append(f"- MBH worst month: **{mbh_worst[0]} {fmt_pct(mbh_worst[1])}**, best: {mbh_best[0]} {fmt_pct(mbh_best[1])}")
    lines.append(f"- Recon worst month: **{recon_worst[0]} {fmt_pct(recon_worst[1])}**, best: {recon_best[0]} {fmt_pct(recon_best[1])}")
    lines.append("")
    lines.append("## 3. Read")
    lines.append("")
    cadence_ratio = (mbh["active_pct"] / recon_extra["days_with_trades_pct"]) if recon_extra["days_with_trades_pct"] else float("inf")
    lump_ratio = (recon["active_day_vol"] / mbh["active_day_vol"]) if mbh["active_day_vol"] else float("inf")
    lines.append(
        f"- **Cadence gap:** MBH carries risk ~every trading day ({fmt_pct(mbh['active_pct'])} active); "
        f"the clone opens a trade on only {fmt_pct(recon_extra['days_with_trades_pct'])} of days "
        f"(~{cadence_ratio:.0f}x less frequent)."
    )
    lines.append(
        f"- **Lumpiness gap:** on the days it *does* fire, the clone's per-day swing is {lump_ratio:.1f}x MBH's "
        f"({fmt_pct(recon['active_day_vol'])} vs {fmt_pct(mbh['active_day_vol'])} active-day vol) -- "
        f"a few large bets instead of many small premium clips. Its all-day vol still reads lower "
        f"({fmt_pct(recon['daily_vol'])} vs {fmt_pct(mbh['daily_vol'])}) only because ~89% of days sit flat in cash."
    )
    lines.append(
        f"- **Win-rate gap:** MBH {fmt_pct(mbh['win_rate_active'])} of active days green vs clone {fmt_pct(recon['win_rate_active'])}."
    )
    lines.append("")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(description="Compare MBH daily returns to the reconstruction.")
    parser.add_argument("--recon-daily", required=True, help="Path to daily_regime_validation.csv")
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument(
        "--mbh-sheet",
        default="",
        help="MBH fund sheet; defaults to data/mbh_returns/<year>.csv",
    )
    parser.add_argument("--out-prefix", required=True, help="Output path prefix (no extension)")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    mbh_path = Path(args.mbh_sheet) if args.mbh_sheet else root / "data" / "mbh_returns" / f"{args.year}.csv"
    recon_path = Path(args.recon_daily)

    mbh_daily = read_mbh_daily(mbh_path, year=args.year)
    recon_rows = read_recon_daily(recon_path)
    recon_daily = {r["date"]: r["return"] for r in recon_rows}

    if not mbh_daily:
        raise SystemExit(f"No MBH daily returns parsed from {mbh_path}")
    if not recon_rows:
        raise SystemExit(f"No reconstruction rows parsed from {recon_path}")

    # Aligned window = overlap of the two date ranges, restricted to dates MBH
    # actually traded (its calendar is the ground truth set of trading days).
    lo = max(min(mbh_daily), min(recon_daily))
    hi = min(max(mbh_daily), max(recon_daily))
    window_dates = sorted(d for d in mbh_daily if lo <= d <= hi)

    mbh_window = [mbh_daily[d] for d in window_dates]
    recon_window = [recon_daily.get(d, 0.0) for d in window_dates]

    mbh_stats = series_stats(mbh_window)
    recon_stats = series_stats(recon_window)

    # Reconstruction trade-cadence extras (only days that exist in recon file
    # and fall in the window count toward "days we could have traded").
    recon_in_window = [r for r in recon_rows if lo <= r["date"] <= hi]
    days_with_trades = sum(1 for r in recon_in_window if r["trades"] > 0)
    total_trades = sum(r["trades"] for r in recon_in_window)
    denom_days = len(window_dates)
    recon_extra = {
        "days_with_trades": days_with_trades,
        "days_with_trades_pct": days_with_trades / denom_days if denom_days else 0.0,
        "avg_trades_per_active_day": (total_trades / days_with_trades) if days_with_trades else 0.0,
    }

    mbh_monthly = monthly_returns({d: mbh_daily[d] for d in window_dates})
    recon_monthly = monthly_returns({d: recon_daily.get(d, 0.0) for d in window_dates})
    months = sorted(set(mbh_monthly) | set(recon_monthly))

    out_prefix = Path(args.out_prefix)
    summary_csv = out_prefix.with_name(out_prefix.name + "_summary.csv")
    monthly_csv = out_prefix.with_name(out_prefix.name + "_monthly.csv")
    md_path = out_prefix.with_suffix(".md")

    write_summary_csv(summary_csv, mbh_stats, recon_stats, recon_extra)
    write_monthly_csv(monthly_csv, months, mbh_monthly, recon_monthly)
    md = build_markdown(
        args.year, recon_path, mbh_path, (lo, hi),
        mbh_stats, recon_stats, recon_extra, months, mbh_monthly, recon_monthly,
    )
    md_path.write_text(md, encoding="utf-8")

    # Console summary
    print(f"Window {lo} -> {hi}  ({denom_days} MBH trading days)")
    print(f"  MBH    : active {mbh_stats['active_pct']:.0%}  vol {mbh_stats['daily_vol']*100:.2f}%/d  "
          f"win {mbh_stats['win_rate_active']:.0%}  ann {mbh_stats['ann_return']*100:.1f}%  Sharpe {mbh_stats['sharpe']:.2f}")
    print(f"  Recon  : trades on {recon_extra['days_with_trades_pct']:.0%} of days  vol {recon_stats['daily_vol']*100:.2f}%/d  "
          f"win {recon_stats['win_rate_active']:.0%}  ann {recon_stats['ann_return']*100:.1f}%  Sharpe {recon_stats['sharpe']:.2f}")
    print(f"wrote {summary_csv}")
    print(f"wrote {monthly_csv}")
    print(f"wrote {md_path}")


if __name__ == "__main__":
    main()
