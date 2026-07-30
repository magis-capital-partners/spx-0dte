"""Merge offset-strike A/B shard checkpoints."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))
sys.path.insert(0, str(ROOT / "scripts"))

from run_offset_strike_ab import (  # noqa: E402
    HOLDOUT_START,
    OUT,
    SELECTION_END,
    build_variants,
    filter_daily,
    load_checkpoint,
    summarize_variant,
    work_dir,
)


def merge_daily(a: List[dict], b: List[dict]) -> List[dict]:
    by_date = {row["date"]: row for row in a}
    for row in b:
        by_date[row["date"]] = row
    return [by_date[d] for d in sorted(by_date)]


def write_summary(path: Path, rows: List[dict]) -> None:
    path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    if not rows:
        return
    fields: List[str] = []
    seen = set()
    for row in rows:
        for k in row:
            if k not in seen:
                seen.add(k)
                fields.append(k)
    with path.with_suffix(".csv").open("w", newline="", encoding="utf-8") as handle:
        w = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shards", type=int, default=8)
    args = parser.parse_args()

    names = list(build_variants().keys())
    daily_by: Dict[str, List[dict]] = {n: [] for n in names}

    if args.shards <= 1:
        ckpt = load_checkpoint(OUT / "checkpoint.json")
        if not ckpt or not ckpt.get("complete"):
            raise SystemExit("Incomplete single-process checkpoint")
        daily_by = ckpt["daily_by"]
    else:
        for shard in range(args.shards):
            ckpt = load_checkpoint(work_dir(shard, args.shards) / "checkpoint.json")
            if not ckpt or not ckpt.get("complete"):
                raise SystemExit(
                    f"Shard {shard} incomplete "
                    f"({ckpt.get('oos_done') if ckpt else 'missing'}/"
                    f"{ckpt.get('oos_total') if ckpt else '?'})"
                )
            for name in names:
                daily_by[name] = merge_daily(daily_by[name], ckpt["daily_by"].get(name, []))

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "daily_by.json").write_text(
        json.dumps({k: daily_by[k] for k in names}, separators=(",", ":")),
        encoding="utf-8",
    )

    ref = "prod"
    for period, start, end in (
        ("full", None, None),
        ("selection", None, SELECTION_END),
        ("holdout", HOLDOUT_START, None),
    ):
        rows = []
        ref_stats = summarize_variant(
            ref, filter_daily(daily_by[ref], start=start, end=end), period
        )
        for name in names:
            daily = filter_daily(daily_by[name], start=start, end=end)
            row = summarize_variant(name, daily, period)
            row["calmar_delta_vs_prod"] = round(row["calmar"] - ref_stats["calmar"], 4)
            row["cagr_delta_vs_prod"] = round(
                float(row["cagr_pct"]) - float(ref_stats["cagr_pct"]), 2
            )
            row["maxdd_delta_vs_prod"] = round(
                float(row["max_drawdown_pct"]) - float(ref_stats["max_drawdown_pct"]), 2
            )
            row["worst_delta_vs_prod"] = round(
                float(row["worst_day_pct"]) - float(ref_stats["worst_day_pct"]), 2
            )
            rows.append(row)
        rows.sort(key=lambda r: (-float(r["calmar"]), -float(r["cagr_pct"])))
        write_summary(OUT / f"summary_{period}.json", rows)

    print(f"Merged offset-strike A/B -> {OUT}", flush=True)


if __name__ == "__main__":
    main()
