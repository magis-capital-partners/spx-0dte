"""Unit tests for IB short-leg order hygiene (error 201 prevention)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))
sys.path.insert(0, str(ROOT / "live"))

from datetime import datetime
from types import SimpleNamespace

import ib_executor  # noqa: E402
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


class ShortLegCancelIsolationTests(unittest.TestCase):
    """Cancelling backstops must not reach other accounts or strategies.

    IB error 10275 leaves the spare account's positions unreadable, so we cannot
    observe whether it holds same-day SPXW risk on a strike this engine trades.
    openTrades() spans every account in the gateway login, and a contract match
    alone does not establish ownership.
    """

    def _trade(self, *, account: str, order_ref: str = "", order_id: int = 1):
        contract = SimpleNamespace(
            secType="OPT", symbol="SPX", localSymbol="SPXW  260806C07760000",
            tradingClass="SPXW", lastTradeDateOrContractMonth="20260806",
            strike=7760.0, right="C",
        )
        order = SimpleNamespace(orderId=order_id, account=account, orderRef=order_ref)
        return SimpleNamespace(
            contract=contract, order=order,
            orderStatus=SimpleNamespace(status="Submitted"),
        )

    def _candidate(self):
        return SimpleNamespace(short_type="CALL", short_strike=7760.0)

    def _ib(self, trades):
        cancelled = []
        return SimpleNamespace(
            openTrades=lambda: list(trades),
            cancelOrder=lambda o: cancelled.append(o),
            sleep=lambda s: None,
        ), cancelled

    def test_other_account_order_is_not_cancelled(self) -> None:
        ib, cancelled = self._ib([self._trade(account="U27250667")])
        n = ib_executor._cancel_open_orders_on_short_leg(
            ib, self._candidate(), "2026-08-06", account="U805366",
        )
        self.assertEqual(n, 0)
        self.assertEqual(cancelled, [])

    def test_own_account_order_is_cancelled(self) -> None:
        ib, cancelled = self._ib([self._trade(account="U805366")])
        n = ib_executor._cancel_open_orders_on_short_leg(
            ib, self._candidate(), "2026-08-06", account="U805366",
        )
        self.assertEqual(n, 1)
        self.assertEqual(len(cancelled), 1)

    def test_foreign_strategy_orderref_is_never_cancelled(self) -> None:
        ib, cancelled = self._ib(
            [self._trade(account="U805366", order_ref="B5P|bucket5")]
        )
        n = ib_executor._cancel_open_orders_on_short_leg(
            ib, self._candidate(), "2026-08-06", account="U805366",
        )
        self.assertEqual(n, 0)
        self.assertEqual(cancelled, [])
