"""Unit tests for IB short-leg order hygiene (error 201 prevention)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))
sys.path.insert(0, str(ROOT / "live"))

from datetime import datetime

from mbh_simulator import CandidateRecord, OptionQuote  # noqa: E402
from ib_executor import (  # noqa: E402
    OpenSpread,
    _contract_is_short_leg,
    _same_short_leg,
    _short_leg_contract_key,
)


def _cand(side: str, short: float, long: float) -> CandidateRecord:
    short_type = "PUT" if side == "bull_put" else "CALL"
    return CandidateRecord(
        timestamp=__import__("datetime").datetime.now(),
        side=side,
        status="pass",
        reason="",
        score=2.0,
        expiry="20260706",
        short_type=short_type,
        short_strike=short,
        long_strike=long,
        short_delta=-0.15,
        long_delta=-0.05,
        spot=7500.0,
        distance_pct=0.01,
        width=abs(long - short),
        credit=0.5,
        credit_to_width=0.5 / abs(long - short),
        stop_loss_to_credit=2.0,
        straddle_residual_z=0.0,
        skew_z=0.0,
        term_ratio_z=0.0,
        trend_score=1.0,
        realized_vs_implied_z=0.0,
        short_quote=OptionQuote(datetime.now(), "20260706", short_type, short, 0.4, 0.5),
        long_quote=OptionQuote(datetime.now(), "20260706", short_type, long, 0.1, 0.15),
    )


class SameShortLegTests(unittest.TestCase):
    def test_matches_type_and_strike(self) -> None:
        a = _cand("bear_call", 7550, 7610)
        b = _cand("bear_call", 7550, 7620)
        self.assertTrue(_same_short_leg(a, b))

    def test_differs_on_strike(self) -> None:
        a = _cand("bear_call", 7550, 7610)
        b = _cand("bear_call", 7560, 7620)
        self.assertFalse(_same_short_leg(a, b))

    def test_differs_on_type(self) -> None:
        a = _cand("bull_put", 7550, 7500)
        b = _cand("bear_call", 7550, 7610)
        self.assertFalse(_same_short_leg(a, b))


class OpenSpreadDefaultsTests(unittest.TestCase):
    def test_no_backstop_by_default(self) -> None:
        spread = OpenSpread(
            candidate=_cand("bear_call", 7550, 7610),
            contracts=2,
            short_entry_sell=0.8,
            long_entry_buy=0.1,
            stop_price=1.6,
        )
        self.assertIsNone(spread.stop_order_id)


class ContractMatchTests(unittest.TestCase):
    def test_short_leg_key(self) -> None:
        cand = _cand("bear_call", 7550, 7610)
        self.assertEqual(
            _short_leg_contract_key(cand, "2026-07-06"),
            ("SPX", "20260706", 7550.0, "C"),
        )

    def test_contract_is_short_leg(self) -> None:
        cand = _cand("bear_call", 7550, 7610)

        class _Contract:
            secType = "OPT"
            symbol = "SPX"
            lastTradeDateOrContractMonth = "20260706"
            strike = 7550.0
            right = "C"

        self.assertTrue(_contract_is_short_leg(_Contract(), cand, "2026-07-06"))
        _Contract.strike = 7560.0
        self.assertFalse(_contract_is_short_leg(_Contract(), cand, "2026-07-06"))


if __name__ == "__main__":
    unittest.main()
