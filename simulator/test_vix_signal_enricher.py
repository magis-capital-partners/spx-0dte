"""Unit tests for VIX signal enrichment."""
from __future__ import annotations

import csv
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from vix_daily import VixDay  # noqa: E402
from vix_signal_enricher import enrich_signals_for_day, enrich_symbol  # noqa: E402


def _write_signals(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def test_enrich_signals_for_day() -> None:
    rows = [
        {"timestamp": "2024-01-02T09:32:00", "vix": "", "trend_score": "0.1"},
        {"timestamp": "2024-01-02T09:47:00", "vix": "", "trend_score": "0.2"},
    ]
    vix_day = VixDay("2024-01-02", 14.5, 15.0, 14.0, 14.8, 13.9)
    enriched = enrich_signals_for_day(rows, vix_day)
    assert all(row["vix"] == "14.5000" for row in enriched)
    assert all(row["vix_prior_close"] == "13.9000" for row in enriched)


def test_enrich_symbol_updates_processed_day() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        day_dir = root / "symbol=SPXW" / "date=2024-01-02"
        day_dir.mkdir(parents=True)
        _write_signals(
            day_dir / "signals.csv",
            [{"timestamp": "2024-01-02T09:32:00", "vix": "", "trend_score": "0.0"}],
        )
        vix = {"2024-01-02": VixDay("2024-01-02", 16.0, 16.5, 15.8, 16.2, 15.5)}
        summary = enrich_symbol(root, "SPXW", vix)
        assert summary["updated_days"] == 1
        with (day_dir / "signals.csv").open(newline="", encoding="utf-8") as handle:
            row = next(csv.DictReader(handle))
        assert row["vix"] == "16.0000"


def main() -> None:
    test_enrich_signals_for_day()
    test_enrich_symbol_updates_processed_day()
    print("vix_signal_enricher tests: PASS")


if __name__ == "__main__":
    main()
