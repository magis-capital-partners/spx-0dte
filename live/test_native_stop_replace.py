"""Unit tests for cancel→add→replace native short-leg STP lifecycle."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))
sys.path.insert(0, str(ROOT / "live"))

from mbh_simulator import CandidateRecord, OptionQuote, StrategyConfig  # noqa: E402
from live_config import LiveConfig  # noqa: E402
from entry_execution import PendingEntry  # noqa: E402
from ib_executor import (  # noqa: E402
    OpenSpread,
    active_spreads_on_short,
    aggregated_native_stop_plan,
    apply_pending_resolution,
    clear_short_leg_backstops,
    enforce_native_stop_disarm_budget,
    native_stops_enabled,
    place_or_replace_native_stop_for_short,
)


def _cand(side: str, short: float, long: float, bid: float = 0.80) -> CandidateRecord:
    short_type = "PUT" if side == "bull_put" else "CALL"
    now = datetime.now()
    return CandidateRecord(
        timestamp=now,
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
        short_quote=OptionQuote(now, "20260706", short_type, short, bid, bid + 0.05),
        long_quote=OptionQuote(now, "20260706", short_type, long, 0.10, 0.15),
    )


def _spread(cand: CandidateRecord, contracts: int, short_entry: float, *, stop_id=None) -> OpenSpread:
    return OpenSpread(
        candidate=cand,
        contracts=contracts,
        short_entry_sell=short_entry,
        long_entry_buy=0.10,
        stop_price=short_entry * 3.0,
        stop_order_id=stop_id,
    )


class AggregatedPlanTests(unittest.TestCase):
    def test_default_native_multiple_wider_than_strategy(self) -> None:
        """Production default: synthetic 3×, native 4.5× so STP cannot race."""
        live = LiveConfig()  # native_stop_multiple=4.5
        config = StrategyConfig(stop_multiple=3.0)
        a = _spread(_cand("bear_call", 7550, 7610, bid=1.00), 2, 1.00)
        qty, stop_px = aggregated_native_stop_plan([a], live, config)
        self.assertEqual(qty, 2)
        self.assertAlmostEqual(stop_px, 4.50)  # 1.00 * 4.5
        self.assertGreater(stop_px, a.stop_price)  # wider than synthetic 3.0

    def test_apply_pending_sets_synthetic_stop_not_native(self) -> None:
        live = LiveConfig(use_native_stop_replace=True, native_stop_multiple=4.5)
        config = StrategyConfig(stop_multiple=3.0)
        today = "2026-07-06"
        new_cand = _cand("bear_call", 7550, 7620, bid=0.80)
        new_spread = _spread(new_cand, 1, 0.80)  # stop_price already 2.40
        pending = PendingEntry(
            spread=new_spread,
            trade=MagicMock(),
            candidate=new_cand,
            contracts=1,
            natural_credit=0.55,
            limit_credit=0.50,
            submitted_at=datetime.now(),
            work_until=datetime.now() + timedelta(seconds=30),
            next_ladder_at=datetime.now() + timedelta(seconds=60),
            tranche_time=datetime.now(),
            sleeve="core",
            score=2.0,
        )
        open_spreads: list = []
        apply_pending_resolution(
            {"event": "entry", "credit": 0.50},
            pending,
            open_spreads=open_spreads,
            config=config,
            sleeve_margin_used={"core": 0.0},
            portfolio_margin_used=0.0,
            ib=None,
            today=today,
            dry=True,
            live=live,
        )
        self.assertAlmostEqual(open_spreads[0].stop_price, 2.40)  # 0.80 * 3, not * 4.5

    def test_tightest_stop_and_total_qty(self) -> None:
        live = LiveConfig(use_native_stop_replace=True, native_stop_multiple=None)
        config = StrategyConfig(stop_multiple=3.0)
        a = _spread(_cand("bear_call", 7550, 7610, bid=1.00), 2, 1.00)
        b = _spread(_cand("bear_call", 7550, 7620, bid=0.80), 3, 0.80)
        qty, stop_px = aggregated_native_stop_plan([a, b], live, config)
        self.assertEqual(qty, 5)
        self.assertAlmostEqual(stop_px, 2.40)  # 0.80 * 3

    def test_legacy_backstop_wider(self) -> None:
        live = LiveConfig(use_native_stop_replace=False, use_native_stop_backstop=True)
        config = StrategyConfig(stop_multiple=3.0)
        a = _spread(_cand("bear_call", 7550, 7610), 2, 1.00)  # stop_price=3.0
        qty, stop_px = aggregated_native_stop_plan([a], live, config)
        self.assertEqual(qty, 2)
        self.assertAlmostEqual(stop_px, 4.50)

    def test_enabled_flags(self) -> None:
        self.assertTrue(native_stops_enabled(LiveConfig(use_native_stop_replace=True)))
        self.assertTrue(
            native_stops_enabled(
                LiveConfig(use_native_stop_replace=False, use_native_stop_backstop=True)
            )
        )
        self.assertFalse(
            native_stops_enabled(
                LiveConfig(use_native_stop_replace=False, use_native_stop_backstop=False)
            )
        )


class DryRearmLifecycleTests(unittest.TestCase):
    def test_clear_then_rearm_aggregates(self) -> None:
        live = LiveConfig(use_native_stop_replace=True)
        config = StrategyConfig(stop_multiple=3.0)
        today = "2026-07-06"
        cand = _cand("bear_call", 7550, 7610, bid=1.00)
        spreads = [
            _spread(cand, 2, 1.00, stop_id=111),
            _spread(_cand("bear_call", 7550, 7620, bid=0.80), 1, 0.80, stop_id=111),
        ]
        clear_short_leg_backstops(None, cand, spreads, today, dry=True, reason="pre_entry")
        self.assertTrue(all(s.stop_order_id is None for s in spreads))

        oid = place_or_replace_native_stop_for_short(
            None,
            cand,
            spreads,
            today,
            dry=True,
            live=live,
            config=config,
            reason="post_fill",
        )
        self.assertIsNotNone(oid)
        self.assertTrue(all(s.stop_order_id == oid for s in spreads))

    def test_stopped_spreads_excluded_from_active(self) -> None:
        cand = _cand("bull_put", 7400, 7250)
        open_a = _spread(cand, 2, 0.90)
        stopped = _spread(cand, 2, 0.90)
        stopped.stopped = True
        active = active_spreads_on_short([open_a, stopped], cand)
        self.assertEqual(active, [open_a])

    def test_apply_pending_fill_rearms(self) -> None:
        live = LiveConfig(use_native_stop_replace=True)
        config = StrategyConfig(stop_multiple=3.0)
        today = "2026-07-06"
        prior = _spread(_cand("bear_call", 7550, 7610, bid=1.00), 2, 1.00, stop_id=None)
        open_spreads = [prior]
        new_cand = _cand("bear_call", 7550, 7620, bid=0.80)
        new_spread = _spread(new_cand, 1, 0.80)
        pending = PendingEntry(
            spread=new_spread,
            trade=MagicMock(),
            candidate=new_cand,
            contracts=1,
            natural_credit=0.55,
            limit_credit=0.50,
            submitted_at=datetime.now(),
            work_until=datetime.now() + timedelta(seconds=30),
            next_ladder_at=datetime.now() + timedelta(seconds=60),
            tranche_time=datetime.now(),
            sleeve="core",
            score=2.0,
        )
        filled, credit, margin, port = apply_pending_resolution(
            {"event": "entry", "credit": 0.50},
            pending,
            open_spreads=open_spreads,
            config=config,
            sleeve_margin_used={"core": 0.0},
            portfolio_margin_used=0.0,
            ib=None,
            today=today,
            dry=True,
            live=live,
        )
        self.assertEqual(filled, 1)
        self.assertEqual(len(open_spreads), 2)
        self.assertTrue(all(s.stop_order_id is not None for s in open_spreads))
        self.assertEqual(open_spreads[0].stop_order_id, open_spreads[1].stop_order_id)

    def test_apply_pending_reject_rearms_prior(self) -> None:
        live = LiveConfig(use_native_stop_replace=True)
        config = StrategyConfig(stop_multiple=3.0)
        today = "2026-07-06"
        prior = _spread(_cand("bear_call", 7550, 7610, bid=1.00), 2, 1.00, stop_id=None)
        open_spreads = [prior]
        new_cand = _cand("bear_call", 7550, 7620, bid=0.80)
        pending = PendingEntry(
            spread=_spread(new_cand, 1, 0.80),
            trade=MagicMock(),
            candidate=new_cand,
            contracts=1,
            natural_credit=0.55,
            limit_credit=0.50,
            submitted_at=datetime.now(),
            work_until=datetime.now() + timedelta(seconds=30),
            next_ladder_at=datetime.now() + timedelta(seconds=60),
            tranche_time=datetime.now(),
            sleeve="core",
            score=2.0,
        )
        filled, *_ = apply_pending_resolution(
            {"event": "order_rejected", "reason": "entry_unfilled"},
            pending,
            open_spreads=open_spreads,
            config=config,
            sleeve_margin_used={"core": 0.0},
            portfolio_margin_used=0.0,
            ib=None,
            today=today,
            dry=True,
            live=live,
        )
        self.assertEqual(filled, 0)
        self.assertEqual(len(open_spreads), 1)
        self.assertIsNotNone(prior.stop_order_id)


class DisarmBudgetTests(unittest.TestCase):
    def test_cancels_pending_after_max_disarm(self) -> None:
        live = LiveConfig(
            use_native_stop_replace=True,
            native_stop_disarm_max_seconds=45.0,
        )
        config = StrategyConfig(stop_multiple=3.0)
        today = "2026-07-06"
        prior = _spread(_cand("bear_call", 7550, 7610, bid=1.00), 2, 1.00, stop_id=None)
        open_spreads = [prior]
        cand = _cand("bear_call", 7550, 7620, bid=0.80)
        submitted = datetime(2026, 7, 6, 10, 0, 0)
        pending = PendingEntry(
            spread=_spread(cand, 1, 0.80),
            trade=MagicMock(),
            candidate=cand,
            contracts=1,
            natural_credit=0.55,
            limit_credit=0.50,
            submitted_at=submitted,
            work_until=submitted + timedelta(seconds=870),
            next_ladder_at=submitted + timedelta(seconds=60),
            tranche_time=submitted,
            sleeve="core",
            score=2.0,
        )
        remaining = enforce_native_stop_disarm_budget(
            None,
            pending,
            open_spreads,
            today,
            now=submitted + timedelta(seconds=46),
            dry=True,
            live=live,
            config=config,
        )
        self.assertIsNone(remaining)
        self.assertIsNotNone(prior.stop_order_id)

    def test_keeps_pending_within_budget(self) -> None:
        live = LiveConfig(
            use_native_stop_replace=True,
            native_stop_disarm_max_seconds=45.0,
        )
        config = StrategyConfig(stop_multiple=3.0)
        prior = _spread(_cand("bear_call", 7550, 7610, bid=1.00), 2, 1.00, stop_id=None)
        cand = _cand("bear_call", 7550, 7620, bid=0.80)
        submitted = datetime(2026, 7, 6, 10, 0, 0)
        pending = PendingEntry(
            spread=_spread(cand, 1, 0.80),
            trade=MagicMock(),
            candidate=cand,
            contracts=1,
            natural_credit=0.55,
            limit_credit=0.50,
            submitted_at=submitted,
            work_until=submitted + timedelta(seconds=870),
            next_ladder_at=submitted + timedelta(seconds=60),
            tranche_time=submitted,
            sleeve="core",
            score=2.0,
        )
        remaining = enforce_native_stop_disarm_budget(
            None,
            pending,
            [prior],
            "2026-07-06",
            now=submitted + timedelta(seconds=20),
            dry=True,
            live=live,
            config=config,
        )
        self.assertIs(remaining, pending)


if __name__ == "__main__":
    unittest.main()
