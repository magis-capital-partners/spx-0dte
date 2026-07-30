"""Merge overnight Calmar suite shard checkpoints (Wave 3: full + selection + holdout)."""
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
from overnight_calmar_variants import (  # noqa: E402
    HOLDOUT_START,
    SELECTION_END,
    build_all_variants,
)


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


def write_summary(path: Path, summaries: List[dict]) -> None:
    path.write_text(json.dumps(summaries, indent=2), encoding="utf-8")
    if not summaries:
        return
    csv_path = path.with_suffix(".csv")
    fieldnames = list(summaries[0].keys())
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(summaries)


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
                f"Shard {shard} incomplete ({ckpt.get('oos_done') if ckpt else 'missing'}/"
                f"{ckpt.get('oos_total') if ckpt else '?'})"
            )
        for name in names:
            daily_by[name] = merge_daily(daily_by[name], ckpt["daily_by"].get(name, []))
            trade_agg[name] = merge_trade_agg(trade_agg[name], ckpt["trade_agg"].get(name, empty_trade_agg()))

    OUT.mkdir(parents=True, exist_ok=True)

    full = build_summaries(variants, daily_by, trade_agg, period="full")
    selection = build_summaries(
        variants, daily_by, trade_agg, period="selection", end=SELECTION_END
    )
    holdout = build_summaries(
        variants, daily_by, trade_agg, period="holdout", start=HOLDOUT_START
    )

    write_summary(OUT / "summary.json", full)
    write_summary(OUT / "summary_selection.json", selection)
    write_summary(OUT / "summary_holdout.json", holdout)

    # Compact daily store for post-hoc era cuts (dates + pnl only).
    daily_compact = {
        name: [{"date": r["date"], "net_pnl": r["net_pnl"], "era": r.get("era")} for r in rows]
        for name, rows in daily_by.items()
    }
    (OUT / "daily_by_compact.json").write_text(json.dumps(daily_compact), encoding="utf-8")

    print(
        f"Merged {args.shards} shards -> {OUT / 'summary.json'} "
        f"({len(full)} variants; selection<={SELECTION_END}, holdout>={HOLDOUT_START})"
    )


if __name__ == "__main__":
    main()
