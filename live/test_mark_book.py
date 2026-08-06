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
from live_config import LiveConfig  # noqa: E402


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
    def test_mark_outage_flatten_delay_is_five_minutes(self) -> None:
        self.assertEqual(LiveConfig().mark_unavailable_flatten_seconds, 300.0)

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


class ZeroBidShortAskTests(unittest.TestCase):
    """A large ask with no bid is a stale print, not a price.

    Regression cover for 2026-08-05: a 16:00 chain re-subscription left the
    7720 short put (3.55 points OTM, settling worthless) quoted with no bid and
    an ask near 60. The mark went from +$4,130 to -$7,890.
    """

    def test_zero_bid_large_ask_is_unmarkable(self) -> None:
        cfg = SimpleNamespace(multiplier=100.0)
        quotes = [
            _quote("CALL", 7500, 0.0, 60.0),  # no bid, absurd ask
            _quote("CALL", 7550, 0.2, 0.3),
        ]
        mark = _mark_book(
            [_spread()], quotes, cfg, max_ask_without_bid=5.0,  # type: ignore[arg-type]
        )
        self.assertEqual(mark.quality, "unavailable")
        self.assertEqual(mark.missing_count, 1)

    def test_zero_bid_small_ask_still_marks(self) -> None:
        # A far-OTM expiring option legitimately has no bid and a tiny ask;
        # that must still mark, or every expiry would degrade to "unavailable".
        cfg = SimpleNamespace(multiplier=100.0)
        quotes = [
            _quote("CALL", 7500, 0.0, 0.05),
            _quote("CALL", 7550, 0.0, 0.05),
        ]
        mark = _mark_book(
            [_spread()], quotes, cfg, max_ask_without_bid=5.0,  # type: ignore[arg-type]
        )
        self.assertEqual(mark.quality, "ok")
        # 1.8 - 0.05 - 0.3 + 0.0 = 1.45 -> $145
        self.assertAlmostEqual(mark.pnl, 145.0)

    def test_two_sided_large_ask_is_trusted(self) -> None:
        # Genuinely deep ITM: bid and ask both large. That is a real loss and
        # must not be suppressed, or the halt/flatten governors would go blind.
        cfg = SimpleNamespace(multiplier=100.0)
        quotes = [
            _quote("CALL", 7500, 58.0, 60.0),
            _quote("CALL", 7550, 8.0, 9.0),
        ]
        mark = _mark_book(
            [_spread()], quotes, cfg, max_ask_without_bid=5.0,  # type: ignore[arg-type]
        )
        self.assertEqual(mark.quality, "ok")
        # 1.8 - 60.0 - 0.3 + 8.0 = -50.5 -> -$5,050
        self.assertAlmostEqual(mark.pnl, -5050.0)

    def test_guard_disabled_reproduces_the_bug(self) -> None:
        cfg = SimpleNamespace(multiplier=100.0)
        quotes = [
            _quote("CALL", 7500, 0.0, 60.0),
            _quote("CALL", 7550, 0.2, 0.3),
        ]
        mark = _mark_book(
            [_spread()], quotes, cfg, max_ask_without_bid=0.0,  # type: ignore[arg-type]
        )
        self.assertEqual(mark.quality, "ok")
        self.assertAlmostEqual(mark.pnl, -5830.0)  # phantom loss

    def test_default_guard_is_enabled(self) -> None:
        self.assertGreater(LiveConfig().mark_max_short_ask_without_bid, 0.0)

    def test_reproduces_20260805_session_end(self) -> None:
        """The five spreads open at 16:00 on 2026-08-05, with the bad put quote.

        Unguarded this yields the recorded -$7,890; guarded, the put becomes
        unmarkable so the session-end gate keeps the last good +$4,130 instead.
        """
        cfg = SimpleNamespace(multiplier=100.0)
        book = []
        for short, long_, n, sell, buy, right, side in [
            (7790, 7865, 2, 5.4, 0.4, "CALL", "bear_call"),
            (7780, 7855, 2, 4.7, 0.25, "CALL", "bear_call"),
            (7720, 7570, 2, 6.5, 0.2, "PUT", "bull_put"),
            (7755, 7830, 2, 4.2, 0.15, "CALL", "bear_call"),
            (7760, 7835, 1, 2.0, 0.15, "CALL", "bear_call"),
        ]:
            s = _spread(short, long_, n)
            s.candidate.short_type = right
            s.candidate.side = side
            s.short_entry_sell = sell
            s.long_entry_buy = buy
            book.append(s)

        quotes = [
            _quote("CALL", 7790, 0.0, 0.05), _quote("CALL", 7865, 0.0, 0.05),
            _quote("CALL", 7780, 0.0, 0.05), _quote("CALL", 7855, 0.0, 0.05),
            _quote("PUT", 7720, 0.0, 60.17), _quote("PUT", 7570, 0.0, 0.05),
            _quote("CALL", 7755, 0.0, 0.05), _quote("CALL", 7830, 0.0, 0.05),
            _quote("CALL", 7760, 0.0, 0.05), _quote("CALL", 7835, 0.0, 0.05),
        ]

        unguarded = _mark_book(book, quotes, cfg, max_ask_without_bid=0.0)  # type: ignore[arg-type]
        self.assertEqual(unguarded.quality, "ok")
        self.assertLess(unguarded.pnl, -7000.0)  # the phantom -$7,890

        guarded = _mark_book(book, quotes, cfg, max_ask_without_bid=5.0)  # type: ignore[arg-type]
        self.assertEqual(guarded.quality, "partial")
        self.assertEqual(guarded.missing_count, 1)
        self.assertGreater(guarded.pnl, 0.0)  # the four sound bear_calls only


if __name__ == "__main__":
    unittest.main()
