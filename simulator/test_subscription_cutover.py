"""Dress rehearsal: the ThetaData subscription ends and the pipeline survives.

Drives the REAL scripts (refresh_live_baselines.py, build_processed_from_ib.py)
over a scratch processed dir through the full transition:

  phase 1  vendor days only            -> normal rolling window
  phase 2  vendor stops, IB days start -> frozen vendor window + countdown note
  phase 3  40 IB days accumulated      -> automatic cutover to IB-only window

and finally loads the phase-3 payload through the executor's own
load_baselines_file to prove the live loop accepts it.
"""
from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))
sys.path.insert(0, str(ROOT / "live"))

from chain_recorder import ChainMinuteRecorder  # noqa: E402
from live_features import MinuteFeatureSample  # noqa: E402
from mbh_simulator import OptionQuote  # noqa: E402

REFRESH = ROOT / "scripts" / "refresh_live_baselines.py"
CONVERT = ROOT / "scripts" / "build_processed_from_ib.py"


def _weekdays(start: date, count: int) -> list[str]:
    out: list[str] = []
    d = start
    while len(out) < count:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _write_vendor_day(processed: Path, day: str) -> None:
    day_dir = processed / "symbol=SPXW" / f"date={day}"
    day_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for minute in range(3):
        rows.append({
            "timestamp": f"{day}T09:{31 + minute}:00",
            "straddle_residual_z": 0.01 * minute,
            "skew_z": 0.02 + 0.001 * minute,
            "term_ratio_z": -0.4,
            "trend_score": 0.005 * minute,
            "realized_vs_implied_z": 0.001,
        })
    with (day_dir / "signals.csv").open("w", newline="", encoding="utf-8") as h:
        writer = csv.DictWriter(h, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _record_ib_day(live_dir: Path, day: str) -> None:
    rec = ChainMinuteRecorder(live_dir / day / "chain_minutes.jsonl")
    base = datetime.fromisoformat(f"{day}T09:31:00")
    for minute in range(3):
        ts = base + timedelta(minutes=minute)
        spot = 7500.0 + minute
        quotes = [
            OptionQuote(ts, day, "CALL", 7500.0, 17.0, 17.6, 0.51, 0.14, spot),
            OptionQuote(ts, day, "PUT", 7500.0, 16.8, 17.4, -0.49, 0.141, spot),
            OptionQuote(ts, day, "CALL", 7580.0, 2.1, 2.3, 0.25, 0.128, spot),
            OptionQuote(ts, day, "PUT", 7410.0, 2.4, 2.6, -0.25, 0.152, spot),
        ]
        rec.record_sample(MinuteFeatureSample(ts, spot, quotes, 5))


def _run(script: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True, text=True, check=False,
    )


class SubscriptionCutoverTests(unittest.TestCase):
    def test_full_transition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            processed = tmp / "processed"
            live_dir = tmp / "live"
            out = tmp / "baselines.json"

            # Enough vendor history for a 5-day window (small train_count so
            # the rehearsal stays fast; the logic is count-agnostic).
            train_count = 5
            vendor_days = _weekdays(date(2026, 6, 1), 10)
            for d in vendor_days:
                _write_vendor_day(processed, d)

            def refresh(as_of: str) -> dict:
                proc = _run(
                    REFRESH,
                    "--processed-dir", str(processed),
                    "--train-count", str(train_count),
                    "--as-of", as_of,
                    "--out", str(out),
                )
                self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
                return json.loads(out.read_text(encoding="utf-8"))

            # ---- Phase 1: vendor era, business as usual -------------------
            payload = refresh("2026-06-20")
            self.assertEqual(payload["train_source"], "thetadata")
            self.assertNotIn("train_note", payload)
            self.assertEqual(payload["train_dates"], vendor_days[-train_count:])

            # ---- Phase 2: subscription ended; first IB days recorded ------
            ib_days = _weekdays(date(2026, 6, 22), 7)
            for d in ib_days[:2]:
                _record_ib_day(live_dir, d)
            proc = _run(
                CONVERT, "--auto",
                "--live-dir", str(live_dir),
                "--processed-dir", str(processed),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)

            payload = refresh("2026-06-24")
            self.assertEqual(payload["train_source"], "thetadata")
            self.assertIn("CUTOVER PENDING", payload["train_note"])
            self.assertEqual(payload["train_dates"], vendor_days[-train_count:])

            # ---- Phase 3: enough IB days -> automatic cutover -------------
            for d in ib_days[2:]:
                _record_ib_day(live_dir, d)
            proc = _run(
                CONVERT, "--auto",
                "--live-dir", str(live_dir),
                "--processed-dir", str(processed),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)

            payload = refresh("2026-07-05")
            self.assertEqual(payload["train_source"], "ib_live")
            self.assertNotIn("train_note", payload)
            self.assertEqual(payload["train_dates"], ib_days[-train_count:])
            # The window is pure IB — not one vendor day crossed the seam.
            self.assertFalse(set(payload["train_dates"]) & set(vendor_days))

            # ---- The executor accepts the cutover payload -----------------
            from ib_executor import load_baselines_file
            full, core = load_baselines_file(out, max_age_days=3)
            self.assertEqual(full["train_source"], "ib_live")
            self.assertIn("global", core)
            self.assertNotIn("train_source", core)  # meta stripped from z-core


if __name__ == "__main__":
    unittest.main()
