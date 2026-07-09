"""Unit tests for entry limit pricing and quote guards."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))
sys.path.insert(0, str(ROOT / "live"))

from live_config import LiveConfig  # noqa: E402
from mbh_simulator import CandidateRecord, OptionQuote  # noqa: E402
from entry_execution import (  # noqa: E402
    entry_limit_credit,
    entry_quote_block_reason,
    natural_credit,
    work_deadline,
)


def _cand(short: float, long: float, *, bid: float = 3.0, ask_long: float = 0.2) -> CandidateRecord:
    return CandidateRecord(
        timestamp=datetime.now(),
        side="bear_call",
        status="pass",
        reason="",
        score=2.0,
        expiry="20260706",
        short_type="CALL",
        short_strike=short,
        long_strike=long,
        short_delta=-0.15,
        long_delta=-0.05,
        spot=7500.0,
        distance_pct=0.01,
        width=abs(long - short),
        credit=bid - ask_long,
        credit_to_width=(bid - ask_long) / abs(long - short),
        stop_loss_to_credit=2.0,
        straddle_residual_z=0.0,
        skew_z=0.0,
        term_ratio_z=0.0,
        trend_score=1.0,
        realized_vs_implied_z=0.0,
        short_quote=OptionQuote(datetime.now(), "20260706", "CALL", short, bid - 0.05, bid),
        long_quote=OptionQuote(datetime.now(), "20260706", "CALL", long, ask_long - 0.05, ask_long),
    )


class EntryExecutionTests(unittest.TestCase):
    def test_natural_credit(self) -> None:
        cand = _cand(7545, 7615, bid=3.25, ask_long=0.15)
        self.assertAlmostEqual(natural_credit(cand), 3.05, places=2)

    def test_limit_applies_concession(self) -> None:
        live = LiveConfig(entry_limit_concession=0.05)
        self.assertAlmostEqual(entry_limit_credit(3.20, live), 3.10, places=2)

    def test_ladder_walks_price(self) -> None:
        live = LiveConfig(entry_limit_concession=0.05, entry_ladder_step=0.05)
        self.assertAlmostEqual(entry_limit_credit(3.20, live, ladder_step=2), 3.00, places=2)

    def test_blocks_incomplete_nbbo(self) -> None:
        cand = _cand(7545, 7615)
        cand = CandidateRecord(
            **{**cand.__dict__, "long_quote": OptionQuote(
                datetime.now(), "20260706", "CALL", 7615.0, 0.0, 0.0,
            )}
        )
        self.assertEqual(entry_quote_block_reason(cand, LiveConfig()), "incomplete_nbbo")

    def test_blocks_stale_quote(self) -> None:
        cand = _cand(7545, 7615)
        live = LiveConfig(max_leg_quote_age_seconds=5.0)
        self.assertEqual(
            entry_quote_block_reason(cand, live, leg_ages=[10.0, 1.0]),
            "stale_quote",
        )

    def test_work_deadline_uses_config_seconds(self) -> None:
        live = LiveConfig(entry_work_seconds=600.0)
        start = datetime(2026, 7, 6, 13, 32, 0)
        end = work_deadline(start, live, 15)
        self.assertEqual((end - start).total_seconds(), 600.0)


if __name__ == "__main__":
    unittest.main()
