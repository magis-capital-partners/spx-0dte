"""Simulator stop confirm seconds (parity with live 120s)."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))

from mbh_simulator import (  # noqa: E402
    OptionQuote,
    StrategyConfig,
    Trade,
    process_stops,
)


def _trade(stop_price: float = 3.0) -> Trade:
    return Trade(
        trade_id=1,
        entry_time=datetime(2026, 7, 9, 10, 0, 0),
        expiry="2026-07-09",
        side="bear_call",
        model="core",
        contracts=1,
        short_type="CALL",
        short_strike=7550.0,
        long_type="CALL",
        long_strike=7625.0,
        short_entry_sell=1.0,
        long_entry_buy=0.2,
        entry_credit=0.8,
        stop_price=stop_price,
    )


def _snap(ts: datetime, ask: float) -> list:
    return [
        OptionQuote(ts, "2026-07-09", "CALL", 7550.0, ask - 0.05, ask, underlying_price=7500.0),
        OptionQuote(ts, "2026-07-09", "CALL", 7625.0, 0.10, 0.15, underlying_price=7500.0),
    ]


class StopConfirmSecondsTests(unittest.TestCase):
    def test_needs_120s_on_minute_bars(self) -> None:
        cfg = StrategyConfig(
            use_short_leg_stops=True,
            stop_confirm_seconds=120.0,
            stop_confirmation_count=2,
            stop_fill_slippage=0.25,
        )
        trade = _trade()
        t0 = datetime(2026, 7, 9, 11, 0, 0)
        # First breach
        self.assertEqual(process_stops([trade], t0, _snap(t0, 3.10), cfg), [])
        self.assertFalse(trade.stopped)
        # +60s still confirming
        t1 = t0 + timedelta(seconds=60)
        self.assertEqual(process_stops([trade], t1, _snap(t1, 3.10), cfg), [])
        self.assertFalse(trade.stopped)
        # +120s fires
        t2 = t0 + timedelta(seconds=120)
        stopped = process_stops([trade], t2, _snap(t2, 3.10), cfg)
        self.assertEqual(len(stopped), 1)
        self.assertTrue(trade.stopped)
        self.assertAlmostEqual(trade.stop_fill, 3.10 + 0.25)

    def test_blowthrough_fill_not_better_than_stop(self) -> None:
        cfg = StrategyConfig(
            use_short_leg_stops=True,
            stop_confirm_seconds=0.0,
            stop_confirmation_count=1,
            stop_fill_slippage=0.25,
        )
        trade = _trade(stop_price=5.0)
        t0 = datetime(2026, 7, 9, 11, 0, 0)
        # Ask somehow below stop but mode still triggers? Force by setting ask >= stop
        process_stops([trade], t0, _snap(t0, 5.50), cfg)
        self.assertTrue(trade.stopped)
        self.assertAlmostEqual(trade.stop_fill, 5.50 + 0.25)


if __name__ == "__main__":
    unittest.main()
