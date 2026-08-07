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

    def test_signal_parity_panels_present(self) -> None:
        """The SignalParity/TrancheParityTable panels consume reconcile.json's
        signal_parity block; renaming a data key must fail loudly here."""
        source = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
        self.assertIn("function SignalParity", source)
        self.assertIn("function TrancheParityTable", source)
        self.assertIn("Signal parity — live skew_z vs backtest replay", source)
        self.assertIn("Tranche signal parity — live vs backtest replay", source)
        # Data-contract keys produced by build_dashboard_data.build_live().
        for key in (
            "skew_parity_mean_abs", "skew_parity_max_abs",
            "skew_gate_flips", "signal_parity_alert",
            "entries_bull_put", "entries_bear_call",
            "tranche_signals", "signal_parity",
        ):
            self.assertIn(key, source)
        # SessionNow gate-proximity chip reads the sanitized gist field.
        self.assertIn("last_tranche", source)
        self.assertIn("Skew z (last tranche)", source)


if __name__ == "__main__":
    unittest.main()
