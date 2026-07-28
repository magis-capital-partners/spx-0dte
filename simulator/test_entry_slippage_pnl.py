"""Entry slippage must reduce settled P&L (not only entry_credit field)."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))

from mbh_simulator import (  # noqa: E402
    OptionQuote,
    StrategyConfig,
    open_trade,
    settle_trade,
)


class EntrySlippagePnLTests(unittest.TestCase):
    def test_slippage_reduces_short_entry_and_pnl(self) -> None:
        ts = datetime(2026, 7, 9, 10, 0, 0)
        short = OptionQuote(ts, "2026-07-09", "CALL", 5500.0, 2.45, 2.55, underlying_price=5400.0, delta=-0.20)
        long = OptionQuote(ts, "2026-07-09", "CALL", 5575.0, 0.40, 0.50, underlying_price=5400.0, delta=-0.05)

        base_cfg = StrategyConfig(entry_fill_slippage=0.0, stop_multiple=3.0, fee_per_contract=0.0)
        slip_cfg = StrategyConfig(entry_fill_slippage=0.05, stop_multiple=3.0, fee_per_contract=0.0)

        base = open_trade(1, ts, "bear_call", "core", 1, short, long, base_cfg)
        slipped = open_trade(2, ts, "bear_call", "core", 1, short, long, slip_cfg)
        assert base is not None and slipped is not None

        self.assertAlmostEqual(base.short_entry_sell, 2.45)
        self.assertAlmostEqual(slipped.short_entry_sell, 2.40)
        self.assertAlmostEqual(slipped.entry_credit, base.entry_credit - 0.05, places=4)
        self.assertAlmostEqual(slipped.stop_price, 2.40 * 3.0, places=4)

        close_spot = 5400.0
        settle_trade(base, close_spot, [short, long], base_cfg)
        settle_trade(slipped, close_spot, [short, long], slip_cfg)
        # Both expire OTM → intrinsic 0; slipped book should be $5 worse per contract.
        self.assertAlmostEqual(base.net_pnl - slipped.net_pnl, 5.0, places=2)


if __name__ == "__main__":
    unittest.main()
