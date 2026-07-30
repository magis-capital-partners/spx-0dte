"""Sweep elevated VIX (25–35) sizing multipliers with VIX>35 skip.

Compares skip_gt35 baseline against elevated-band upscale at 1.25× … 3×, with an
optional per-tranche contract cap (mirrors live ``max_contracts_per_tranche``).

Usage:
    python scripts/run_vix_elevated_scale_tests.py
    python scripts/run_vix_elevated_scale_tests.py --baseline-contracts 2 --account-equity 500000 --max-contracts 0
    python scripts/run_vix_elevated_scale_tests.py --max-contracts 6 --max-oos-days 200

Outputs: data/vix_elevated_scale_tests/summary.json + summary.csv
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
    PRODUCTION_BASELINE_CONTRACTS,
    PRODUCTION_SIZING_SCHEME,
    PRODUCTION_TRAIN_COUNT,
    SCHEMES,
    build_p3_poststop_cooldown_config,
)
from regime_validation import apply_rolling_baseline, discover_dates  # noqa: E402
from stop_calibration_runner import DEFAULT_PROCESSED  # noqa: E402
from unconditional_baseline import trade_stats  # noqa: E402
from vix_sizing_policies import VixElevatedSkipPolicy  # noqa: E402

RESULTS = ROOT / "data" / "vix_elevated_scale_tests"
DEFAULT_ELEVATED_SCALES = (1.25, 1.5, 1.75, 2.0, 2.5, 3.0)


def build_variants(
    schedule,
    *,
    max_contracts: Optional[int],
    elevated_scales: Tuple[float, ...],
) -> Dict[str, Tuple[StrategyConfig, Callable]]:
    def policy(scale: float = 1.0) -> VixElevatedSkipPolicy:
        return VixElevatedSkipPolicy(
            schedule,
            elevated_scale=scale,
            max_contracts=max_contracts,
        )

    variants: Dict[str, Tuple[StrategyConfig, Callable]] = {
        "skip_gt35_only": (None, lambda: policy(1.0)),  # config filled by caller
    }
    for scale in elevated_scales:
        tag = f"elevated_{str(scale).replace('.', '')}x"
        variants[tag] = (None, lambda s=scale: policy(s))
    return variants


def run_matrix(
    *,
    processed_dir: Path,
    train_count: int,
    config: StrategyConfig,
    schedule,
    max_contracts: Optional[int],
    elevated_scales: Tuple[float, ...],
    max_oos_days: int = 0,
    signals_filename: str = "signals_unconditional.csv",
    results_dir: Optional[Path] = None,
) -> List[dict]:
    floor, eras = load_era_rules(DEFAULT_RULES)
    processed_dates = discover_dates(processed_dir, "SPXW")
    resolved_start = resolve_start_date(processed_dates, floor, require_mon_and_wed=True)
    end_date = processed_dates[-1]
    eligible = discover_eligible_dates(processed_dates, floor=resolved_start, end=end_date, eras=eras)
    if len(eligible) <= train_count:
        raise SystemExit(f"Need more than {train_count} eligible dates; have {len(eligible)}")
    if max_oos_days > 0:
        eligible = eligible[: train_count + max_oos_days]

    variants = build_variants(schedule, max_contracts=max_contracts, elevated_scales=elevated_scales)
    daily_by: Dict[str, List[dict]] = {name: [] for name in variants}
    trades_by: Dict[str, List[dict]] = {name: [] for name in variants}
    oos = len(eligible) - train_count
    cap_label = "none" if max_contracts is None else str(max_contracts)
    print(
        f"VIX elevated scale tests: {len(variants)} variants × {oos} OOS days "
        f"(baseline={config.baseline_contracts} cap={cap_label})",
        flush=True,
    )

    for index in range(train_count, len(eligible)):
        test_date = eligible[index]
        train_dates = eligible[index - train_count : index]
        apply_rolling_baseline(processed_dir, "SPXW", train_dates, test_date, signals_filename)
        day_dir = processed_dir / "symbol=SPXW" / f"date={test_date}"
        quotes = read_quotes_csv(day_dir / "normalized_option_quotes.csv")
        signals = read_signals_csv(day_dir / signals_filename)

        for name, (_, policy_factory) in variants.items():
            policy = policy_factory()
            result = simulate_day(quotes, signals, config=config, policy=policy)
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
    for name in variants:
        daily_rows = daily_by[name]
        spread_trades = [r for r in trades_by[name] if r.get("model") != "net_long_overlay"]
        ts = trade_stats(spread_trades)
        port = portfolio_stats(daily_rows, config.account_equity)
        summary_rows.append(
            {
                "variant": name,
                "baseline_contracts": config.baseline_contracts,
                "max_contracts_per_tranche": max_contracts,
                "account_equity": config.account_equity,
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
    return summary_rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Elevated VIX sizing multiplier sweep (skip VIX>35).")
    parser.add_argument("--processed-dir", type=Path, default=DEFAULT_PROCESSED)
    parser.add_argument("--train-count", type=int, default=PRODUCTION_TRAIN_COUNT)
    parser.add_argument("--max-oos-days", type=int, default=0, help="0 = full OOS eligible calendar")
    parser.add_argument("--signals-filename", default="signals_unconditional.csv")
    parser.add_argument("--results-dir", type=Path, default=RESULTS)
    parser.add_argument("--baseline-contracts", type=int, default=PRODUCTION_BASELINE_CONTRACTS)
    parser.add_argument("--account-equity", type=float, default=PRODUCTION_ACCOUNT_EQUITY)
    parser.add_argument(
        "--max-contracts",
        type=int,
        default=0,
        help="Per-tranche cap (0 = no cap, mirrors loosened live limit)",
    )
    parser.add_argument(
        "--elevated-scales",
        default="",
        help="Comma-separated multipliers (default: 1.25,1.5,1.75,2,2.5,3)",
    )
    args = parser.parse_args()

    scales = DEFAULT_ELEVATED_SCALES
    if args.elevated_scales.strip():
        scales = tuple(float(x.strip()) for x in args.elevated_scales.split(",") if x.strip())

    max_contracts: Optional[int] = None if args.max_contracts <= 0 else args.max_contracts
    schedule = SCHEMES[PRODUCTION_SIZING_SCHEME]
    config = build_p3_poststop_cooldown_config(
        account_equity=args.account_equity,
        baseline_contracts=args.baseline_contracts,
    )

    args.results_dir.mkdir(parents=True, exist_ok=True)
    summary_rows = run_matrix(
        processed_dir=args.processed_dir,
        train_count=args.train_count,
        config=config,
        schedule=schedule,
        max_contracts=max_contracts,
        elevated_scales=scales,
        max_oos_days=args.max_oos_days,
        signals_filename=args.signals_filename,
        results_dir=args.results_dir,
    )
    write_csv(args.results_dir / "summary.csv", summary_rows)
    meta = {
        "baseline_contracts": args.baseline_contracts,
        "account_equity": args.account_equity,
        "max_contracts_per_tranche": max_contracts,
        "elevated_scales": list(scales),
        "skip_above": 35.0,
        "elevated_band": [25.0, 35.0],
    }
    payload = {"meta": meta, "variants": summary_rows}
    (args.results_dir / "summary.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print("\n=== VIX elevated scale tests (skip VIX>35) ===")
    for row in sorted(summary_rows, key=lambda r: r.get("cagr_pct", 0), reverse=True):
        print(
            f"  {row['variant']:<22} CAGR={row['cagr_pct']:>6.1f}%  "
            f"worst={row['worst_day_pct']:>6.2f}%  maxDD={row['max_drawdown_pct']:>6.2f}%  "
            f"sharpe={row['sharpe']:>5.2f}  trades={row['total_trades']}"
        )
    print(f"\nWrote {args.results_dir / 'summary.json'}")


if __name__ == "__main__":
    main()
