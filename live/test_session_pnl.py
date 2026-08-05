import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from session_pnl import session_pnl_summary


def _entry(side="bear_call", short=7835.0, long_=7910.0, credit=5.85, contracts=1):
    return {
        "event": "entry",
        "side": side,
        "short_strike": short,
        "long_strike": long_,
        "credit": credit,
        "contracts": contracts,
    }


def _flatten_fill(side="bear_call", short=7835.0, long_=7910.0, price=-1.70, contracts=1):
    return {
        "event": "flatten_fill",
        "side": side,
        "short_strike": short,
        "long_strike": long_,
        "fill_price": price,
        "contracts": contracts,
    }


def _stop(side="bear_call", short=7835.0, long_=7910.0, stop_fill=14.45, contracts=1):
    return {
        "event": "stop",
        "side": side,
        "short_strike": short,
        "long_strike": long_,
        "stop_fill": stop_fill,
        "contracts": contracts,
    }


class ClosedPnlTests(unittest.TestCase):
    def test_flattened_spread_realizes_credit_minus_debit(self) -> None:
        pnl = session_pnl_summary([_entry(credit=5.85), _flatten_fill(price=-1.70)])
        self.assertAlmostEqual(pnl.closed_pnl, (5.85 - 1.70) * 100)
        self.assertEqual(pnl.open_contracts, 0)
        self.assertEqual(pnl.contracts_traded, 1)

    def test_open_spread_is_excluded_so_marked_pnl_is_not_double_counted(self) -> None:
        pnl = session_pnl_summary([_entry()])
        self.assertEqual(pnl.closed_pnl, 0.0)
        self.assertEqual(pnl.open_contracts, 1)

    def test_stopped_spread_stays_open_because_the_long_wing_is_retained(self) -> None:
        pnl = session_pnl_summary([_entry(), _stop()])
        self.assertEqual(pnl.closed_pnl, 0.0)
        self.assertEqual(pnl.open_contracts, 1)
        self.assertEqual(pnl.stopped_contracts, 1)

    def test_stopped_then_flattened_nets_cover_cost_and_wing_sale(self) -> None:
        # Sold at 5.85, bought the short back at 4.00, sold the wing for 0.50.
        pnl = session_pnl_summary([
            _entry(credit=5.85),
            _stop(stop_fill=4.00),
            _flatten_fill(price=0.50),
        ])
        self.assertAlmostEqual(pnl.closed_pnl, (5.85 - 4.00 + 0.50) * 100)
        self.assertEqual(pnl.open_contracts, 0)

    def test_multi_contract_and_partial_flatten(self) -> None:
        pnl = session_pnl_summary([
            _entry(credit=5.00, contracts=3),
            _flatten_fill(price=-2.00, contracts=2),
        ])
        self.assertAlmostEqual(pnl.closed_pnl, (5.00 - 2.00) * 2 * 100)
        self.assertEqual(pnl.open_contracts, 1)
        self.assertEqual(pnl.contracts_traded, 3)

    def test_fifo_matches_each_flatten_to_its_own_entry_credit(self) -> None:
        pnl = session_pnl_summary([
            _entry(credit=5.85),
            _entry(credit=4.70),
            _flatten_fill(price=-1.70),
            _flatten_fill(price=-1.65),
        ])
        expected = ((5.85 - 1.70) + (4.70 - 1.65)) * 100
        self.assertAlmostEqual(pnl.closed_pnl, expected)
        self.assertEqual(pnl.open_contracts, 0)

    def test_distinct_spreads_do_not_cross_match(self) -> None:
        pnl = session_pnl_summary([
            _entry(side="bear_call", short=7835.0, long_=7910.0, credit=5.85),
            _entry(side="bull_put", short=7600.0, long_=7450.0, credit=3.00),
            _flatten_fill(side="bull_put", short=7600.0, long_=7450.0, price=-1.00),
        ])
        self.assertAlmostEqual(pnl.closed_pnl, (3.00 - 1.00) * 100)
        self.assertEqual(pnl.open_contracts, 1)

    def test_todays_real_session_shape(self) -> None:
        events = [_entry(credit=c) for c in (5.85, 4.70, 5.25, 4.35)]
        events += [_flatten_fill(price=p) for p in (-1.70, -1.65, -2.50, -2.50)]
        pnl = session_pnl_summary(events)
        self.assertAlmostEqual(pnl.closed_pnl, 1180.0)
        self.assertAlmostEqual(pnl.credit_received, 2015.0)
        self.assertEqual(pnl.contracts_traded, 4)
        self.assertEqual(pnl.entry_count, 4)
        self.assertEqual(pnl.open_contracts, 0)

    def test_malformed_rows_are_skipped(self) -> None:
        pnl = session_pnl_summary([
            {"event": "entry", "contracts": "bad", "credit": 1.0},
            {"event": "entry"},
            _entry(credit=5.00),
            {"event": "flatten_fill", "fill_price": None, "contracts": None},
        ])
        self.assertEqual(pnl.entry_count, 1)
        self.assertEqual(pnl.contracts_traded, 1)
        self.assertEqual(pnl.closed_pnl, 0.0)


if __name__ == "__main__":
    unittest.main()
