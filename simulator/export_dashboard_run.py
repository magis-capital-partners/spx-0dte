"""Export a stop-calibration or time-of-day sizing variant to data/dashboard_runs/."""
from __future__ import annotations

import argparse
import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from expiry_calendar import (
    DEFAULT_RULES,
    discover_eligible_dates,
    era_for_date,
    load_era_rules,
    parse_date,
    resolve_start_date,
)
from historical_baselines import write_csv
from mbh_simulator import read_quotes_csv, read_signals_csv, simulate_day, stop_diagnostics_to_rows, trades_to_rows
from portfolio_metrics import portfolio_stats
from regime_validation import apply_rolling_baseline, discover_dates
from stop_calibration_runner import (
    DEFAULT_PROCESSED,
    base_config,
    portfolio_stats as legacy_portfolio_stats,
    run_config,
)
from time_of_day_sizing_runner import SCHEMES, TimeOfDaySizePolicy, WINNERS as TOD_WINNERS
from unconditional_baseline import trade_stats

# Canonical frozen winners — single source of truth (see simulator/profiles.py).
from profiles import WINNERS

PRESETS = {
    "3d_flatten_3_5": {
        "kind": "stop_calibration",
        "variant_id": "3D_flatten_3.5",
        "phase": "3D",
        "kwargs": WINNERS,
        "label": "3D flatten @ -3.5% (wide wings + 3x stop)",
    },
    "linear_decay_downsize": {
        "kind": "time_of_day",
        "scheme": "linear_decay_downsize",
        "label": "3D + linear decay downsize (sell early, less late)",
    },
    "p3_trend1_skew075": {
        "kind": "historical_3d",
        "scheme": "linear_decay_downsize",
        "config_overrides": {
            "candidate_max_adverse_trend": 1.0,
            "candidate_max_adverse_skew": 0.75,
        },
        "label": "#1 Trend + Skew gates (improvement plan winner)",
    },
}


def export_time_of_day_scheme(
    scheme: str,
    out_dir: Path,
    processed_dir: Path,
    symbol: str,
    signals_filename: str,
    train_count: int,
) -> dict:
    if scheme not in SCHEMES:
        raise SystemExit(f"Unknown scheme {scheme!r}; choose from {sorted(SCHEMES)}")

    dates = discover_dates(processed_dir, symbol)
    if len(dates) <= train_count:
        raise SystemExit(f"Need more than {train_count} dates; have {len(dates)}")

    config = base_config(**TOD_WINNERS)
    policy = TimeOfDaySizePolicy(SCHEMES[scheme])
    daily_rows = []
    all_trades = []
    stop_rows = []

    oos_days = len(dates) - train_count
    print(f"Running {scheme} -> {out_dir} ({oos_days} OOS days)...")
    for index in range(train_count, len(dates)):
        test_date = dates[index]
        train_dates = dates[index - train_count : index]
        apply_rolling_baseline(processed_dir, symbol, train_dates, test_date, signals_filename)
        day_dir = processed_dir / f"symbol={symbol}" / f"date={test_date}"
        result = simulate_day(
            read_quotes_csv(day_dir / "normalized_option_quotes.csv"),
            read_signals_csv(day_dir / signals_filename),
            config=config,
            policy=policy,
        )
        day_trades = trades_to_rows(result.trades)
        day_stops = stop_diagnostics_to_rows(result.trades)
        for row in day_trades:
            row["date"] = test_date
            all_trades.append(row)
        for row in day_stops:
            row["date"] = test_date
            stop_rows.append(row)
        daily_rows.append(
            {
                "date": test_date,
                "trades": len(result.trades),
                "contracts_sold": sum(int(t.contracts) for t in result.trades),
                "stopped_trades": sum(1 for t in result.trades if t.stopped),
                "net_pnl": round(result.net_pnl, 2),
                "halted": result.halted,
            }
        )
        done = index - train_count + 1
        if done % 50 == 0:
            print(f"  {done}/{oos_days} OOS days done ({test_date})")

    spread_trades = [r for r in all_trades if r.get("model") != "net_long_overlay"]
    ts = trade_stats(spread_trades)
    port = legacy_portfolio_stats(daily_rows, config.account_equity)

    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(out_dir / "daily_summary.csv", daily_rows)
    write_csv(out_dir / "trades.csv", all_trades)
    write_csv(out_dir / "stop_diagnostics.csv", stop_rows)

    return {
        "scheme": scheme,
        **port,
        "spread_win_rate": ts["win_rate"],
        "spread_expectancy": ts["expectancy_per_trade"],
    }


