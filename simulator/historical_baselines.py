from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from pathlib import Path
from statistics import mean, pstdev
from typing import Dict, Iterable, List, Tuple


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROCESSED = ROOT / "data" / "processed"
DEFAULT_MODEL_DIR = ROOT / "data" / "models"

FEATURES = ["straddle_residual_z", "skew_z", "term_ratio_z", "trend_score", "realized_vs_implied_z"]

# Statistical floor on per-minute std, as a fraction of the feature's global std.
# Early-session minutes are structurally constant across every training day
# (trend/straddle at 09:31; realized-vs-implied until 6 spot observations), so
# their pstdev is exactly 0 and the old 1e-9 divide-by-zero guard turned any
# nonzero live raw value into a z-score on the order of 1e6 (2026-08-04:
# realized_vs_implied_z = -1.29M at 09:32). Flooring at 10% of the global std
# keeps healthy minutes untouched while making degenerate minutes produce
# finite, meaningful z-scores on both the live and backtest paths.
STD_FLOOR_FRAC = 0.10


def read_csv(path: Path) -> List[dict]:
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


def minute_key(timestamp: str) -> str:
    # Works for ISO timestamps emitted by feature_builder.
    return timestamp.split("T", 1)[1][:5]


def safe_float(value: object, default: float = 0.0) -> float:
    try:
        if value in {"", None}:
            return default
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def processed_signal_path(processed_dir: Path, symbol: str, trade_date: str, filename: str = "signals.csv") -> Path:
    return processed_dir / f"symbol={symbol}" / f"date={trade_date}" / filename


def compute_baselines(processed_dir: Path, symbol: str, train_dates: Iterable[str]) -> dict:
    values: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
    global_values: Dict[str, List[float]] = defaultdict(list)

    for trade_date in train_dates:
        path = processed_signal_path(processed_dir, symbol, trade_date)
        if not path.exists():
            continue
        for row in read_csv(path):
            key = minute_key(row["timestamp"])
            for feature in FEATURES:
                value = safe_float(row.get(feature))
                values[key][feature].append(value)
                global_values[feature].append(value)

    baselines = {"minutes": {}, "global": {}, "features": FEATURES}
    for feature in FEATURES:
        series = global_values[feature]
        baselines["global"][feature] = {
            "mean": mean(series) if series else 0.0,
            "std": max(pstdev(series), 1e-9) if len(series) > 1 else 1.0,
            "count": len(series),
        }

    for key, feature_map in values.items():
        baselines["minutes"][key] = {}
        for feature in FEATURES:
            series = feature_map.get(feature, [])
            fallback = baselines["global"][feature]
            std_floor = STD_FLOOR_FRAC * fallback["std"]
            baselines["minutes"][key][feature] = {
                "mean": mean(series) if series else fallback["mean"],
                "std": max(pstdev(series), std_floor, 1e-9) if len(series) > 1 else fallback["std"],
                "count": len(series),
            }
    return baselines


def zscore(value: float, stats: dict) -> float:
    return (value - stats["mean"]) / max(stats["std"], 1e-9)


def transform_rows(rows: List[dict], baselines: dict) -> List[dict]:
    transformed: List[dict] = []
    for row in rows:
        key = minute_key(row["timestamp"])
        minute_stats = baselines["minutes"].get(key, {})
        out = dict(row)
        for feature in FEATURES:
            raw_value = safe_float(row.get(feature))
            out[f"raw_{feature}"] = raw_value
            stats = minute_stats.get(feature, baselines["global"][feature])
            out[feature] = zscore(raw_value, stats)
        transformed.append(out)
    return transformed


def save_baselines(path: Path, baselines: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(baselines, indent=2), encoding="utf-8")


def load_baselines(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_dates(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Build and apply historical no-lookahead signal baselines.")
    parser.add_argument("--symbol", default="SPXW")
    parser.add_argument("--train-dates", required=True, help="Comma-separated training dates.")
    parser.add_argument("--apply-dates", required=True, help="Comma-separated dates to transform.")
    parser.add_argument("--processed-dir", default=str(DEFAULT_PROCESSED))
    parser.add_argument("--model-dir", default=str(DEFAULT_MODEL_DIR))
    parser.add_argument("--model-name", default="historical_signal_baselines")
    parser.add_argument("--output-filename", default="signals_historical.csv")
    args = parser.parse_args()

    processed_dir = Path(args.processed_dir)
    model_dir = Path(args.model_dir)
    train_dates = parse_dates(args.train_dates)
    apply_dates = parse_dates(args.apply_dates)

    baselines = compute_baselines(processed_dir, args.symbol, train_dates)
    baseline_path = model_dir / f"{args.model_name}.json"
    save_baselines(baseline_path, baselines)

    for trade_date in apply_dates:
        input_path = processed_signal_path(processed_dir, args.symbol, trade_date)
        output_path = processed_signal_path(processed_dir, args.symbol, trade_date, args.output_filename)
        rows = read_csv(input_path)
        write_csv(output_path, transform_rows(rows, baselines))
        print(f"{trade_date} wrote {output_path}")
    print(f"baseline_model={baseline_path}")


if __name__ == "__main__":
    main()
