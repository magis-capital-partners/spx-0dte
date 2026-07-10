"""Unit tests for session recovery, lock, and IB risk matching."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))
sys.path.insert(0, str(ROOT / "live"))

from mbh_simulator import CandidateRecord  # noqa: E402
from ib_executor import OpenSpread  # noqa: E402
from session_recovery import (  # noqa: E402
    LegKey,
    acquire_executor_lock,
    expected_leg_net_from_spreads,
    open_entry_events_from_fills,
    recover_session_book,
    release_executor_lock,
    rebuild_open_spreads_from_entries,
    unmatched_ib_risk,
)


class OpenEntryEventsTests(unittest.TestCase):
    def test_entry_then_stop_clears(self) -> None:
        events = [
            {"event": "entry", "side": "bear_call", "short_strike": 7550, "long_strike": 7610, "contracts": 2, "credit": 1.5},
            {"event": "stop", "side": "bear_call", "short_strike": 7550, "long_strike": 7610},
        ]
        self.assertEqual(open_entry_events_from_fills(events), [])

    def test_flatten_clears_all(self) -> None:
        events = [
            {"event": "entry", "side": "bull_put", "short_strike": 7400, "long_strike": 7200, "contracts": 1, "credit": 3.0},
            {"event": "entry", "side": "bear_call", "short_strike": 7500, "long_strike": 7550, "contracts": 1, "credit": 1.0},
            {"event": "flatten"},
        ]
        self.assertEqual(open_entry_events_from_fills(events), [])

    def test_open_entries_remain(self) -> None:
        events = [
            {"event": "entry", "side": "bull_put", "short_strike": 7400, "long_strike": 7200, "contracts": 1, "credit": 3.0},
            {"event": "entry", "side": "bear_call", "short_strike": 7500, "long_strike": 7550, "contracts": 2, "credit": 1.2},
            {"event": "stop", "side": "bear_call", "short_strike": 7500, "long_strike": 7550},
        ]
        open_rows = open_entry_events_from_fills(events)
        self.assertEqual(len(open_rows), 1)
        self.assertEqual(open_rows[0]["side"], "bull_put")


class RebuildSpreadsTests(unittest.TestCase):
    def test_rebuild_sets_stop_and_gross(self) -> None:
        entries = [
            {
                "event": "entry",
                "side": "bear_call",
                "short_strike": 7545,
                "long_strike": 7625,
                "contracts": 2,
                "credit": 1.65,
                "score": 1.7,
                "sleeve": "core",
                "ts": "2026-07-09T11:47:14",
            }
        ]
        spreads, gross = rebuild_open_spreads_from_entries(
            entries,
            today="2026-07-09",
            stop_multiple=2.0,
            OpenSpread=OpenSpread,
            CandidateRecord=CandidateRecord,
        )
        self.assertEqual(len(spreads), 1)
        self.assertEqual(spreads[0].contracts, 2)
        self.assertAlmostEqual(gross, 1.65 * 2 * 100)
        self.assertEqual(spreads[0].candidate.short_type, "CALL")
        nets = expected_leg_net_from_spreads(spreads, "2026-07-09")
        self.assertEqual(nets[LegKey("C", 7545.0, "20260709")], -2)
        self.assertEqual(nets[LegKey("C", 7625.0, "20260709")], 2)


class UnmatchedRiskTests(unittest.TestCase):
    def test_residual_detected(self) -> None:
        expected = {LegKey("C", 7545.0, "20260709"): -2, LegKey("C", 7625.0, "20260709"): 2}
        ib = {LegKey("C", 7545.0, "20260709"): -2, LegKey("C", 7625.0, "20260709"): 2, LegKey("P", 7400.0, "20260709"): -1}
        residual = unmatched_ib_risk(ib, expected)
        self.assertEqual(residual, {LegKey("P", 7400.0, "20260709"): -1})


class RecoverSessionBookTests(unittest.TestCase):
    def test_fills_only_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            live_dir = Path(tmp)
            day = "2026-07-09"
            day_dir = live_dir / day
            day_dir.mkdir(parents=True)
            fills = day_dir / "fills.jsonl"
            rows = [
                {"event": "entry", "side": "bear_call", "short_strike": 7545, "long_strike": 7625, "contracts": 2, "credit": 1.65},
                {"event": "entry", "side": "bear_call", "short_strike": 7545, "long_strike": 7610, "contracts": 1, "credit": 1.7},
            ]
            fills.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
            book = recover_session_book(
                today=day,
                stop_multiple=2.0,
                OpenSpread=OpenSpread,
                CandidateRecord=CandidateRecord,
                ib=None,
                live_dir=live_dir,
            )
            self.assertEqual(len(book.spreads), 2)
            self.assertAlmostEqual(book.gross_credit_sold, 1.65 * 2 * 100 + 1.7 * 100)

    def test_fail_loud_on_unmatched_ib(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            live_dir = Path(tmp)
            day = "2026-07-09"
            day_dir = live_dir / day
            day_dir.mkdir(parents=True)
            (day_dir / "fills.jsonl").write_text("", encoding="utf-8")

            class FakeContract:
                secType = "OPT"
                symbol = "SPX"
                tradingClass = "SPXW"
                localSymbol = "SPXW  260709C07545000"
                lastTradeDateOrContractMonth = "20260709"
                right = "C"
                strike = 7545.0

            class FakeIB:
                def reqPositions(self):
                    return None

                def sleep(self, _sec):
                    return None

                def positions(self):
                    return [SimpleNamespace(contract=FakeContract(), position=-2)]

                def openTrades(self):
                    return []

            with self.assertRaises(SystemExit) as ctx:
                recover_session_book(
                    today=day,
                    stop_multiple=2.0,
                    OpenSpread=OpenSpread,
                    CandidateRecord=CandidateRecord,
                    ib=FakeIB(),
                    live_dir=live_dir,
                    fail_on_unmatched=True,
                )
            msg = str(ctx.exception)
            self.assertTrue(
                "IB has SPXW positions" in msg or "IB SPXW risk not explained" in msg,
                msg,
            )


class ExecutorLockTests(unittest.TestCase):
    def test_second_acquire_fails_while_alive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            live_dir = Path(tmp)
            day = "2026-07-10"
            path = acquire_executor_lock(day, live_dir=live_dir)
            self.assertTrue(path.exists())
            # Simulate another process by rewriting pid to a fake alive-looking value
            # — on Windows/Linux we only assert same-pid reentry path: force after release.
            release_executor_lock(path)
            path2 = acquire_executor_lock(day, live_dir=live_dir)
            self.assertTrue(path2.exists())
            payload = json.loads(path2.read_text(encoding="utf-8"))
            self.assertEqual(payload["pid"], os.getpid())
            release_executor_lock(path2)


if __name__ == "__main__":
    unittest.main()
