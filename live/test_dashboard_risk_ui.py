"""Guard the dashboard's risk empty state against source-encoding regressions."""
from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DashboardRiskUiTests(unittest.TestCase):
    def test_risk_empty_state_uses_ascii_safe_escape_and_restart_message(self) -> None:
        source = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
        self.assertIn('const MISSING = "\\u2014";', source)
        self.assertIn("Risk metrics will appear after the executor", source)
        for label in (
            "Max loss (no stop)", "Planned stop loss*",
            "Defined-risk margin", "Marked return / margin",
        ):
            line = next(row for row in source.splitlines() if label in row)
            self.assertIn("MISSING", line)
            self.assertNotIn("â€", line)


if __name__ == "__main__":
    unittest.main()
