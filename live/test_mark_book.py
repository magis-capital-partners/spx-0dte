"""Tests for mark-to-market quality (never treat missing quotes as $0 PnL)."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))
sys.path.insert(0, str(ROOT / "live"))

from mbh_simulator import CandidateRecord, OptionQuote  # noqa: E402
from ib_executor import OpenSpread, _mark_book  # noqa: E402


def _quote(option_type: str, strike: float, bid: float, ask: float) -> OptionQuote:
    return OptionQuote(
        timestamp=datetime(2026, 7, 18, 10, 0),
        expiry="20260718",
        option_type=option_type,
        strike=strike,
        bid=bid,
        ask=ask,
    )


def _spread(short: float = 7500, long: float = 7550, contracts: int = 1) -> OpenSpread:
    cand = CandidateRecord(
        timestamp=datetime(2026, 7, 18, 10, 0),
        side="bear_call",
        status="open",
        reason="test",
        score=1.0,
        expiry="20260718",
        short_type="CALL",
        short_strike=short,
        long_strike=long,
        short_delta=None,
        long_delta=None,
        spot=7480.0,
        distance_pct=0.0,
        width=long - short,
        credit=1.5,
        credit_to_width=1.5 / (long - short),
        stop_loss_to_credit=3.0,
        straddle_residual_z=0.0,
        skew_z=0.0,
        term_ratio_z=0.0,
        trend_score=0.0,
        realized_vs_implied_z=0.0,
        contracts=contracts,
        sleeve="core",
    )
    return OpenSpread(
        candidate=cand,
        contracts=contracts,
        short_entry_sell=1.8,
        long_entry_buy=0.3,
        stop_price=5.4,
    )


class MarkBookTests(unittest.TestCase):
    def test_empty_quotes_unavailable(self) -> None:
        cfg = SimpleNamespace(multiplier=100.0)
        mark = _mark_book([_spread()], [], cfg)  # type: ignore[arg-type]
        self.assertEqual(mark.quality, "unavailable")
        self.assertEqual(mark.pnl, 0.0)
        self.assertEqual(mark.missing_count, 1)

    def test_ok_mark(self) -> None:
        cfg = SimpleNamespace(multiplier=100.0)
        quotes = [
            _quote("CALL", 7500, 1.0, 1.1),
            _quote("CALL", 7550, 0.2, 0.3),
        ]
        # PnL per = 1.8 - 1.1 - 0.3 + 0.2 = 0.6 → $60
        mark = _mark_book([_spread()], quotes, cfg)  # type: ignore[arg-type]
        self.assertEqual(mark.quality, "ok")
        self.assertAlmostEqual(mark.pnl, 60.0)

    def test_partial_mark(self) -> None:
        cfg = SimpleNamespace(multiplier=100.0)
        s1 = _spread(7500, 7550)
        s2 = _spread(7600, 7650)
        quotes = [
            _quote("CALL", 7500, 1.0, 1.1),
            _quote("CALL", 7550, 0.2, 0.3),
        ]
        mark = _mark_book([s1, s2], quotes, cfg)  # type: ignore[arg-type]
        self.assertEqual(mark.quality, "partial")
        self.assertEqual(mark.marked_count, 1)
        self.assertEqual(mark.missing_count, 1)

    def test_no_open_is_ok(self) -> None:
        cfg = SimpleNamespace(multiplier=100.0)
        closed = _spread()
        closed.closed = True
        mark = _mark_book([closed], [], cfg)  # type: ignore[arg-type]
        self.assertEqual(mark.quality, "ok")
        self.assertEqual(mark.open_count, 0)


if __name__ == "__main__":
    unittest.main()
