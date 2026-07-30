"""Overnight Calmar improvement suite — all phases, checkpointed, shardable.

Usage:
  python scripts/run_overnight_calmar_suite.py --shard 0 --shards 4 --resume
  python scripts/merge_overnight_calmar_shards.py --shards 4
  python scripts/summarize_overnight_calmar_suite.py
"""
from __future__ import annotations

import argparse
import json
import sys
import time as time_mod
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
SIM = ROOT / "simulator"
sys.path.insert(0, str(SIM))
sys.path.insert(0, str(ROOT / "scripts"))

from expiry_calendar import DEFAULT_RULES, discover_eligible_dates, era_for_date, load_era_rules, resolve_start_date  # noqa: E402
from historical_baselines import processed_signal_path, read_csv, transform_rows  # noqa: E402
from mbh_simulator import read_quotes_csv, simulate_day, trades_to_rows  # noqa: E402
from portfolio_metrics import portfolio_stats  # noqa: E402
from profiles import PRODUCTION_TRAIN_COUNT  # noqa: E402
from regime_validation import discover_dates  # noqa: E402
from overnight_calmar_variants import (  # noqa: E402
    ACCOUNT,
    HOLDOUT_START,
    SELECTION_END,
    build_all_variants,
    make_policy,
)

PROCESSED = ROOT / "data" / "processed"
OUT = ROOT / "data" / "overnight_calmar_suite"
TRAIN = PRODUCTION_TRAIN_COUNT
CHECKPOINT_VERSION = 3  # Wave 3 + selection/holdout split; invalidates Wave 2 checkpoints
DEFAULT_SIGNALS = "signals_unconditional.csv"


def load_rolled_signals(processed_dir: Path, symbol: str, train_dates: List[str], test_date: str):
    from mbh_simulator import read_signals_csv  # noqa: E402

    path = processed_signal_path(processed_dir, symbol, test_date, DEFAULT_SIGNALS)
    if path.exists():
        return read_signals_csv(path)
    rows = []
    for d in train_dates:
        p = processed_dir / f"symbol={symbol}" / f"date={d}" / "signals.csv"
        if p.exists():
            rows.extend(read_csv(p))
    rows.extend(read_csv(processed_dir / f"symbol={symbol}" / f"date={test_date}" / "signals.csv"))
    return transform_rows(rows)


def empty_trade_agg() -> dict:
    return {"trades": 0, "wins": 0, "stopped": 0, "total_pnl": 0.0}


def update_trade_agg(agg: dict, trade_rows: List[dict]) -> None:
    for t in trade_rows:
        if t.get("model") == "net_long_overlay":
            continue
        agg["trades"] += 1
        pnl = float(t.get("net_pnl") or 0.0)
        agg["total_pnl"] += pnl
        if pnl > 0:
            agg["wins"] += 1
        if t.get("stopped"):
            agg["stopped"] += 1


def agg_win_rate(agg: dict) -> float:
    n = agg.get("trades", 0)
    return round(agg["wins"] / n, 4) if n else 0.0


def agg_expectancy(agg: dict) -> float:
    n = agg.get("trades", 0)
    return round(agg["total_pnl"] / n, 2) if n else 0.0


def agg_stop_rate(agg: dict) -> float:
    n = agg.get("trades", 0)
    return round(agg["stopped"] / n, 4) if n else 0.0


def shard_bounds(oos_total: int, shard: int, shards: int) -> Tuple[int, int]:
    chunk = (oos_total + shards - 1) // shards
    start = shard * chunk
    end = min(oos_total, start + chunk)
    return start, end


def work_dir(shard: int, shards: int) -> Path:
    if shards <= 1:
        return OUT
    return OUT / f"shard_{shard}"


def save_checkpoint(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)


