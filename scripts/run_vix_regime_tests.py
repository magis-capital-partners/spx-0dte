"""Walk-forward VIX sizing experiments on production dashboard path.

Variants (all use p3_poststop_cooldown_120 substrate + linear_decay_downsize):
- baseline_tod: current production sizing (no VIX)
- ddq_vix: DDQ VIX tiers on top of time-of-day schedule
- skip_lt12 / skip_lt15 / skip_gt35
- half_lt15 / half_lt17
- tc_friction_lt15: skip VIX < 15 (transaction-cost hypothesis)

Outputs: data/vix_regime_tests/summary.json + summary.csv
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
SIMULATOR = ROOT / "simulator"
sys.path.insert(0, str(SIMULATOR))

from expiry_calendar import (  # noqa: E402
    DEFAULT_RULES,
    discover_eligible_dates,
    load_era_rules,
    resolve_start_date,
)
from historical_baselines import write_csv  # noqa: E402
from mbh_simulator import (  # noqa: E402
    StrategyConfig,
    read_quotes_csv,
    read_signals_csv,
    simulate_day,
    trades_to_rows,
)
from portfolio_metrics import portfolio_stats  # noqa: E402
from profiles import (  # noqa: E402
    PRODUCTION_ACCOUNT_EQUITY,
    PRODUCTION_SIZING_SCHEME,
    PRODUCTION_TRAIN_COUNT,
    SCHEMES,
    build_p3_poststop_cooldown_config,
)
from regime_validation import apply_rolling_baseline, discover_dates  # noqa: E402
from stop_calibration_runner import DEFAULT_PROCESSED  # noqa: E402
from time_of_day_sizing_runner import TimeOfDaySizePolicy  # noqa: E402
from unconditional_baseline import trade_stats  # noqa: E402
from vix_daily import DEFAULT_VIX_CSV, load_vix_daily, regime_bucket  # noqa: E402
from vix_sizing_policies import VixTimeOfDayPolicy  # noqa: E402

RESULTS = ROOT / "data" / "vix_regime_tests"


def build_variants() -> Dict[str, Tuple[StrategyConfig, Callable]]:
    base = build_p3_poststop_cooldown_config(account_equity=PRODUCTION_ACCOUNT_EQUITY)
    schedule = SCHEMES[PRODUCTION_SIZING_SCHEME]
    variants: Dict[str, Tuple[StrategyConfig, Callable]] = {
        "baseline_tod": (base, lambda: TimeOfDaySizePolicy(schedule)),
        "ddq_vix": (base, lambda: VixTimeOfDayPolicy(schedule, vix_mode="ddq")),
        "skip_lt12": (base, lambda: VixTimeOfDayPolicy(schedule, vix_mode="skip_lt12")),
        "skip_lt15": (base, lambda: VixTimeOfDayPolicy(schedule, vix_mode="skip_lt15")),
        "skip_gt35": (base, lambda: VixTimeOfDayPolicy(schedule, vix_mode="skip_gt35")),
        "half_lt15": (base, lambda: VixTimeOfDayPolicy(schedule, vix_mode="half_lt15")),
        "half_lt17": (base, lambda: VixTimeOfDayPolicy(schedule, vix_mode="half_lt17")),
        "tc_friction_lt15": (base, lambda: VixTimeOfDayPolicy(schedule, vix_mode="tc_friction_lt15")),
    }
    return variants


def run_matrix(
    *,
    processed_dir: Path,
    train_count: int,
    max_oos_days: int = 0,
    signals_filename: str = "signals_unconditional.csv",
    results_dir: Optional[Path] = None,
) -> Tuple[List[dict], Dict[str, List[dict]]]:
    floor, eras = load_era_rules(DEFAULT_RULES)
    processed_dates = discover_dates(processed_dir, "SPXW")
    resolved_start = resolve_start_date(processed_dates, floor, require_mon_and_wed=True)
    end_date = processed_dates[-1]
    eligible = discover_eligible_dates(processed_dates, floor=resolved_start, end=end_date, eras=eras)
    if len(eligible) <= train_count:
        raise SystemExit(f"Need more than {train_count} eligible dates; have {len(eligible)}")

    if max_oos_days > 0:
        eligible = eligible[: train_count + max_oos_days]

    variants = build_variants()
    daily_by: Dict[str, List[dict]] = {name: [] for name in variants}
    trades_by: Dict[str, List[dict]] = {name: [] for name in variants}
    oos = len(eligible) - train_count
    print(f"VIX regime tests: {len(variants)} variants × {oos} OOS eligible days", flush=True)

    for index in range(train_count, len(eligible)):
        test_date = eligible[index]
        train_dates = eligible[index - train_count : index]
        apply_rolling_baseline(processed_dir, "SPXW", train_dates, test_date, signals_filename)
        day_dir = processed_dir / f"symbol=SPXW" / f"date={test_date}"
        quotes = read_quotes_csv(day_dir / "normalized_option_quotes.csv")
        signals = read_signals_csv(day_dir / signals_filename)

        for name, (cfg, policy_factory) in variants.items():
            policy = policy_factory()
            result = simulate_day(quotes, signals, config=cfg, policy=policy)
            day_trades = trades_to_rows(result.trades)
            for row in day_trades:
                row["date"] = test_date
                trades_by[name].append(row)
            daily_by[name].append(
                {
                    "date": test_date,
                    "trades": len(result.trades),
                    "stopped_trades": sum(1 for t in result.trades if t.stopped),
                    "net_pnl": round(result.net_pnl, 2),
                    "halted": result.halted,
                }
            )
        done = index - train_count + 1
        if done % 100 == 0:
            print(f"  {done}/{oos} OOS days ({test_date})", flush=True)

    summary_rows: List[dict] = []
    for name, cfg in ((n, v[0]) for n, v in variants.items()):
        daily_rows = daily_by[name]
        spread_trades = [r for r in trades_by[name] if r.get("model") != "net_long_overlay"]
        ts = trade_stats(spread_trades)
        port = portfolio_stats(daily_rows, cfg.account_equity)
        summary_rows.append(
            {
                "variant": name,
                **port,
                "spread_win_rate": ts["win_rate"],
                "spread_expectancy": ts["expectancy_per_trade"],
                "total_trades": len(spread_trades),
            }
        )
        if results_dir is not None:
            variant_dir = results_dir / name
            variant_dir.mkdir(parents=True, exist_ok=True)
            write_csv(variant_dir / "daily_summary.csv", daily_rows)
            write_csv(variant_dir / "trades.csv", spread_trades)
    return summary_rows, daily_by


def regime_breakdown(daily_rows: List[dict], vix_by_date: dict) -> List[dict]:
    buckets: Dict[str, dict] = {}
    for row in daily_rows:
        trade_date = row["date"]
        vix_day = vix_by_date.get(trade_date)
        bucket = regime_bucket(vix_day.open) if vix_day else "missing"
        agg = buckets.setdefault(
            bucket,
            {"vix_bucket": bucket, "days": 0, "net_pnl": 0.0, "wins": 0, "trades": 0, "stopped_trades": 0},
        )
        agg["days"] += 1
        agg["net_pnl"] += float(row["net_pnl"])
        agg["wins"] += 1 if float(row["net_pnl"]) > 0 else 0
        agg["trades"] += int(row.get("trades", 0))
        agg["stopped_trades"] += int(row.get("stopped_trades", 0))
    out = []
    for bucket, agg in sorted(buckets.items()):
        days = agg["days"]
        out.append(
            {
                "vix_bucket": bucket,
                "days": days,
                "net_pnl": round(agg["net_pnl"], 0),
                "mean_day": round(agg["net_pnl"] / days, 0) if days else 0,
                "win_rate": round(agg["wins"] / days, 3) if days else 0,
                "stop_rate": round(agg["stopped_trades"] / max(agg["trades"], 1), 3),
            }
        )
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Run VIX sizing variant matrix on production path.")
    parser.add_argument("--processed-dir", type=Path, default=DEFAULT_PROCESSED)
    parser.add_argument("--train-count", type=int, default=PRODUCTION_TRAIN_COUNT)
    parser.add_argument("--max-oos-days", type=int, default=0, help="0 = full OOS eligible calendar")
    parser.add_argument(
        "--signals-filename",
        default="signals_unconditional.csv",
        help="Rolling-baseline signals (vix copied from enriched signals.csv).",
    )
    parser.add_argument("--results-dir", type=Path, default=RESULTS)
    args = parser.parse_args()

    args.results_dir.mkdir(parents=True, exist_ok=True)
    summary_rows, daily_by = run_matrix(
        processed_dir=args.processed_dir,
        train_count=args.train_count,
        max_oos_days=args.max_oos_days,
        signals_filename=args.signals_filename,
        results_dir=args.results_dir,
    )
    write_csv(args.results_dir / "summary.csv", summary_rows)
    (args.results_dir / "summary.json").write_text(json.dumps(summary_rows, indent=2), encoding="utf-8")

    vix_by_date = load_vix_daily(DEFAULT_VIX_CSV)
    baseline_daily = daily_by.get("baseline_tod", [])
    breakdown = regime_breakdown(baseline_daily, vix_by_date)
    (args.results_dir / "baseline_regime_breakdown.json").write_text(
        json.dumps(breakdown, indent=2),
        encoding="utf-8",
    )

    print("\n=== VIX regime sizing tests ===")
    for row in sorted(summary_rows, key=lambda r: r.get("cagr_pct", 0), reverse=True):
        print(
            f"  {row['variant']:<20} CAGR={row['cagr_pct']:>6.1f}%  "
            f"worst={row['worst_day_pct']:>6.2f}%  maxDD={row['max_drawdown_pct']:>6.2f}%  "
            f"sharpe={row['sharpe']:>5.2f}"
        )
    print(f"\nWrote {args.results_dir / 'summary.json'}")
    print(f"Wrote {args.results_dir / 'baseline_regime_breakdown.json'}")


if __name__ == "__main__":
    main()
