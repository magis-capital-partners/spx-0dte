"""Tests for live entry_risk_block wiring and stop-count recovery."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))
sys.path.insert(0, str(ROOT / "live"))

from mbh_simulator import StrategyConfig  # noqa: E402
from live_entry_risk import (  # noqa: E402
    apply_live_risk_overlays,
    live_entry_risk_block,
    recover_side_stop_counts,
)
from live_config import LiveConfig  # noqa: E402


class LiveEntryRiskTests(unittest.TestCase):
    def test_apply_stop_caps(self) -> None:
        cfg = StrategyConfig(max_stops_per_side=999, max_stops_per_day=999)
        live = LiveConfig(live_max_stops_per_side=2, live_max_stops_per_day=4)
        out = apply_live_risk_overlays(cfg, live)
        self.assertEqual(out.max_stops_per_side, 2)
        self.assertEqual(out.max_stops_per_day, 4)
        self.assertFalse(out.use_portfolio_allocator)

    def test_allocator_flag(self) -> None:
        cfg = StrategyConfig(use_portfolio_allocator=False)
        live = LiveConfig(use_portfolio_allocator_live=True)
        out = apply_live_risk_overlays(cfg, live)
        self.assertTrue(out.use_portfolio_allocator)

    def test_condor_is_disabled_unless_paired_execution_is_enabled(self) -> None:
        cfg = StrategyConfig(use_condor_sleeve=True)
        self.assertFalse(apply_live_risk_overlays(cfg, LiveConfig()).use_condor_sleeve)
        self.assertTrue(
            apply_live_risk_overlays(
                cfg, LiveConfig(enable_paired_condor_live=True)
            ).use_condor_sleeve
        )

    def test_side_stop_limit(self) -> None:
        cfg = StrategyConfig(max_stops_per_side=2, max_stops_per_day=4, same_side_stop_cooldown_minutes=0)
        cand = SimpleNamespace(
            side="bear_call",
            short_type="CALL",
            short_strike=7500.0,
            long_strike=7550.0,
            credit_to_width=0.05,
        )
        reason = live_entry_risk_block(
            cand,
            [],
            now=datetime(2026, 7, 18, 11, 0),
            config=cfg,
            side_stop_cooldown_until={},
            side_stop_counts={"bear_call": 2},
        )
        self.assertEqual(reason, "side_stop_limit")

    def test_same_strike_multiplier_supersedes_static_floor(self) -> None:
        # 2026-08-06: max_open_same_strike_multiple=12 (intended cap 12x2=24)
        # was set, but this static field still fed max_open_contracts_same_strike,
        # and that gate ran ahead of open_risk_caps' multiplier-aware one — so a
        # tranche was blocked at 4 contracts against an intended cap of 24.
        cfg = StrategyConfig(max_open_contracts_same_strike=999)
        live = LiveConfig(max_open_same_strike=2, max_open_same_strike_multiple=12.0)
        out = apply_live_risk_overlays(cfg, live)
        self.assertEqual(out.max_open_contracts_same_strike, 0)

    def test_static_floor_applies_when_multiplier_disabled(self) -> None:
        cfg = StrategyConfig(max_open_contracts_same_strike=999)
        live = LiveConfig(max_open_same_strike=2, max_open_same_strike_multiple=0.0)
        out = apply_live_risk_overlays(cfg, live)
        self.assertEqual(out.max_open_contracts_same_strike, 2)

    def test_todays_blocked_tranche_now_passes_gate_one(self) -> None:
        # The exact 2026-08-06 10:17 candidate: 2 open at the strike, 2 more
        # sized, against a static floor of 2 and an intended dynamic cap of 24.
        # max_open_trades_same_side_strike=999 matches the production profile's
        # resolution (a trade-count gate, unrelated to the contracts bug here —
        # left at its class default of 1 it would block on trade count alone).
        cfg = StrategyConfig(
            max_open_contracts_same_strike=0,  # post-fix resolution
            max_open_trades_same_side_strike=999,
        )
        cand = SimpleNamespace(
            side="bear_call",
            short_type="CALL",
            short_strike=7760.0,
            long_strike=7835.0,
            credit_to_width=0.05,
            contracts=2,
            sleeve="core",
        )
        open_trade = SimpleNamespace(
            side="bear_call",
            short_type="CALL",
            short_strike=7760.0,
            long_strike=7835.0,
            contracts=2,
            exit_reason="open",
            stopped=False,
        )
        reason = live_entry_risk_block(
            cand,
            [SimpleNamespace(candidate=open_trade, contracts=2, closed=False)],
            now=datetime(2026, 8, 6, 10, 17, 1),
            config=cfg,
            side_stop_cooldown_until={},
            side_stop_counts={},
        )
        self.assertFalse(reason)

    def test_recover_counts(self) -> None:
        events = [
            {"event": "stop", "side": "bear_call"},
            {"event": "stop", "side": "bear_call"},
            {"event": "stop", "side": "bull_put"},
            {"event": "entry", "side": "bear_call"},
        ]
        counts = recover_side_stop_counts(events)
        self.assertEqual(counts, {"bear_call": 2, "bull_put": 1})


if __name__ == "__main__":
    unittest.main()
