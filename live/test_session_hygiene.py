"""Unit tests for pre-session state hygiene and IB account isolation."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))
sys.path.insert(0, str(ROOT / "live"))

import ib_executor  # noqa: E402
from session_hygiene import (  # noqa: E402
    EXIT_BLOCKED,
    EXIT_OK,
    EXIT_WARN,
    prune_stale_kill_files,
    run_check_started,
    run_pre_session,
    session_started,
)
from session_hygiene import HygieneReport  # noqa: E402


class _Tmp(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.live = Path(self._dir.name)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _day(self, day: str) -> Path:
        path = self.live / day
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _write_fills(self, day: str, events: list) -> None:
        path = self._day(day) / "fills.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event) + "\n")


class KillFileTests(_Tmp):
    def test_clean_state_is_ok(self) -> None:
        self._day("2026-08-06")
        report = run_pre_session("2026-08-06", live_dir=self.live)
        self.assertEqual(report.exit_code, EXIT_OK)
        self.assertEqual(report.blockers, [])

    def test_global_kill_blocks(self) -> None:
        (self.live / "KILL").write_text("stop everything", encoding="utf-8")
        report = run_pre_session("2026-08-06", live_dir=self.live)
        self.assertEqual(report.exit_code, EXIT_BLOCKED)
        self.assertIn("global KILL", report.blockers[0])
        # Never removed: it may be a deliberate stop-everything switch.
        self.assertTrue((self.live / "KILL").is_file())

    def test_todays_kill_blocks_and_survives(self) -> None:
        self._day("2026-08-06").joinpath("KILL").write_text("wd", encoding="utf-8")
        report = run_pre_session("2026-08-06", live_dir=self.live)
        self.assertEqual(report.exit_code, EXIT_BLOCKED)
        self.assertTrue((self.live / "2026-08-06" / "KILL").is_file())

    def test_leftover_clear_file_warns_but_does_not_block(self) -> None:
        self._day("2026-08-06").joinpath("CLEAR_STALE_HALT").write_text("", encoding="utf-8")
        report = run_pre_session("2026-08-06", live_dir=self.live)
        self.assertEqual(report.exit_code, EXIT_WARN)
        self.assertIn("CLEAR_STALE_HALT", report.warnings[0])
        # Reported, not tidied away — releasing a halt stays an explicit act.
        self.assertTrue((self.live / "2026-08-06" / "CLEAR_STALE_HALT").is_file())


class PruneTests(_Tmp):
    def test_past_kill_files_are_pruned(self) -> None:
        for day in ("2026-07-30", "2026-08-03", "2026-08-05"):
            self._day(day).joinpath("KILL").write_text("x", encoding="utf-8")
        self._day("2026-08-06")
        report = run_pre_session("2026-08-06", live_dir=self.live)
        self.assertEqual(len(report.pruned), 3)
        for day in ("2026-07-30", "2026-08-03", "2026-08-05"):
            self.assertFalse((self.live / day / "KILL").exists())

    def test_prune_never_touches_today_or_future(self) -> None:
        self._day("2026-08-06").joinpath("KILL").write_text("x", encoding="utf-8")
        self._day("2026-08-07").joinpath("KILL").write_text("x", encoding="utf-8")
        report = HygieneReport()
        prune_stale_kill_files("2026-08-06", live_dir=self.live, report=report)
        self.assertEqual(report.pruned, [])
        self.assertTrue((self.live / "2026-08-06" / "KILL").is_file())
        self.assertTrue((self.live / "2026-08-07" / "KILL").is_file())

    def test_keep_days_retains_recent(self) -> None:
        self._day("2026-08-05").joinpath("KILL").write_text("x", encoding="utf-8")
        self._day("2026-07-30").joinpath("KILL").write_text("x", encoding="utf-8")
        report = HygieneReport()
        prune_stale_kill_files(
            "2026-08-06", live_dir=self.live, keep_days=3, report=report
        )
        self.assertTrue((self.live / "2026-08-05" / "KILL").is_file())
        self.assertFalse((self.live / "2026-07-30" / "KILL").exists())

    def test_dry_run_deletes_nothing(self) -> None:
        self._day("2026-07-30").joinpath("KILL").write_text("x", encoding="utf-8")
        report = HygieneReport()
        prune_stale_kill_files(
            "2026-08-06", live_dir=self.live, report=report, dry_run=True
        )
        self.assertTrue((self.live / "2026-07-30" / "KILL").is_file())
        self.assertIn("would remove", report.pruned[0])


class SessionStartedTests(_Tmp):
    def test_detects_session_start(self) -> None:
        self._write_fills("2026-08-06", [{"event": "session_start", "mode": "live"}])
        self.assertTrue(session_started("2026-08-06", live_dir=self.live))

    def test_missing_fills_is_not_started(self) -> None:
        self._day("2026-08-06")
        self.assertFalse(session_started("2026-08-06", live_dir=self.live))

    def test_other_events_do_not_count(self) -> None:
        self._write_fills("2026-08-06", [{"event": "halt_entries", "reason": "x"}])
        self.assertFalse(session_started("2026-08-06", live_dir=self.live))

    def test_corrupt_line_is_skipped(self) -> None:
        path = self._day("2026-08-06") / "fills.jsonl"
        path.write_text('not json\n{"event": "session_start"}\n', encoding="utf-8")
        self.assertTrue(session_started("2026-08-06", live_dir=self.live))

    def test_check_started_blocks_on_a_trading_day(self) -> None:
        self._day("2026-08-06")  # a Thursday
        report = run_check_started("2026-08-06", live_dir=self.live)
        self.assertEqual(report.exit_code, EXIT_BLOCKED)
        self.assertIn("no session_start", report.blockers[0])

    def test_check_started_silent_on_a_weekend(self) -> None:
        # 2026-08-08 is a Saturday: no session is expected, so no alert.
        self._day("2026-08-08")
        report = run_check_started("2026-08-08", live_dir=self.live)
        self.assertEqual(report.exit_code, EXIT_OK)
        self.assertEqual(report.blockers, [])


class PositionsUnavailableTests(unittest.TestCase):
    """IB 10275 must mark an account's position read as unverified."""

    def setUp(self) -> None:
        ib_executor._POSITIONS_UNAVAILABLE_ACCOUNTS.clear()

    def tearDown(self) -> None:
        ib_executor._POSITIONS_UNAVAILABLE_ACCOUNTS.clear()

    def test_parses_account_from_real_error_string(self) -> None:
        ib_executor._note_positions_unavailable(
            "Positions info is not available for account(s): U27250667 until the "
            "application is finished and approved."
        )
        self.assertTrue(ib_executor.positions_unavailable_for("U27250667"))

    def test_unrelated_account_stays_verifiable(self) -> None:
        ib_executor._note_positions_unavailable(
            "Positions info is not available for account(s): U27250667 until the "
            "application is finished and approved."
        )
        # The traded account is a different one, so a flat read is still trusted.
        self.assertFalse(ib_executor.positions_unavailable_for("U805366"))

    def test_multiple_accounts_are_captured(self) -> None:
        ib_executor._note_positions_unavailable(
            "Positions info is not available for account(s): U111, U222 until approved."
        )
        self.assertTrue(ib_executor.positions_unavailable_for("U111"))
        self.assertTrue(ib_executor.positions_unavailable_for("U222"))

    def test_unrelated_error_text_is_ignored(self) -> None:
        ib_executor._note_positions_unavailable("HMDS data farm connection is inactive")
        self.assertEqual(ib_executor._POSITIONS_UNAVAILABLE_ACCOUNTS, set())

    def test_empty_account_is_never_flagged(self) -> None:
        self.assertFalse(ib_executor.positions_unavailable_for(None))
        self.assertFalse(ib_executor.positions_unavailable_for(""))

    def test_10275_is_quieted_to_file_only(self) -> None:
        self.assertIn(10275, ib_executor._QUIET_IB_ERROR_CODES)


if __name__ == "__main__":
    unittest.main()
