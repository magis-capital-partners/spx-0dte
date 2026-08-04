"""Run identity metadata: git hash, config hash, signal version."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "live"))
sys.path.insert(0, str(ROOT / "simulator"))

from run_metadata import (  # noqa: E402
    build_run_metadata,
    config_hash,
    git_commit_hash,
    signal_version,
)


class RunMetadataTests(unittest.TestCase):
    def test_git_commit_hash_available_in_repo(self) -> None:
        commit = git_commit_hash()
        self.assertTrue(commit)
        self.assertNotEqual(commit, "unknown")

    def test_config_hash_deterministic_and_sensitive(self) -> None:
        a = {"stop_confirm_seconds": 120.0, "contracts_per_tranche": 1}
        b = {"contracts_per_tranche": 1, "stop_confirm_seconds": 120.0}
        self.assertEqual(config_hash(a), config_hash(b))  # key order irrelevant
        changed = dict(a, stop_confirm_seconds=60.0)
        self.assertNotEqual(config_hash(a), config_hash(changed))
        self.assertEqual(len(config_hash(a)), 12)

    def test_signal_version_stable(self) -> None:
        v = signal_version()
        self.assertTrue(v.startswith("v"))
        self.assertEqual(v, signal_version())

    def test_build_run_metadata_shape(self) -> None:
        meta = build_run_metadata({"mode": "live"}, {"stop_multiple": 3.0})
        for key in ("run_id", "git_commit", "config_hash", "signal_version", "pid"):
            self.assertIn(key, meta)
        self.assertEqual(len(meta["run_id"]), 12)
        # Distinct processes/runs must be distinguishable.
        again = build_run_metadata({"mode": "live"}, {"stop_multiple": 3.0})
        self.assertNotEqual(meta["run_id"], again["run_id"])
        self.assertEqual(meta["config_hash"], again["config_hash"])


if __name__ == "__main__":
    unittest.main()
