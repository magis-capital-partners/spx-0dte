"""Merge suite+shard checkpoints into batch summary.json."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from run_cagr_improvement_batches import (  # noqa: E402
    BATCH_BUILDERS,
    BATCH_SUITES,
    OUT,
    build_summaries,
    checkpoint_path,
    empty_trade_agg,
    filter_variants,
    load_checkpoint,
    print_results,
    work_dir,
    write_summary,
)


def merge_trade_agg(a: dict, b: dict) -> dict:
    return {
        "trades": a.get("trades", 0) + b.get("trades", 0),
        "wins": a.get("wins", 0) + b.get("wins", 0),
        "stopped": a.get("stopped", 0) + b.get("stopped", 0),
        "total_pnl": a.get("total_pnl", 0.0) + b.get("total_pnl", 0.0),
    }


def merge_daily(a: List[dict], b: List[dict]) -> List[dict]:
    by_date = {row["date"]: row for row in a}
    for row in b:
        by_date[row["date"]] = row
    return [by_date[d] for d in sorted(by_date)]


def merge_suite_shards(batch_id: int, suite: str, shards: int) -> Tuple[Dict[str, List[dict]], Dict[str, dict]]:
    variants = filter_variants(batch_id, suite)
    names = [x[1] for x in variants]
    daily_by: Dict[str, List[dict]] = {n: [] for n in names}
    trade_agg: Dict[str, dict] = {n: empty_trade_agg() for n in names}

    for shard in range(shards):
        ckpt = load_checkpoint(checkpoint_path(work_dir(batch_id, suite, shard, shards)))
        if not ckpt:
            raise SystemExit(f"Missing checkpoint batch={batch_id} suite={suite} shard={shard}")
        if not ckpt.get("complete"):
            raise SystemExit(
                f"Incomplete batch={batch_id} suite={suite} shard={shard} "
                f"({ckpt.get('oos_done')}/{ckpt.get('oos_total')}). Resume job then re-merge."
            )
        for name in names:
            daily_by[name] = merge_daily(daily_by[name], ckpt["daily_by"].get(name, []))
            trade_agg[name] = merge_trade_agg(trade_agg[name], ckpt["trade_agg"].get(name, empty_trade_agg()))

    return daily_by, trade_agg


def merge_batch(batch_id: int, shards: int, suite_mode: bool) -> tuple[List[dict], dict]:
    if suite_mode:
        suites = BATCH_SUITES[batch_id]
        all_variants = BATCH_BUILDERS[batch_id]()
        names = [x[1] for x in all_variants]
        daily_by: Dict[str, List[dict]] = {n: [] for n in names}
        trade_agg: Dict[str, dict] = {n: empty_trade_agg() for n in names}

        for suite in suites:
            s_daily, s_agg = merge_suite_shards(batch_id, suite, shards)
            for name in s_daily:
                daily_by[name] = s_daily[name]
            for name in s_agg:
                trade_agg[name] = s_agg[name]

        return build_summaries(batch_id, all_variants, daily_by, trade_agg)

    # Legacy: no suite subdirs
    from run_cagr_improvement_batches import shard_dir  # noqa: E402

    variants = BATCH_BUILDERS[batch_id]()
    names = [x[1] for x in variants]
    daily_by: Dict[str, List[dict]] = {n: [] for n in names}
    trade_agg: Dict[str, dict] = {n: empty_trade_agg() for n in names}

    for shard in range(shards):
        ckpt = load_checkpoint(checkpoint_path(shard_dir(batch_id, shard, shards)))
        if not ckpt or not ckpt.get("complete"):
            raise SystemExit(f"Incomplete or missing legacy shard {shard} for batch {batch_id}")
        for name in names:
            daily_by[name] = merge_daily(daily_by[name], ckpt["daily_by"].get(name, []))
            trade_agg[name] = merge_trade_agg(trade_agg[name], ckpt["trade_agg"].get(name, empty_trade_agg()))

    return build_summaries(batch_id, variants, daily_by, trade_agg)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", type=int, required=True, choices=[1, 2, 3])
    parser.add_argument("--shards", type=int, default=4)
    parser.add_argument("--suite-mode", action="store_true", help="Merge suite_*/shard_* layout")
    args = parser.parse_args()

    summaries, ref = merge_batch(args.batch, args.shards, args.suite_mode)
    out_dir = OUT / f"batch{args.batch}"
    write_summary(out_dir, summaries, ref)
    print_results(args.batch, summaries, ref)
    print(f"\nMerged batch {args.batch} -> {out_dir / 'summary.json'}")


if __name__ == "__main__":
    main()
