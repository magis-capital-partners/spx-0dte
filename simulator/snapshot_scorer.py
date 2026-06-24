from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Tuple


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESULTS = ROOT / "data" / "results"
DEFAULT_TARGETS = ROOT / "position_snapshots.csv"


def read_csv(path: Path) -> List[dict]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def target_key(row: dict) -> Tuple[str, float]:
    return row["source"], float(row["strike"])


def sim_key(row: dict) -> Tuple[str, float]:
    return row["timestamp"], float(row["strike"])


def summarize(rows: Iterable[dict], call_key: str, put_key: str) -> dict:
    rows = list(rows)
    long_calls = sum(int(float(row[call_key])) for row in rows if int(float(row[call_key])) > 0)
    short_calls = -sum(int(float(row[call_key])) for row in rows if int(float(row[call_key])) < 0)
    long_puts = sum(int(float(row[put_key])) for row in rows if int(float(row[put_key])) > 0)
    short_puts = -sum(int(float(row[put_key])) for row in rows if int(float(row[put_key])) < 0)
    return {
        "long_calls": long_calls,
        "short_calls": short_calls,
        "net_calls": long_calls - short_calls,
        "long_puts": long_puts,
        "short_puts": short_puts,
        "net_puts": long_puts - short_puts,
        "total_longs": long_calls + long_puts,
        "total_shorts": short_calls + short_puts,
        "net_contracts": long_calls + long_puts - short_calls - short_puts,
    }


def aggregate_score(target: dict, simulated: dict) -> float:
    keys = ["long_calls", "short_calls", "long_puts", "short_puts", "total_longs", "total_shorts", "net_contracts"]
    total = 0.0
    for key in keys:
        total += abs(simulated[key] - target[key]) / max(abs(target[key]), 1)
    return total / len(keys)


def exact_strike_score(target_rows: List[dict], sim_rows: List[dict]) -> float:
    target_by_strike = {float(row["strike"]): row for row in target_rows}
    sim_by_strike = {float(row["strike"]): row for row in sim_rows}
    strikes = sorted(set(target_by_strike) | set(sim_by_strike))
    numerator = 0.0
    denominator = 0.0
    for strike in strikes:
        target = target_by_strike.get(strike, {"call_contracts": 0, "put_contracts": 0})
        sim = sim_by_strike.get(strike, {"call_contracts": 0, "put_contracts": 0})
        for key in ["call_contracts", "put_contracts"]:
            target_value = int(float(target[key]))
            sim_value = int(float(sim[key]))
            numerator += abs(sim_value - target_value)
            denominator += max(abs(target_value), 1)
    return numerator / max(denominator, 1.0)


def timestamp_source_map(target_source: str) -> str:
    mapping = {
        "ddq_2026_03_02_1100": "2026-03-02T11:00:00",
        "ddq_2026_03_02_1500": "2026-03-02T15:00:00",
    }
    return mapping.get(target_source, "")


def score_snapshots(targets_path: Path, simulated_path: Path, target_sources: List[str]) -> List[dict]:
    target_rows = read_csv(targets_path)
    simulated_rows = read_csv(simulated_path)
    results = []
    for source in target_sources:
        timestamp = timestamp_source_map(source)
        if not timestamp:
            continue
        target_subset = [row for row in target_rows if row["source"] == source]
        sim_subset = [row for row in simulated_rows if row["timestamp"] == timestamp]
        if not target_subset or not sim_subset:
            continue
        target_summary = summarize(target_subset, "call_contracts", "put_contracts")
        sim_summary = summarize(sim_subset, "call_contracts", "put_contracts")
        exact = exact_strike_score(target_subset, sim_subset)
        aggregate = aggregate_score(target_summary, sim_summary)
        results.append(
            {
                "target_source": source,
                "timestamp": timestamp,
                "exact_strike_score": round(exact, 6),
                "aggregate_score": round(aggregate, 6),
                "combined_score": round((exact + aggregate) / 2.0, 6),
                **{f"target_{key}": value for key, value in target_summary.items()},
                **{f"sim_{key}": value for key, value in sim_summary.items()},
            }
        )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Score simulated holdings against MBH/DDQ position snapshots.")
    parser.add_argument("--targets", default=str(DEFAULT_TARGETS))
    parser.add_argument("--simulated", default=str(DEFAULT_RESULTS / "simulated_holdings_snapshots.csv"))
    parser.add_argument("--output", default=str(DEFAULT_RESULTS / "snapshot_scores.csv"))
    parser.add_argument("--target-sources", nargs="+", default=["ddq_2026_03_02_1100", "ddq_2026_03_02_1500"])
    args = parser.parse_args()

    rows = score_snapshots(Path(args.targets), Path(args.simulated), args.target_sources)
    write_csv(Path(args.output), rows)
    print(f"wrote {args.output}")
    for row in rows:
        print(row)


if __name__ == "__main__":
    main()
