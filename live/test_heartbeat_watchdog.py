"""Tests for heartbeat + local watchdog evaluation."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "live"))

from heartbeat import (  # noqa: E402
    heartbeat_age_seconds,
    read_heartbeat,
    write_heartbeat,
)
from watchdog import evaluate_watchdog  # noqa: E402


class HeartbeatWatchdogTests(unittest.TestCase):
    def test_write_read_age(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            live_dir = Path(tmp)
            write_heartbeat(
                "2026-07-18",
                open_count=2,
                marked_pnl=-100.0,
                live_dir=live_dir,
            )
            hb = read_heartbeat("2026-07-18", live_dir=live_dir)
            assert hb is not None
            self.assertEqual(hb["open_count"], 2)
            age = heartbeat_age_seconds(hb, now=datetime.now())
            self.assertIsNotNone(age)
            self.assertLess(age, 5.0)

    def test_stale_heartbeat_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            live_dir = Path(tmp)
            day = "2026-07-18"
            day_dir = live_dir / day
            day_dir.mkdir(parents=True)
            old = (datetime.now() - timedelta(seconds=60)).isoformat()
            (day_dir / "heartbeat.json").write_text(
                json.dumps({"ts": old, "pid": os.getpid(), "open_count": 2}),
                encoding="utf-8",
            )
            (day_dir / "executor.lock").write_text(
                json.dumps({"pid": os.getpid(), "date": day}),
                encoding="utf-8",
            )
            reason = evaluate_watchdog(day, max_heartbeat_age=30.0, live_dir=live_dir)
            self.assertIsNotNone(reason)
            self.assertIn("heartbeat_stale", reason or "")

    def test_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            live_dir = Path(tmp)
            day = "2026-07-18"
            write_heartbeat(day, open_count=1, marked_pnl=0.0, live_dir=live_dir)
            day_dir = live_dir / day
            (day_dir / "executor.lock").write_text(
                json.dumps({"pid": os.getpid(), "date": day}),
                encoding="utf-8",
            )
            reason = evaluate_watchdog(day, max_heartbeat_age=30.0, live_dir=live_dir)
            self.assertIsNone(reason)


if __name__ == "__main__":
    unittest.main()
