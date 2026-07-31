"""Regression coverage for the 2026-07-31 IB price-collar rejection."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "live"))

from combo_pricing import ComboQuote, protect_credit_limit  # noqa: E402


class ComboPricingTests(unittest.TestCase):
    def test_july_31_price_collar_replay(self) -> None:
        # IB rejected BUY -6.00 and said the order must be no more aggressive
        # than -6.30 while the BAG market was -6.95.
        decision = protect_credit_limit(6.00, ComboQuote(bid=-6.95, ask=-6.30))
        self.assertTrue(decision.ok)
        self.assertEqual(decision.collar_credit, 6.30)
        self.assertEqual(decision.allowed_credit, 6.30)

    def test_missing_or_non_credit_combo_nbbo_fails_closed(self) -> None:
        decision = protect_credit_limit(6.00, ComboQuote(bid=None, ask=None))
        self.assertEqual(decision.reason, "combo_nbbo_unavailable")
        self.assertIsNone(decision.allowed_credit)


if __name__ == "__main__":
    unittest.main()
