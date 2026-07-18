"""Tests for external KILL switch."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "live"))

from kill_switch import check_kill_switch, kill_paths  # noqa: E402


class KillSwitchTests(unittest.TestCase):
    def test_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            live_dir = Path(tmp)
            self.assertIsNone(check_kill_switch("2026-07-18", live_dir=live_dir))

    def test_global_kill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            live_dir = Path(tmp)
            global_path, _ = kill_paths("2026-07-18", live_dir=live_dir)
            global_path.write_text("stop\n", encoding="utf-8")
            hit = check_kill_switch("2026-07-18", live_dir=live_dir)
            assert hit is not None
            self.assertEqual(hit.scope, "global")
            self.assertEqual(hit.path, global_path)

    def test_session_kill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            live_dir = Path(tmp)
            _, session_path = kill_paths("2026-07-18", live_dir=live_dir)
            session_path.parent.mkdir(parents=True)
            session_path.write_text("stop\n", encoding="utf-8")
            hit = check_kill_switch("2026-07-18", live_dir=live_dir)
            assert hit is not None
            self.assertEqual(hit.scope, "session")

    def test_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            live_dir = Path(tmp)
            global_path, _ = kill_paths("2026-07-18", live_dir=live_dir)
            global_path.write_text("stop\n", encoding="utf-8")
            self.assertIsNone(
                check_kill_switch("2026-07-18", enabled=False, live_dir=live_dir)
            )


if __name__ == "__main__":
    unittest.main()
