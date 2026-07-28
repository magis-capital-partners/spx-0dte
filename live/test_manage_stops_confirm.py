"""Synthetic stop confirmation uses wall-clock seconds, not poll count alone."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))
sys.path.insert(0, str(ROOT / "live"))

from mbh_simulator import CandidateRecord, OptionQuote, StrategyConfig  # noqa: E402
from live_config import LiveConfig  # noqa: E402
from ib_executor import OpenSpread, manage_stops  # noqa: E402


def _spread(short: float = 7550.0, short_entry: float = 1.0) -> OpenSpread:
    now = datetime(2026, 7, 9, 11, 0, 0)
    cand = CandidateRecord(
        timestamp=now,
        side="bear_call",
        status="pass",
        reason="",
        score=1.0,
        expiry="20260709",
        short_type="CALL",
        short_strike=short,
        long_strike=short + 75,
        short_delta=-0.2,
        long_delta=-0.05,
        spot=7500.0,
        distance_pct=0.01,
        width=75.0,
        credit=0.5,
        credit_to_width=0.5 / 75.0,
        stop_loss_to_credit=3.0,
        straddle_residual_z=0.0,
        skew_z=0.0,
        term_ratio_z=0.0,
        trend_score=0.0,
        realized_vs_implied_z=0.0,
    )
    return OpenSpread(
        candidate=cand,
        contracts=1,
        short_entry_sell=short_entry,
        long_entry_buy=0.2,
        stop_price=short_entry * 3.0,
    )


class ManageStopsConfirmTests(unittest.TestCase):
    def test_brief_spike_does_not_stop(self) -> None:
        live = LiveConfig(stop_confirm_seconds=120.0)
        config = StrategyConfig(stop_multiple=3.0, stop_confirmation_count=2, use_short_leg_stops=True)
        spread = _spread()
        t0 = datetime(2026, 7, 9, 11, 0, 0)
        ask = spread.stop_price + 0.10
        q = [OptionQuote(t0, "20260709", "CALL", 7550.0, ask - 0.05, ask)]
        stopped = manage_stops(None, [spread], q, config, "2026-07-09", True, live, now=t0)
        self.assertEqual(stopped, [])
        self.assertFalse(spread.stopped)
        # 30s later still below confirm window
        t1 = t0 + timedelta(seconds=30)
        stopped = manage_stops(None, [spread], q, config, "2026-07-09", True, live, now=t1)
        self.assertEqual(stopped, [])
        self.assertFalse(spread.stopped)

    def test_sustained_breach_stops(self) -> None:
        live = LiveConfig(stop_confirm_seconds=120.0)
        config = StrategyConfig(stop_multiple=3.0, stop_confirmation_count=2, use_short_leg_stops=True)
        spread = _spread()
        t0 = datetime(2026, 7, 9, 11, 0, 0)
        ask = spread.stop_price + 0.10
        q = [OptionQuote(t0, "20260709", "CALL", 7550.0, ask - 0.05, ask)]
        manage_stops(None, [spread], q, config, "2026-07-09", True, live, now=t0)
        t1 = t0 + timedelta(seconds=120)
        stopped = manage_stops(None, [spread], q, config, "2026-07-09", True, live, now=t1)
        self.assertEqual(len(stopped), 1)
        self.assertTrue(spread.stopped)

    def test_reset_on_ask_recede(self) -> None:
        live = LiveConfig(stop_confirm_seconds=120.0)
        config = StrategyConfig(stop_multiple=3.0, use_short_leg_stops=True)
        spread = _spread()
        t0 = datetime(2026, 7, 9, 11, 0, 0)
        hot = spread.stop_price + 0.10
        cool = spread.stop_price - 0.50
        manage_stops(
            None, [spread],
            [OptionQuote(t0, "20260709", "CALL", 7550.0, hot - 0.05, hot)],
            config, "2026-07-09", True, live, now=t0,
        )
        t1 = t0 + timedelta(seconds=60)
        manage_stops(
            None, [spread],
            [OptionQuote(t1, "20260709", "CALL", 7550.0, cool - 0.05, cool)],
            config, "2026-07-09", True, live, now=t1,
        )
        self.assertIsNone(spread.stop_breach_since)
        t2 = t1 + timedelta(seconds=120)
        stopped = manage_stops(
            None, [spread],
            [OptionQuote(t2, "20260709", "CALL", 7550.0, hot - 0.05, hot)],
            config, "2026-07-09", True, live, now=t2,
        )
        # Fresh breach at t2 — not yet confirmed
        self.assertEqual(stopped, [])
        self.assertFalse(spread.stopped)


if __name__ == "__main__":
    unittest.main()
