"""Regression checks for canonical strategy profiles."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from profiles import (  # noqa: E402
    PRODUCTION_PROFILE,
    PRODUCTION_SIZING_SCHEME,
    PROFILE_BUILDERS,
    build_3d_flatten_config,
    build_p3_poststop_cooldown_config,
    build_p3_trend_skew_config,
)


def test_production_profile_gates() -> None:
    cfg = build_p3_trend_skew_config()
    assert cfg.candidate_max_adverse_trend == 1.0
    assert cfg.candidate_max_adverse_skew == 0.75
    assert cfg.flatten_on_daily_loss is True
    assert cfg.flatten_loss_limit_pct == 0.035
    assert cfg.stop_multiple == 3.0
    assert cfg.put_wing_width == 200.0
    assert cfg.call_wing_width == 75.0


def test_profile_registry() -> None:
    assert PRODUCTION_PROFILE in PROFILE_BUILDERS
    assert PROFILE_BUILDERS[PRODUCTION_PROFILE] is build_p3_poststop_cooldown_config
    assert PROFILE_BUILDERS["p3_trend1_skew075"] is build_p3_trend_skew_config
    base = build_3d_flatten_config()
    assert base.candidate_max_adverse_trend == 99.0
    assert base.candidate_max_adverse_skew == 99.0


def test_sizing_scheme_exists() -> None:
    from profiles import SCHEMES

    assert PRODUCTION_SIZING_SCHEME in SCHEMES
    assert len(SCHEMES[PRODUCTION_SIZING_SCHEME]) >= 5


def test_poststop_profile_cooldown() -> None:
    cfg = build_p3_poststop_cooldown_config()
    assert cfg.same_side_stop_cooldown_minutes == 120
    assert cfg.candidate_max_adverse_trend == 1.0
    assert cfg.candidate_max_adverse_skew == 0.65
    assert cfg.flatten_loss_limit_pct == 0.0325
    assert cfg.daily_loss_limit_pct == 0.0225
    assert cfg.put_wing_width == 150.0  # Wave 2 Calmar winner put_wing_150
    assert cfg.call_wing_width == 75.0
    assert cfg.entry_fill_slippage == 0.05
    assert cfg.stop_fill_slippage == 0.25
    assert cfg.fee_per_contract == 1.25
    assert cfg.stop_confirm_seconds == 120.0
    assert "p3_poststop_cooldown_120" in PROFILE_BUILDERS


def test_trend_bc_085_profile() -> None:
    from profiles import build_p3_trend_bc_085_config

    cfg = build_p3_trend_bc_085_config()
    assert cfg.candidate_max_adverse_trend == 0.85
    assert cfg.put_wing_width == 150.0
    assert cfg.candidate_max_adverse_skew == 0.65
    assert cfg.stop_fill_slippage == 0.25
    assert cfg.stop_confirm_seconds == 120.0
    assert "p3_trend_bc_085" in PROFILE_BUILDERS


def main() -> None:
    test_production_profile_gates()
    test_profile_registry()
    test_sizing_scheme_exists()
    test_poststop_profile_cooldown()
    test_trend_bc_085_profile()
    print("profile regression: PASS")


if __name__ == "__main__":
    main()
