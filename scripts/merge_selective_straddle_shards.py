"""Merge selective overlay shard checkpoints into full/selection/holdout summaries."""
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

from run_selective_straddle_overlay import (  # noqa: E402
    OUT,
    build_summaries,
    empty_trade_agg,
    load_checkpoint,
    load_variants,
    out_root,
    work_dir,
    _write_a1c_report,
)
from selective_overlay_variants import HOLDOUT_START, SELECTION_END  # noqa: E402


def merge_daily(a: List[dict], b: List[dict]) -> List[dict]:
    by_date = {row["date"]: row for row in a}
    for row in b:
        by_date[row["date"]] = row
    return [by_date[d] for d in sorted(by_date)]


def merge_trade_agg(a: dict, b: dict) -> dict:
    keys = set(a) | set(b)
    out = {}
    for k in keys:
        av, bv = a.get(k, 0), b.get(k, 0)
        if isinstance(av, (int, float)) or isinstance(bv, (int, float)):
            out[k] = (av or 0) + (bv or 0)
        else:
            out[k] = av or bv
    return out


def write_summary(path: Path, summaries: List[dict]) -> None:
    path.write_text(json.dumps(summaries, indent=2), encoding="utf-8")
    if not summaries:
        return
    with path.with_suffix(".csv").open("w", newline="", encoding="utf-8") as handle:
        w = csv.DictWriter(handle, fieldnames=list(summaries[0].keys()))
        w.writeheader()
        w.writerows(summaries)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True)
    parser.add_argument("--shards", type=int, default=8)
    parser.add_argument("--winners-json", type=Path, default=None)
    args = parser.parse_args()

    variants = load_variants(args.phase, args.winners_json)
    names = [v.name for v in variants]
    daily_by: Dict[str, List[dict]] = {n: [] for n in names}
    trade_agg: Dict[str, dict] = {n: empty_trade_agg() for n in names}
    a1c_rows: List[dict] = []

    for shard in range(args.shards):
        ckpt = load_checkpoint(work_dir(args.phase, shard, args.shards) / "checkpoint.json")
        if not ckpt or not ckpt.get("complete"):
            raise SystemExit(
                f"Shard {shard} incomplete ({ckpt.get('oos_done') if ckpt else 'missing'}/"
                f"{ckpt.get('oos_total') if ckpt else '?'})"
            )
        for name in names:
            daily_by[name] = merge_daily(daily_by[name], ckpt["daily_by"].get(name, []))
            trade_agg[name] = merge_trade_agg(
                trade_agg[name], ckpt["trade_agg"].get(name, empty_trade_agg())
            )
        a1c_rows.extend(ckpt.get("a1c_rows") or [])

    root = out_root(args.phase)
    root.mkdir(parents=True, exist_ok=True)
    full = build_summaries(variants, daily_by, trade_agg, period="full")
    selection = build_summaries(variants, daily_by, trade_agg, period="selection", end=SELECTION_END)
    holdout = build_summaries(variants, daily_by, trade_agg, period="holdout", start=HOLDOUT_START)
    write_summary(root / "summary_full.json", full)
    write_summary(root / "summary_selection.json", selection)
    write_summary(root / "summary_holdout.json", holdout)

    # persist merged daily for summarize diagnostics
    (root / "daily_by.json").write_text(
        json.dumps({k: daily_by[k] for k in names}, indent=2), encoding="utf-8"
    )
    (root / "trade_agg.json").write_text(json.dumps(trade_agg, indent=2), encoding="utf-8")

    if args.phase.upper() == "A1C":
        _write_a1c_report(a1c_rows, root)

    print(
        f"Merged phase={args.phase}: full={len(full)} sel={len(selection)} hold={len(holdout)} -> {root}"
    )


if __name__ == "__main__":
    main()
