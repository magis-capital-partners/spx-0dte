"""P1 homogeneity gate: scaled (contracts, equity) must scale P&L ~linearly."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))

from compounding_sizing import scaled_day_config, scaled_day_policy  # noqa: E402
from expiry_calendar import (  # noqa: E402
    DEFAULT_RULES,
    discover_eligible_dates,
    load_era_rules,
    resolve_start_date,
)
from mbh_simulator import read_quotes_csv, read_signals_csv, simulate_day  # noqa: E402
from profiles import PRODUCTION_TRAIN_COUNT  # noqa: E402
from regime_validation import apply_rolling_baseline, discover_dates  # noqa: E402

PROCESSED = ROOT / "data" / "processed"
KS = (0.5, 1.0, 2.0, 3.0, 4.0)
# Relative error vs nominal k: contract rounding dominates when round(31*k)/31 ≠ k.
# We also check scaling vs *realized* contract ratio, which should be tighter.
REL_TOL_NOMINAL = 0.06
# Residual non-linearity: IC max(1,...) floors, credit-cap clipping, fee discreteness.
REL_TOL_REALIZED = 0.025
ABS_TOL = 100.0  # dollars


def _sample_dates(n: int = 8):
    if not PROCESSED.exists():
        pytest.skip("processed data missing")
    floor, eras = load_era_rules(DEFAULT_RULES)
    dates = discover_dates(PROCESSED, "SPXW")
    if len(dates) < PRODUCTION_TRAIN_COUNT + 50:
        pytest.skip("insufficient processed dates")
    start = resolve_start_date(dates, floor, require_mon_and_wed=True)
    eligible = discover_eligible_dates(dates, floor=start, end=dates[-1], eras=eras)
    step = max(1, (len(eligible) - PRODUCTION_TRAIN_COUNT) // n)
    idxs = [PRODUCTION_TRAIN_COUNT + i * step for i in range(n)]
    idxs = [i for i in idxs if i < len(eligible)]
    return [eligible[i] for i in idxs], eligible


def test_homogeneity_scaled_pnl_tracks_k() -> None:
    sample, eligible = _sample_dates(8)
    errors: list[str] = []

    for test_date in sample:
        index = eligible.index(test_date)
        train = eligible[index - PRODUCTION_TRAIN_COUNT : index]
        day_dir = PROCESSED / "symbol=SPXW" / f"date={test_date}"
        quotes = read_quotes_csv(day_dir / "normalized_option_quotes.csv")
        apply_rolling_baseline(PROCESSED, "SPXW", train, test_date, "signals_unconditional.csv")
        signals = read_signals_csv(day_dir / "signals_unconditional.csv")

        base_cfg = scaled_day_config(1.0)
        base_pol = scaled_day_policy(1.0)
        base = simulate_day(quotes, signals, config=base_cfg, policy=base_pol)
        base_pnl = float(base.net_pnl)
        base_c = sum(t.contracts for t in base.trades) or 0

        for k in KS:
            if abs(k - 1.0) < 1e-12:
                continue
            cfg = scaled_day_config(k)
            pol = scaled_day_policy(k)
            result = simulate_day(quotes, signals, config=cfg, policy=pol)
            actual = float(result.net_pnl)
            res_c = sum(t.contracts for t in result.trades) or 0

            expected_nominal = k * base_pnl
            denom = max(abs(expected_nominal), 1.0)
            rel_nom = abs(actual - expected_nominal) / denom
            if rel_nom > REL_TOL_NOMINAL and abs(actual - expected_nominal) > ABS_TOL:
                errors.append(
                    f"{test_date} k={k}: nominal expected {expected_nominal:,.2f} "
                    f"got {actual:,.2f} (rel {rel_nom:.3%})"
                )

            if base_c > 0 and res_c > 0:
                realized_k = res_c / base_c
                expected_realized = realized_k * base_pnl
                denom_r = max(abs(expected_realized), 1.0)
                rel_r = abs(actual - expected_realized) / denom_r
                if rel_r > REL_TOL_REALIZED and abs(actual - expected_realized) > ABS_TOL:
                    errors.append(
                        f"{test_date} k={k}: realized-k={realized_k:.4f} expected "
                        f"{expected_realized:,.2f} got {actual:,.2f} (rel {rel_r:.3%})"
                    )
                assert abs(realized_k - k) / k <= 0.08, (
                    f"{test_date} k={k}: contracts {res_c}/{base_c}={realized_k:.3f}"
                )

    assert not errors, "Homogeneity failed:\n" + "\n".join(f"  {e}" for e in errors)


def test_scaled_config_fields() -> None:
    cfg = scaled_day_config(2.0)
    assert cfg.account_equity == pytest.approx(26_000_000.0)
    assert cfg.baseline_contracts == 62
    pol = scaled_day_policy(2.0)
    assert pol.max_contracts == 96


if __name__ == "__main__":
    test_scaled_config_fields()
    test_homogeneity_scaled_pnl_tracks_k()
    print("OK")
