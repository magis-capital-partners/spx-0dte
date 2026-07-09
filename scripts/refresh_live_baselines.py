"""Refresh rolling signal baselines for live execution.

Uses the same 40-day eligible-date train window as backtest
(``apply_rolling_baseline`` / ``historical_3d_backtest``).

Usage:
    python scripts/refresh_live_baselines.py
    python scripts/refresh_live_baselines.py --out data/models/live_signal_baselines.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIMULATOR = ROOT / "simulator"
sys.path.insert(0, str(SIMULATOR))

from expiry_calendar import (  # noqa: E402
    DEFAULT_RULES,
    discover_eligible_dates,
    load_era_rules,
    resolve_start_date,
)
from historical_baselines import compute_baselines  # noqa: E402
from live_features import baselines_payload_for_live  # noqa: E402
from profiles import PRODUCTION_TRAIN_COUNT  # noqa: E402
from regime_validation import discover_dates  # noqa: E402

DEFAULT_OUT = ROOT / "data" / "models" / "live_signal_baselines.json"
DEFAULT_PROCESSED = ROOT / "data" / "processed"


def resolve_train_dates(
    eligible: list[str],
    *,
    as_of: str,
    train_count: int,
) -> list[str]:
    """Last ``train_count`` eligible dates strictly before ``as_of``."""
    prior = [d for d in eligible if d < as_of]
    if len(prior) >= train_count:
        return prior[-train_count:]
    if len(eligible) >= train_count:
        return eligible[-train_count:]
    raise SystemExit(
        f"Need at least {train_count} eligible dates for baselines; have {len(eligible)}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Build live rolling signal baselines JSON.")
    parser.add_argument("--processed-dir", default=str(DEFAULT_PROCESSED))
    parser.add_argument("--symbol", default="SPXW")
    parser.add_argument("--train-count", type=int, default=PRODUCTION_TRAIN_COUNT)
    parser.add_argument("--as-of", default=date.today().isoformat(), help="Reference date (default: today).")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--rules-file", default=str(DEFAULT_RULES))
    args = parser.parse_args()

    processed = Path(args.processed_dir)
    floor, eras = load_era_rules(Path(args.rules_file))
    processed_dates = discover_dates(processed, args.symbol)
    if not processed_dates:
        raise SystemExit(f"No processed dates under {processed}")

    resolved_end = processed_dates[-1]
    resolved_start = resolve_start_date(processed_dates, floor, require_mon_and_wed=True)
    eligible = discover_eligible_dates(
        processed_dates,
        floor=resolved_start,
        end=resolved_end,
        eras=eras,
    )
    train_dates = resolve_train_dates(eligible, as_of=args.as_of, train_count=args.train_count)
    baselines = compute_baselines(processed, args.symbol, train_dates)
    payload = baselines_payload_for_live(baselines, train_dates)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"Wrote {out}")
    print(f"  train_dates: {train_dates[0]} -> {train_dates[-1]} ({len(train_dates)} days)")
    print(f"  as_of: {args.as_of}")
    for feature in baselines["features"]:
        g = baselines["global"][feature]
        print(f"  global {feature}: mean={g['mean']:.4f} std={g['std']:.4f} n={g['count']}")


if __name__ == "__main__":
    main()
