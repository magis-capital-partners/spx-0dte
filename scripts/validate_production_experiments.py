"""Validate production experiments on the dashboard OOS path.

Runs three variants on the same eligible-calendar days, matching
``export_dashboard_run.py --preset p3_trend1_skew075``:

- current production (p3_trend1_skew075 + linear_decay_downsize)
- experiment 1: production + entry_start=10:00 (p3_combo delta)
- experiment 2: production + same_side_stop_cooldown_minutes=120

Outputs:
  data/dashboard_experiment_validation/summary.json
  data/dashboard_runs/<variant_id>/  (daily_summary, trades, summary.json)
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import datetime, time
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
SIMULATOR = ROOT / "simulator"
sys.path.insert(0, str(SIMULATOR))

from expiry_calendar import (  # noqa: E402
    DEFAULT_RULES,
    discover_eligible_dates,
    era_for_date,
    load_era_rules,
    parse_date,
    resolve_start_date,
)
from historical_baselines import write_csv  # noqa: E402
from mbh_simulator import (  # noqa: E402
    StrategyConfig,
    read_quotes_csv,
    read_signals_csv,
    simulate_day,
    stop_diagnostics_to_rows,
    trades_to_rows,
)
from portfolio_metrics import portfolio_stats  # noqa: E402
from profiles import (  # noqa: E402
    PRODUCTION_ACCOUNT_EQUITY,
    PRODUCTION_SIZING_SCHEME,
    PRODUCTION_TRAIN_COUNT,
    SCHEMES,
    build_p3_trend_skew_config,
)
from regime_validation import apply_rolling_baseline, discover_dates  # noqa: E402
from stop_calibration_runner import DEFAULT_PROCESSED  # noqa: E402
from time_of_day_sizing_runner import TimeOfDaySizePolicy  # noqa: E402
from unconditional_baseline import trade_stats  # noqa: E402

RESULTS_DIR = ROOT / "data" / "dashboard_experiment_validation"
DASHBOARD_RUNS = ROOT / "data" / "dashboard_runs"


def build_variants() -> Dict[str, Tuple[StrategyConfig, str]]:
    base = build_p3_trend_skew_config(account_equity=PRODUCTION_ACCOUNT_EQUITY)
    return {
        "p3_trend1_skew075": (
            base,
            "Current production (trend 1.0 + skew 0.75, entry from 9:32)",
        ),
        "p3_entry10_combo": (
            replace(base, entry_start=time(10, 0)),
            "Production + entry_start=10:00 (p3_combo delta)",
        ),
        "p3_poststop_cooldown_120": (
            replace(base, same_side_stop_cooldown_minutes=120),
            "Production + 120min same-side stop cooldown",
        ),
    }


def run_validation(
    *,
    processed_dir: Path,
    train_count: int,
    end_date: str,
    max_oos_days: int = 0,
) -> dict:
    floor, eras = load_era_rules(DEFAULT_RULES)
    processed_dates = discover_dates(processed_dir, "SPXW")
    if not processed_dates:
        raise SystemExit(f"No processed dates under {processed_dir}")

    resolved_end = end_date or processed_dates[-1]
    resolved_start = resolve_start_date(processed_dates, floor, require_mon_and_wed=True)
    eligible_dates = discover_eligible_dates(
        processed_dates,
        floor=resolved_start,
        end=resolved_end,
        eras=eras,
    )
    if len(eligible_dates) <= train_count:
        raise SystemExit(f"Need more than {train_count} eligible dates; have {len(eligible_dates)}")

    variants = build_variants()
    scheme = PRODUCTION_SIZING_SCHEME
    if scheme not in SCHEMES:
        raise SystemExit(f"Unknown sizing scheme {scheme!r}")

    daily_by: Dict[str, List[dict]] = {name: [] for name in variants}
    trades_by: Dict[str, List[dict]] = {name: [] for name in variants}
    stops_by: Dict[str, List[dict]] = {name: [] for name in variants}

    oos_total = len(eligible_dates) - train_count
    if max_oos_days > 0:
        eligible_dates = eligible_dates[: train_count + max_oos_days]
        oos_total = max_oos_days

    print(
        f"Dashboard-parity validation: {len(variants)} variants × {oos_total} OOS eligible days",
        flush=True,
    )
    print(f"  start={resolved_start} end={resolved_end} train={train_count} scheme={scheme}", flush=True)

    for index in range(train_count, len(eligible_dates)):
        test_date = eligible_dates[index]
        train_dates = eligible_dates[index - train_count : index]
        apply_rolling_baseline(processed_dir, "SPXW", train_dates, test_date, "signals_unconditional.csv")
        day_dir = processed_dir / "symbol=SPXW" / f"date={test_date}"
        quotes = read_quotes_csv(day_dir / "normalized_option_quotes.csv")
        signals = read_signals_csv(day_dir / "signals_unconditional.csv")
        day = parse_date(test_date)
        era = era_for_date(day, eras)

        for name, (config, _) in variants.items():
            policy = TimeOfDaySizePolicy(SCHEMES[scheme])
            result = simulate_day(quotes, signals, config=config, policy=policy)
            day_trades = trades_to_rows(result.trades)
            day_stops = stop_diagnostics_to_rows(result.trades)
            for row in day_trades:
                row["date"] = test_date
                trades_by[name].append(row)
            for row in day_stops:
                row["date"] = test_date
                stops_by[name].append(row)
            daily_by[name].append(
                {
                    "date": test_date,
                    "weekday": day.strftime("%a"),
                    "era": era,
                    "eligible": True,
                    "traded": len(result.trades) > 0,
                    "trades": len(result.trades),
                    "contracts_sold": sum(int(t.contracts) for t in result.trades),
                    "stopped_trades": sum(1 for t in result.trades if t.stopped),
                    "net_pnl": round(result.net_pnl, 2),
                    "halted": result.halted,
                }
            )

        done = index - train_count + 1
        if done % 50 == 0 or done == oos_total:
            print(f"  {done}/{oos_total} OOS days done ({test_date})", flush=True)

    baseline_id = "p3_trend1_skew075"
    baseline = portfolio_stats(daily_by[baseline_id], PRODUCTION_ACCOUNT_EQUITY, metrics_mode="eligible_only")
    rows: List[dict] = []

    for name, (_, label) in variants.items():
        daily = daily_by[name]
        spread_trades = [r for r in trades_by[name] if r.get("model") != "net_long_overlay"]
        ts = trade_stats(spread_trades)
        headline = portfolio_stats(daily, PRODUCTION_ACCOUNT_EQUITY, metrics_mode="eligible_only")

        out_dir = DASHBOARD_RUNS / name
        out_dir.mkdir(parents=True, exist_ok=True)
        write_csv(out_dir / "daily_summary.csv", daily)
        write_csv(out_dir / "trades.csv", trades_by[name])
        write_csv(out_dir / "stop_diagnostics.csv", stops_by[name])
        summary = {
            "generated_at": datetime.now().isoformat(),
            "preset": name,
            "label": label,
            "start_date": resolved_start,
            "end_date": resolved_end,
            "sizing_scheme": scheme,
            "eligible_dates": len(eligible_dates),
            "oos_eligible_days": len(daily),
            "first_oos_date": daily[0]["date"] if daily else "",
            "last_oos_date": daily[-1]["date"] if daily else "",
            "headline": headline,
            "trade_stats": ts,
        }
        (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

        rows.append(
            {
                "variant": name,
                "label": label,
                **headline,
                "spread_win_rate": ts["win_rate"],
                "spread_expectancy": ts["expectancy_per_trade"],
                "cagr_delta_vs_current": round(headline["cagr_pct"] - baseline["cagr_pct"], 2),
                "worst_day_delta_vs_current": round(headline["worst_day_pct"] - baseline["worst_day_pct"], 2),
                "max_dd_delta_vs_current": round(headline["max_drawdown_pct"] - baseline["max_drawdown_pct"], 2),
            }
        )

    payload = {
        "generated_at": datetime.now().isoformat(),
        "method": "export_dashboard_run historical_3d parity",
        "baseline_variant": baseline_id,
        "train_count": train_count,
        "sizing_scheme": scheme,
        "start_date": resolved_start,
        "end_date": resolved_end,
        "oos_eligible_days": oos_total,
        "current_production": next(r for r in rows if r["variant"] == baseline_id),
        "experiments": [r for r in rows if r["variant"] != baseline_id],
        "all_variants": rows,
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "summary.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def print_summary(payload: dict) -> None:
    base = payload["current_production"]
    print("\n=== vs current production ===", flush=True)
    print(
        f"{'Variant':28s} {'CAGR':>7s} {'dCAGR':>7s} {'Worst':>7s} {'dWorst':>7s} "
        f"{'MaxDD':>7s} {'dDD':>7s} {'Sharpe':>7s} {'Trades':>7s}",
        flush=True,
    )
    print("-" * 95, flush=True)
    for row in payload["all_variants"]:
        print(
            f"{row['variant']:28s} {row['cagr_pct']:6.1f}% {row['cagr_delta_vs_current']:+6.2f} "
            f"{row['worst_day_pct']:6.2f}% {row['worst_day_delta_vs_current']:+6.2f} "
            f"{row['max_drawdown_pct']:6.1f}% {row['max_dd_delta_vs_current']:+6.2f} "
            f"{row['sharpe']:7.2f} {row['trades']:7d}",
            flush=True,
        )
    print(f"\nBaseline net PnL: ${base['net_pnl']:,.0f}  ending equity ${base['ending_equity']:,.0f}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--train-count", type=int, default=PRODUCTION_TRAIN_COUNT)
    parser.add_argument("--end-date", default="", help="Cap eligible end date (default: latest processed).")
    parser.add_argument("--max-oos-days", type=int, default=0, help="Smoke test limit (0 = full OOS).")
    args = parser.parse_args()

    payload = run_validation(
        processed_dir=Path(DEFAULT_PROCESSED),
        train_count=args.train_count,
        end_date=args.end_date,
        max_oos_days=args.max_oos_days,
    )
    print_summary(payload)
    print(f"\nWrote {RESULTS_DIR / 'summary.json'}", flush=True)


if __name__ == "__main__":
    main()