def export_historical_3d_variant(
    preset_id: str,
    spec: dict,
    out_dir: Path,
    processed_dir: Path,
    symbol: str,
    signals_filename: str,
    train_count: int,
    *,
    end_date: str = "",
    rules_file: Path = DEFAULT_RULES,
) -> dict:
    """Run eligible-calendar 3D backtest with optional config overrides."""
    scheme = spec["scheme"]
    if scheme not in SCHEMES:
        raise SystemExit(f"Unknown scheme {scheme!r}")

    floor, eras = load_era_rules(rules_file)
    processed_dates = discover_dates(processed_dir, symbol)
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

    overrides = spec.get("config_overrides") or {}
    config = replace(base_config(**TOD_WINNERS), **overrides)
    policy = TimeOfDaySizePolicy(SCHEMES[scheme])

    daily_rows: list[dict] = []
    all_trades: list[dict] = []
    stop_rows: list[dict] = []

    oos_days = len(eligible_dates) - train_count
    print(f"Running {preset_id} -> {out_dir} ({oos_days} OOS eligible days)...")
    for index in range(train_count, len(eligible_dates)):
        test_date = eligible_dates[index]
        train_dates = eligible_dates[index - train_count : index]
        apply_rolling_baseline(processed_dir, symbol, train_dates, test_date, signals_filename)
        day_dir = processed_dir / f"symbol={symbol}" / f"date={test_date}"
        result = simulate_day(
            read_quotes_csv(day_dir / "normalized_option_quotes.csv"),
            read_signals_csv(day_dir / signals_filename),
            config=config,
            policy=policy,
        )
        day = parse_date(test_date)
        era = era_for_date(day, eras)
        day_trades = trades_to_rows(result.trades)
        day_stops = stop_diagnostics_to_rows(result.trades)
        for row in day_trades:
            row["date"] = test_date
            all_trades.append(row)
        for row in day_stops:
            row["date"] = test_date
            stop_rows.append(row)
        daily_rows.append(
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
        if done % 50 == 0 or done == oos_days:
            print(f"  {done}/{oos_days} OOS days done ({test_date})")

    spread_trades = [r for r in all_trades if r.get("model") != "net_long_overlay"]
    ts = trade_stats(spread_trades)
    headline = portfolio_stats(daily_rows, config.account_equity, metrics_mode="eligible_only")

    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(out_dir / "daily_summary.csv", daily_rows)
    write_csv(out_dir / "trades.csv", all_trades)
    write_csv(out_dir / "stop_diagnostics.csv", stop_rows)
    summary = {
        "generated_at": datetime.now().isoformat(),
        "preset": preset_id,
        "start_date": resolved_start,
        "end_date": resolved_end,
        "config_overrides": overrides,
        "sizing_scheme": scheme,
        "eligible_dates": len(eligible_dates),
        "oos_eligible_days": len(daily_rows),
        "first_oos_date": daily_rows[0]["date"] if daily_rows else "",
        "last_oos_date": daily_rows[-1]["date"] if daily_rows else "",
        "headline": headline,
        "trade_stats": ts,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    return {
        **headline,
        "spread_win_rate": ts["win_rate"],
        "spread_expectancy": ts["expectancy_per_trade"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preset", default="linear_decay_downsize")
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--train-count", type=int, default=40)
    parser.add_argument("--end-date", default="", help="Cap eligible end date (default: latest processed).")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    preset = args.preset
    if preset not in PRESETS:
        raise SystemExit(f"Unknown preset {preset!r}; choose from {sorted(PRESETS)}")

    out = Path(args.out_dir) if args.out_dir else root / "data" / "dashboard_runs" / preset
    processed = Path(DEFAULT_PROCESSED)
    spec = PRESETS[preset]

    if spec["kind"] == "time_of_day":
        row = export_time_of_day_scheme(
            spec["scheme"],
            out,
            processed,
            "SPXW",
            "signals_unconditional.csv",
            args.train_count,
        )
    elif spec["kind"] == "historical_3d":
        row = export_historical_3d_variant(
            preset,
            spec,
            out,
            processed,
            "SPXW",
            "signals_unconditional.csv",
            args.train_count,
            end_date=args.end_date,
        )
    else:
        vid = spec["variant_id"]
        kwargs = spec["kwargs"]
        dates = discover_dates(processed, "SPXW")
        cfg = base_config(**kwargs)
        print(f"Running {vid} -> {out} ({len(dates) - args.train_count} OOS days)...")
        row = run_config(
            vid, spec["phase"], cfg, dates, args.train_count,
            processed, "SPXW", "signals_unconditional.csv", out.parent,
        )
        src = out.parent / spec["phase"] / vid
        out.mkdir(parents=True, exist_ok=True)
        for name in ("daily_summary.csv", "trades.csv", "stop_diagnostics.csv"):
            (src / name).replace(out / name)

    print(
        f"  CAGR {row['cagr_pct']:.1f}%  Sharpe {row['sharpe']:.2f}  "
        f"worst {row['worst_day_pct']:.1f}%  trades {row['trades']}"
    )


if __name__ == "__main__":
    main()
