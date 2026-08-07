"""Tests for sanitized live status builders."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "live"))

import session_status_server as sss  # noqa: E402


class LiveStatusTests(unittest.TestCase):
    def test_rollover_loop_requests_restart_after_date_change(self) -> None:
        stopped = threading.Event()
        shutdown_called = threading.Event()

        sss._status_rollover_loop(
            "2026-07-30",
            stopped,
            shutdown_called.set,
            poll_seconds=0,
            today_fn=lambda: "2026-07-31",
        )

        self.assertTrue(shutdown_called.is_set())

    def test_sanitized_omits_stdout_and_strikes(self) -> None:
        full = {
            "schema": 1,
            "source": "local",
            "generated_at": "2026-07-29T15:00:00",
            "date": "2026-07-29",
            "pid": 123,
            "pid_alive": True,
            "heartbeat_ts": "2026-07-29T15:00:00",
            "entries_halted": False,
            "flattened": False,
            "open_count": 1,
            "marked_pnl": -12.5,
            "closed_pnl": 400.0,
            "total_pnl": 387.5,
            "credit_received": 1000.0,
            "contracts_traded": 3,
            "entry_count": 2,
            "open_contracts": 1,
            "recent_events": [
                {
                    "ts": "2026-07-29T14:00:00",
                    "event": "entry",
                    "side": "bull_put",
                    "short_strike": 7360.0,
                }
            ],
            "stdout_path": "x",
        }
        with mock.patch.object(sss, "build_status", return_value=full):
            cloud = sss.build_sanitized_cloud_status(today="2026-07-29")
        self.assertEqual(cloud["source"], "cloud")
        self.assertNotIn("stdout_path", cloud)
        self.assertNotIn("recent_events", cloud)
        self.assertEqual(cloud["last_event"]["event"], "entry")
        self.assertNotIn("short_strike", cloud["last_event"])
        self.assertEqual(cloud["open_count"], 1)
        # Realized P&L must survive sanitization, otherwise a flattened session
        # publishes marked_pnl=0.0 and the day's result disappears.
        self.assertEqual(cloud["closed_pnl"], 400.0)
        self.assertEqual(cloud["total_pnl"], 387.5)
        self.assertEqual(cloud["contracts_traded"], 3)

    def test_config_summary_curates_and_excludes_connection_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            live_dir = Path(tmp)
            day_dir = live_dir / "2026-08-07"
            day_dir.mkdir()
            (day_dir / "config.json").write_text(json.dumps({
                "git_commit": "abc123",
                "live_config": {
                    "profile": "p3_poststop_cooldown_120",
                    "mode": "live",
                    "contracts_per_tranche": 4,
                    "max_contracts_per_tranche": 5,
                    "host": "127.0.0.1",
                    "port": 7496,
                    "ib_account": "U805366",
                    "client_id": 17,
                },
                "strategy_config": {
                    "put_wing_width": 150.0,
                    "call_wing_width": 75.0,
                    "daily_loss_limit_pct": 0.0225,
                },
            }), encoding="utf-8")
            with mock.patch.object(sss, "LIVE_DIR", live_dir):
                summary = sss._config_summary("2026-08-07")
        self.assertEqual(summary["profile"], "p3_poststop_cooldown_120")
        self.assertEqual(summary["contracts_per_tranche"], 4)
        self.assertEqual(summary["put_wing_width"], 150.0)
        self.assertEqual(summary["git_commit"], "abc123")
        self.assertNotIn("host", summary)
        self.assertNotIn("ib_account", summary)
        self.assertNotIn("client_id", summary)

    def test_config_summary_missing_file_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(sss, "LIVE_DIR", Path(tmp)):
                self.assertIsNone(sss._config_summary("2026-08-07"))

    def test_write_cloud_status_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "live_status.json"
            with mock.patch.object(
                sss,
                "build_sanitized_cloud_status",
                return_value={
                    "schema": 1,
                    "source": "cloud",
                    "generated_at": "t",
                    "date": "2026-07-29",
                    "pid_alive": False,
                    "heartbeat_ts": None,
                    "entries_halted": False,
                    "flattened": False,
                    "open_count": 0,
                    "marked_pnl": 0.0,
                    "last_event": None,
                    "note": "x",
                },
            ), mock.patch.object(sss, "LIVE_DIR", Path(tmp)):
                path = sss.write_cloud_status(out_path=out)
            self.assertTrue(path.is_file())
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["source"], "cloud")


if __name__ == "__main__":
    unittest.main()
