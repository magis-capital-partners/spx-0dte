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
