"""Tests for VIX elevated skip + upscale sizing policy."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))

from mbh_simulator import SignalSnapshot, StrategyConfig  # noqa: E402
from profiles import SCHEMES, PRODUCTION_SIZING_SCHEME  # noqa: E402
from vix_sizing_policies import VixElevatedSkipPolicy  # noqa: E402


def _signal(vix: float) -> SignalSnapshot:
    return SignalSnapshot(timestamp=datetime(2024, 6, 15, 10, 0, 0), vix=vix)


def _config(baseline: int = 2) -> StrategyConfig:
    return StrategyConfig(baseline_contracts=baseline)


def test_skip_above_35() -> None:
    policy = VixElevatedSkipPolicy(SCHEMES[PRODUCTION_SIZING_SCHEME], elevated_scale=2.0)
    assert policy.contracts(_signal(36.0), _config()) == 0


def test_elevated_band_scales() -> None:
    schedule = SCHEMES[PRODUCTION_SIZING_SCHEME]
    policy = VixElevatedSkipPolicy(schedule, elevated_scale=2.0, max_contracts=None)
    low = policy.contracts(_signal(18.0), _config())
    high = policy.contracts(_signal(30.0), _config())
    assert high >= low
    assert high == round(low * 2.0) or high > low


def test_max_contracts_cap() -> None:
    schedule = SCHEMES[PRODUCTION_SIZING_SCHEME]
    policy = VixElevatedSkipPolicy(schedule, elevated_scale=3.0, max_contracts=2)
    capped = policy.contracts(_signal(30.0), _config(baseline=2))
    assert capped <= 2


def test_production_vix_policy_peak_contracts() -> None:
    from profiles import PRODUCTION_BASELINE_CONTRACTS, PRODUCTION_SIZING_SCHEME, SCHEMES
    from vix_sizing_policies import build_production_vix_policy

    policy = build_production_vix_policy(SCHEMES[PRODUCTION_SIZING_SCHEME], max_contracts=48)
    signal = SignalSnapshot(timestamp=datetime(2024, 6, 15, 9, 32, 0), vix=30.0)
    cfg = StrategyConfig(baseline_contracts=PRODUCTION_BASELINE_CONTRACTS)
    assert policy.contracts(signal, cfg) == 48


if __name__ == "__main__":
    test_skip_above_35()
    test_elevated_band_scales()
    test_max_contracts_cap()
    test_production_vix_policy_peak_contracts()
    print("ok")
