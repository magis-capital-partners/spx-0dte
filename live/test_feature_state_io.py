"""Feature state persist/reload."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))
sys.path.insert(0, str(ROOT / "live"))

from live_features import SessionFeatureState  # noqa: E402
from feature_state_io import load_feature_state, save_feature_state  # noqa: E402


class FeatureStateIOTests(unittest.TestCase):
    def test_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            live_dir = Path(tmp)
            state = SessionFeatureState(
                first_straddle=45.0,
                first_minutes=380.0,
                previous_spot=5500.0,
                spot_history=[5490.0, 5495.0, 5500.0],
            )
            save_feature_state("2026-07-28", state, live_dir=live_dir)
            loaded = load_feature_state("2026-07-28", live_dir=live_dir)
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.first_straddle, 45.0)
            self.assertEqual(loaded.spot_history, [5490.0, 5495.0, 5500.0])


if __name__ == "__main__":
    unittest.main()
