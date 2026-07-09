"""Verify live feature path matches backtest signals on processed history.

Gate parity (production-critical): ``trend_score`` and ``skew_z`` z-scores must
match ``signals_unconditional.csv`` — those drive ``p3_trend1_skew075`` entry gates.

Usage:
    python simulator/test_live_signal_parity.py
    python simulator/test_live_signal_parity.py --dates 2024-06-15 2025-01-10
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))

from expiry_calendar import (  # noqa: E402
    DEFAULT_RULES,
    discover_eligible_dates,
    load_era_rules,
    resolve_start_date,
)
from historical_baselines import FEATURES, compute_baselines, read_csv, safe_float  # noqa: E402
from live_features import (  # noqa: E402
    SessionFeatureState,
    compute_raw_features,
    split_session_quotes,
    zscore_raw_features,
)
from mbh_simulator import OptionQuote, read_quotes_csv  # noqa: E402
from profiles import PRODUCTION_TRAIN_COUNT  # noqa: E402
from regime_validation import apply_rolling_baseline, discover_dates  # noqa: E402

DEFAULT_PROCESSED = ROOT / "data" / "processed"
# Production gates use these z-scored features only.
GATE_FEATURES = ("trend_score", "skew_z")
RAW_EPS = 0.05
Z_EPS = 0.08
RECENT_FLOOR = "2023-01-01"


def _group_quotes(quotes: Sequence[OptionQuote]) -> Dict[str, List[OptionQuote]]:
    grouped: Dict[str, List[OptionQuote]] = defaultdict(list)
    for q in quotes:
        key = q.timestamp.replace(tzinfo=None).isoformat(timespec="seconds")
        grouped[key].append(q)
    return grouped


def _normalize_ts_key(ts: str) -> str:
    return ts.replace(" EST", "").replace(" EDT", "").split(".")[0]


def _pick_sample_dates(eligible: List[str], train_count: int, n: int, recent_floor: str) -> List[str]:
    oos = eligible[train_count:]
    recent = [d for d in oos if d >= recent_floor]
    pool = recent if len(recent) >= max(3, n) else oos
    if n >= len(pool):
        return pool
    step = max(1, len(pool) // n)
    picks = [pool[i * step] for i in range(n)]
    if picks[-1] != pool[-1]:
        picks.append(pool[-1])
    return picks[:n]


def check_day(
    test_date: str,
    processed: Path,
    train_dates: List[str],
    *,
    raw_eps: float,
    z_eps: float,
) -> Tuple[int, int, int, int, List[str]]:
    symbol = "SPXW"
    day_dir = processed / f"symbol={symbol}" / f"date={test_date}"
    quotes_path = day_dir / "normalized_option_quotes.csv"
    raw_signals_path = day_dir / "signals.csv"
    if not quotes_path.exists() or not raw_signals_path.exists():
        return 0, 0, 0, 0, [f"{test_date}: missing quotes or signals.csv"]

    baselines = compute_baselines(processed, symbol, train_dates)
    apply_rolling_baseline(processed, symbol, train_dates, test_date, "signals_unconditional.csv")
    zscored_rows = read_csv(day_dir / "signals_unconditional.csv")
    z_by_ts = {_normalize_ts_key(r["timestamp"]): r for r in zscored_rows}

    all_quotes = read_quotes_csv(quotes_path)
    quotes_by_ts = _group_quotes(all_quotes)
    raw_rows = read_csv(raw_signals_path)

    state = SessionFeatureState()
    raw_ok = raw_total = 0
    z_ok = z_total = 0
    failures: List[str] = []

    for row in raw_rows:
        ts_key = _normalize_ts_key(row["timestamp"])
        bucket = quotes_by_ts.get(ts_key)
        if not bucket:
            continue
        zero_q, next_q = split_session_quotes(bucket, test_date)
        if not zero_q:
            continue
        spot_vals = [q.underlying_price for q in zero_q if q.underlying_price]
        if not spot_vals:
            continue
        spot = float(spot_vals[0])
        ts = datetime.fromisoformat(ts_key)

        next_bucket = None
        if next_q:
            next_by_ts = _group_quotes(next_q)
            next_bucket = next_by_ts.get(ts_key)

        computed = compute_raw_features(zero_q, spot, ts, state, next_expiry_quotes=next_bucket)

        for feature in GATE_FEATURES:
            expected = safe_float(row.get(feature))
            actual = computed[feature]
            raw_total += 1
            if abs(actual - expected) <= raw_eps:
                raw_ok += 1
            elif len(failures) < 20:
                failures.append(
                    f"{test_date} {ts_key} raw {feature}: live={actual:.4f} csv={expected:.4f}"
                )

        zscored = zscore_raw_features(computed, baselines, ts)
        ref = z_by_ts.get(ts_key)
        if not ref:
            continue
        for feature in GATE_FEATURES:
            expected_z = safe_float(ref.get(feature))
            actual_z = zscored[feature]
            z_total += 1
            if abs(actual_z - expected_z) <= z_eps:
                z_ok += 1
            elif len(failures) < 20:
                failures.append(
                    f"{test_date} {ts_key} z {feature}: live={actual_z:.4f} csv={expected_z:.4f}"
                )

    return raw_ok, raw_total, z_ok, z_total, failures


def main() -> None:
    parser = argparse.ArgumentParser(description="Live vs backtest signal parity (gate features).")
    parser.add_argument("--processed-dir", default=str(DEFAULT_PROCESSED))
    parser.add_argument("--train-count", type=int, default=PRODUCTION_TRAIN_COUNT)
    parser.add_argument("--sample-days", type=int, default=8)
    parser.add_argument("--dates", nargs="*", default=[], help="Explicit dates to test.")
    parser.add_argument("--recent-floor", default=RECENT_FLOOR)
    parser.add_argument("--raw-eps", type=float, default=RAW_EPS)
    parser.add_argument("--z-eps", type=float, default=Z_EPS)
    parser.add_argument("--min-raw-pct", type=float, default=95.0)
    parser.add_argument("--min-z-pct", type=float, default=95.0)
    args = parser.parse_args()

    processed = Path(args.processed_dir)
    floor, eras = load_era_rules(DEFAULT_RULES)
    processed_dates = discover_dates(processed, "SPXW")
    resolved_start = resolve_start_date(processed_dates, floor, require_mon_and_wed=True)
    eligible = discover_eligible_dates(
        processed_dates,
        floor=resolved_start,
        end=processed_dates[-1],
        eras=eras,
    )
    if len(eligible) <= args.train_count:
        raise SystemExit(f"Need >{args.train_count} eligible dates; have {len(eligible)}")

    if args.dates:
        test_dates = [d for d in args.dates if d in eligible]
    else:
        test_dates = _pick_sample_dates(eligible, args.train_count, args.sample_days, args.recent_floor)

    if not test_dates:
        raise SystemExit("No test dates selected.")

    print(f"Gate features: {', '.join(GATE_FEATURES)} (production entry filters)")

    total_raw_ok = total_raw = total_z_ok = total_z = 0
    all_failures: List[str] = []

    for test_date in test_dates:
        idx = eligible.index(test_date)
        train_dates = eligible[idx - args.train_count : idx]
        raw_ok, raw_n, z_ok, z_n, fails = check_day(
            test_date,
            processed,
            train_dates,
            raw_eps=args.raw_eps,
            z_eps=args.z_eps,
        )
        total_raw_ok += raw_ok
        total_raw += raw_n
        total_z_ok += z_ok
        total_z += z_n
        all_failures.extend(fails)
        raw_pct = 100.0 * raw_ok / raw_n if raw_n else 0.0
        z_pct = 100.0 * z_ok / z_n if z_n else 0.0
        print(f"  {test_date}: raw {raw_ok}/{raw_n} ({raw_pct:.1f}%)  z {z_ok}/{z_n} ({z_pct:.1f}%)")

    raw_pct = 100.0 * total_raw_ok / total_raw if total_raw else 0.0
    z_pct = 100.0 * total_z_ok / total_z if total_z else 0.0
    print(f"\nTOTAL (gate features): raw {total_raw_ok}/{total_raw} ({raw_pct:.1f}%)  "
          f"z {total_z_ok}/{total_z} ({z_pct:.1f}%)")

    if all_failures:
        print("\nSample failures:")
        for line in all_failures[:15]:
            print(f"  {line}")

    passed = raw_pct >= args.min_raw_pct and z_pct >= args.min_z_pct
    if not passed:
        raise SystemExit(
            f"FAIL: raw {raw_pct:.1f}% (need {args.min_raw_pct}%), "
            f"z {z_pct:.1f}% (need {args.min_z_pct}%)"
        )
    print("PASS")


if __name__ == "__main__":
    main()
