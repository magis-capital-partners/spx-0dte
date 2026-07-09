"""Unit tests for daily VIX calendar helpers."""
from __future__ import annotations

import csv
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from vix_daily import (  # noqa: E402
    VixDay,
    load_vix_daily,
    merge_vix_rows,
    regime_bucket,
    summarize_coverage,
    write_vix_csv,
)


def test_regime_buckets() -> None:
    assert regime_bucket(10.0) == "ultra_low_lt12"
    assert regime_bucket(14.0) == "low_12_15"
    assert regime_bucket(20.0) == "optimal_17_25"
    assert regime_bucket(40.0) == "extreme_gt35"


def test_write_load_roundtrip() -> None:
    rows = [
        VixDay("2024-01-02", 13.5, 14.0, 13.0, 13.8, None),
        VixDay("2024-01-03", 14.2, 15.0, 14.0, 14.9, 13.8),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "vix_daily.csv"
        assert write_vix_csv(path, rows) == 2
        loaded = load_vix_daily(path)
        assert loaded["2024-01-02"].open == 13.5
        assert loaded["2024-01-03"].prior_close == 13.8


def test_merge_recomputes_prior_close() -> None:
    existing = {
        "2024-01-02": VixDay("2024-01-02", 12.0, 12.5, 11.8, 12.2, None),
    }
    fresh = [VixDay("2024-01-03", 13.0, 13.5, 12.8, 13.1, None)]
    merged = merge_vix_rows(existing, fresh)
    assert merged[0].prior_close is None
    assert merged[1].prior_close == 12.2


def test_summarize_coverage() -> None:
    vix = {"2024-01-02": VixDay("2024-01-02", 12.0, 12.5, 11.8, 12.2, None)}
    summary = summarize_coverage(vix, ["2024-01-02", "2024-01-03"])
    assert summary["covered"] == 1
    assert summary["missing_count"] == 1


def main() -> None:
    test_regime_buckets()
    test_write_load_roundtrip()
    test_merge_recomputes_prior_close()
    test_summarize_coverage()
    print("vix_daily tests: PASS")


if __name__ == "__main__":
    main()
