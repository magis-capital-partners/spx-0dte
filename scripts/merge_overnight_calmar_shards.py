"""Merge overnight Calmar suite shard checkpoints."""
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

from run_overnight_calmar_suite import (  # noqa: E402
    OUT,
    build_summaries,
    empty_trade_agg,
    load_checkpoint,
    work_dir,
)
from overnight_calmar_variants import build_all_variants  # noqa: E402


def merge_daily(a: List[dict], b: List[dict]) -> List[dict]:
    by_date = {row["date"]: row for row in a}
    for row in b:
        by_date[row["date"]] = row
    return [by_date[d] for d in sorted(by_date)]


def merge_trade_agg(a: dict, b: dict) -> dict:
    return {
        "trades": a.get("trades", 0) + b.get("trades", 0),
        "wins": a.get("wins", 0) + b.get("wins", 0),
        "stopped": a.get("stopped", 0) + b.get("stopped", 0),
        "total_pnl": a.get("total_pnl", 0.0) + b.get("total_pnl", 0.0),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shards", type=int, default=4)
    args = parser.parse_args()

    variants = build_all_variants()
    names = [v[1] for v in variants]
    daily_by: Dict[str, List[dict]] = {n: [] for n in names}
    trade_agg: Dict[str, dict] = {n: empty_trade_agg() for n in names}

    for shard in range(args.shards):
        ckpt = load_checkpoint(work_dir(shard, args.shards) / "checkpoint.json")
        if not ckpt or not ckpt.get("complete"):
            raise SystemExit(
                f"Shard {shard} incomplete ({ckpt.get('oos_done') if ckpt else 'missing'}/{ckpt.get('oos_total') if ckpt else '?'})"
            )
        for name in names:
            daily_by[name] = merge_daily(daily_by[name], ckpt["daily_by"].get(name, []))
            trade_agg[name] = merge_trade_agg(trade_agg[name], ckpt["trade_agg"].get(name, empty_trade_agg()))

    summaries = build_summaries(variants, daily_by, trade_agg)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "summary.json").write_text(json.dumps(summaries, indent=2), encoding="utf-8")

    if summaries:
        fieldnames = list(summaries[0].keys())
        with (OUT / "summary.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(summaries)

    print(f"Merged {args.shards} shards -> {OUT / 'summary.json'} ({len(summaries)} variants)")


if __name__ == "__main__":
    main()