def load_checkpoint(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def filter_daily(daily: List[dict], *, end: Optional[str] = None, start: Optional[str] = None) -> List[dict]:
    out: List[dict] = []
    for row in daily:
        d = str(row.get("date") or "")
        if end is not None and d > end:
            continue
        if start is not None and d < start:
            continue
        out.append(row)
    return out


def build_summaries(
    variants: list,
    daily_by: Dict[str, List[dict]],
    trade_agg: Dict[str, dict],
    *,
    period: str = "full",
    end: Optional[str] = None,
    start: Optional[str] = None,
) -> List[dict]:
    ref_name = variants[0][1]
    ref_daily = filter_daily(daily_by[ref_name], end=end, start=start)
    ref_stats = portfolio_stats(ref_daily, ACCOUNT, metrics_mode="eligible_only")
    ref_calmar = (
        float(ref_stats.get("cagr_pct") or 0) / max(float(ref_stats.get("max_drawdown_pct") or 1), 0.01)
    )
    rows: List[dict] = []
    for phase, name, _, _ in variants:
        daily = filter_daily(daily_by[name], end=end, start=start)
        port = portfolio_stats(daily, ACCOUNT, metrics_mode="eligible_only")
        agg = trade_agg.get(name, empty_trade_agg())
        max_dd = float(port.get("max_drawdown_pct") or 0)
        cagr = float(port.get("cagr_pct") or 0)
        calmar = round(cagr / max_dd, 4) if max_dd > 0 else 0.0
        # Trade aggs are full-run only; omit misleading period trade stats.
        trade_fields = {}
        if period == "full":
            trade_fields = {
                "spread_win_rate": agg_win_rate(agg),
                "spread_expectancy": agg_expectancy(agg),
                "total_trades": agg.get("trades", 0),
                "stop_rate": agg_stop_rate(agg),
            }
        else:
            trade_fields = {
                "spread_win_rate": None,
                "spread_expectancy": None,
                "total_trades": sum(int(r.get("trades") or 0) for r in daily),
                "stop_rate": None,
            }
        rows.append(
            {
                "period": period,
                "selection_end": SELECTION_END,
                "holdout_start": HOLDOUT_START,
                "phase": phase,
                "variant": name,
                "n_days": len(daily),
                **port,
                "calmar": calmar,
                **trade_fields,
                "cagr_delta_vs_ref": round(cagr - float(ref_stats.get("cagr_pct") or 0), 2),
                "worst_day_delta_vs_ref": round(
                    float(port.get("worst_day_pct") or 0) - float(ref_stats.get("worst_day_pct") or 0), 2
                ),
                "max_dd_delta_vs_ref": round(max_dd - float(ref_stats.get("max_drawdown_pct") or 0), 2),
                "calmar_delta_vs_ref": round(calmar - ref_calmar, 4),
            }
        )
    return rows


def run_suite(
    *,
    shard: int = 0,
    shards: int = 1,
    max_oos: int = 0,
    resume: bool = False,
    checkpoint_every: int = 25,
) -> bool:
    variants = build_all_variants()
    names = [v[1] for v in variants]
    variant_meta = [{"phase": p, "variant": n} for p, n, _, _ in variants]

    floor, eras = load_era_rules(DEFAULT_RULES)
    processed_dates = discover_dates(PROCESSED, "SPXW")
    resolved_start = resolve_start_date(processed_dates, floor, require_mon_and_wed=True)
    eligible = discover_eligible_dates(
        processed_dates, floor=resolved_start, end=processed_dates[-1], eras=eras
    )

    oos_total = len(eligible) - TRAIN
    if max_oos > 0:
        oos_total = min(oos_total, max_oos)
    oos_start, oos_end = shard_bounds(oos_total, shard, shards)
    shard_days = oos_end - oos_start

    out_dir = work_dir(shard, shards)
    out_dir.mkdir(parents=True, exist_ok=True)
    ckpt_path = out_dir / "checkpoint.json"

    daily_by: Dict[str, List[dict]] = {n: [] for n in names}
    trade_agg: Dict[str, dict] = {n: empty_trade_agg() for n in names}
    trailing_stop: Deque[float] = deque(maxlen=5)
    prior_day_pnl = 0.0
    start_oos_offset = oos_start

    if resume:
        ckpt = load_checkpoint(ckpt_path)
        if ckpt and ckpt.get("version") == CHECKPOINT_VERSION:
            if ckpt.get("complete") and int(ckpt.get("oos_total", 0)) == shard_days:
                print(f"Shard {shard}/{shards} already complete.", flush=True)
                return True
            if int(ckpt.get("oos_done", 0)) > 0:
                daily_by = ckpt["daily_by"]
                trade_agg = ckpt["trade_agg"]
                trailing_stop = deque(ckpt.get("trailing_stop", []), maxlen=5)
                prior_day_pnl = float(ckpt.get("prior_day_pnl", 0.0))
                start_oos_offset = oos_start + int(ckpt.get("oos_done", 0))
                print(
                    f"Resume shard {shard}/{shards} at {start_oos_offset - oos_start}/{shard_days}",
                    flush=True,
                )

    print(
        f"Overnight Calmar suite shard {shard}/{shards}: {len(variants)} variants × {shard_days} OOS days",
        flush=True,
    )

    for oos_i in range(start_oos_offset, oos_end):
        index = TRAIN + oos_i
        test_date = eligible[index]
        train_dates = eligible[index - TRAIN : index]
        day_dir = PROCESSED / "symbol=SPXW" / f"date={test_date}"
        quotes = read_quotes_csv(day_dir / "normalized_option_quotes.csv")
        signals = load_rolled_signals(PROCESSED, "SPXW", train_dates, test_date)
        era = era_for_date(datetime.strptime(test_date, "%Y-%m-%d").date(), eras)

        for phase, name, cfg, policy_key in variants:
            policy = make_policy(
                policy_key,
                trailing_stop=trailing_stop,
                prior_day_pnl=prior_day_pnl,
            )
            result = simulate_day(quotes, signals, config=cfg, policy=policy)
            rows = trades_to_rows(result.trades)
            update_trade_agg(trade_agg[name], rows)
            daily_by[name].append(
                {
                    "date": test_date,
                    "eligible": True,
                    "era": era,
                    "trades": len(result.trades),
                    "stopped_trades": sum(1 for t in result.trades if t.stopped),
                    "net_pnl": round(result.net_pnl, 2),
                    "halted": result.halted,
                }
            )

        ref_name = names[0]
        base_day = daily_by[ref_name][-1]
        tr = int(base_day["trades"])
        sr = int(base_day["stopped_trades"]) / tr if tr else 0.0
        trailing_stop.append(sr)
        prior_day_pnl = float(base_day["net_pnl"])

        done_in_shard = oos_i - oos_start + 1
        if done_in_shard % 50 == 0 or oos_i == oos_end - 1:
            print(f"  shard {shard}: {done_in_shard}/{shard_days} ({test_date})", flush=True)

        if checkpoint_every > 0 and (done_in_shard % checkpoint_every == 0 or oos_i == oos_end - 1):
            save_checkpoint(
                ckpt_path,
                {
                    "version": CHECKPOINT_VERSION,
                    "shard": shard,
                    "shards": shards,
                    "oos_done": done_in_shard,
                    "oos_total": shard_days,
                    "last_date": test_date,
                    "complete": oos_i == oos_end - 1,
                    "variant_meta": variant_meta,
                    "daily_by": daily_by,
                    "trade_agg": trade_agg,
                    "trailing_stop": list(trailing_stop),
                    "prior_day_pnl": prior_day_pnl,
                },
            )
            if done_in_shard % checkpoint_every == 0:
                print(f"  checkpoint saved ({done_in_shard}/{shard_days})", flush=True)

    summaries = build_summaries(variants, daily_by, trade_agg)
    (out_dir / "shard_summary.json").write_text(json.dumps(summaries, indent=2), encoding="utf-8")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Overnight Calmar suite (checkpointed).")
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--shards", type=int, default=1)
    parser.add_argument("--max-oos-days", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--checkpoint-every", type=int, default=25)
    args = parser.parse_args()
    t0 = time_mod.time()
    run_suite(
        shard=args.shard,
        shards=args.shards,
        max_oos=args.max_oos_days,
        resume=args.resume,
        checkpoint_every=args.checkpoint_every,
    )
    print(f"Shard {args.shard} done in {(time_mod.time() - t0) / 60:.1f} min", flush=True)


if __name__ == "__main__":
    main()
