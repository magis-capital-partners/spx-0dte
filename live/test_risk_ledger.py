"""Defined-risk and return-on-margin ledger tests."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "live"))
from risk_ledger import build_risk_snapshot  # noqa: E402


class RiskLedgerTests(unittest.TestCase):
    def test_open_vertical_calculates_risk_margin_and_mark(self) -> None:
        candidate = SimpleNamespace(side="bull_put", short_type="PUT", short_strike=7395.0, long_strike=7245.0)
        spread = SimpleNamespace(candidate=candidate, contracts=1, fill_credit=6.5,
                                 stop_price=30.6, stopped=False, closed=False, condor_id=None)
        quotes = [SimpleNamespace(option_type="PUT", strike=7395.0, ask=8.0, bid=7.8),
                  SimpleNamespace(option_type="PUT", strike=7245.0, ask=0.3, bid=0.2)]
        result = build_risk_snapshot([spread], quotes, multiplier=100)
        self.assertEqual(result["max_loss_no_stop"], 14350.0)
        self.assertEqual(result["planned_stop_loss"], 2410.0)
        self.assertEqual(result["defined_risk_margin"], 15000.0)
        self.assertEqual(result["marked_pnl"], -130.0)
        self.assertAlmostEqual(result["marked_return_on_margin_pct"], -0.87, places=2)

    def test_stopped_short_has_no_remaining_defined_loss(self) -> None:
        candidate = SimpleNamespace(side="bull_put", short_type="PUT", short_strike=7395.0, long_strike=7245.0)
        spread = SimpleNamespace(candidate=candidate, contracts=1, fill_credit=6.5,
                                 stop_price=30.6, stop_fill_price=30.6, stopped=True, closed=False, condor_id=None)
        quotes = [SimpleNamespace(option_type="PUT", strike=7245.0, ask=0.3, bid=0.2)]
        result = build_risk_snapshot([spread], quotes, multiplier=100)
        self.assertEqual(result["max_loss_no_stop"], 0.0)
        self.assertEqual(result["planned_stop_loss"], 0.0)
        self.assertEqual(result["defined_risk_margin"], 0.0)
        self.assertEqual(result["marked_pnl"], -2390.0)


if __name__ == "__main__":
    unittest.main()
