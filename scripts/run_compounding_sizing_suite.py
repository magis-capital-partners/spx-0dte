"""Compounding sizing suite: sequential equity-proportional production path.

Shards by *variant* (path-dependent — cannot shard by date).

  python scripts/run_compounding_sizing_suite.py --shard 0 --shards 8 --resume
  python scripts/merge_compounding_sizing_shards.py --shards 8
  python scripts/summarize_compounding_sizing.py
"""
from __future__ import annotations

import argparse
import json
import sys
import time as time_mod
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
SIM = ROOT / "simulator"
sys.path.insert(0, str(SIM))

from compounding_sizing import (  # noqa: E402
    build_variants,
    scaled_day_config,
    scaled_day_policy,
    shard_variant_names,
)
from expiry_calendar import (  # noqa: E402
    DEFAULT_RULES,
    discover_eligible_dates,
    era_for_date,
    load_era_rules,
    resolve_start_date,
)
from mbh_simulator import (  # noqa: E402
    read_quotes_csv,
    read_signals_csv,
    simulate_day,
    stop_diagnostics_to_rows,
    trades_to_rows,
)
from historical_baselines import write_csv  # noqa: E402
from portfolio_metrics import portfolio_stats  # noqa: E402
from profiles import PRODUCTION_ACCOUNT_EQUITY, PRODUCTION_TRAIN_COUNT  # noqa: E402
from regime_validation import apply_rolling_baseline, discover_dates  # noqa: E402
from unconditional_baseline import trade_stats  # noqa: E402

PROCESSED = ROOT / "data" / "processed"
OUT = ROOT / "data" / "compounding_sizing"
DASHBOARD_DIR = ROOT / "data" / "dashboard_runs" / "p3_poststop_compounding_f1"
ACCOUNT = PRODUCTION_ACCOUNT_EQUITY
TRAIN = PRODUCTION_TRAIN_COUNT
CHECKPOINT_VERSION = 1
SELECTION_END = "2023-12-29"
HOLDOUT_START = "2024-01-02"


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


def run_variant(
    name: str,
    *,
    eligible: List[str],
    eras,
    keep_trades: bool,
    signals_filename: str = "signals_unconditional.csv",
    resume_daily: Optional[List[dict]] = None,
    resume_equity: Optional[float] = None,
    resume_trades: Optional[List[dict]] = None,
    resume_stops: Optional[List[dict]] = None,
    checkpoint_path: Optional[Path] = None,
    checkpoint_every: int = 10,
    shard_meta: Optional[dict] = None,
) -> dict:
    variants = build_variants(ACCOUNT)
    variant = variants[name]
    # Fresh k_of closure state for this run.
    k_of = build_variants(ACCOUNT)[name].k_of

    start_i = TRAIN
    daily: List[dict] = list(resume_daily or [])
    all_trades: List[dict] = list(resume_trades or [])
    stop_rows: List[dict] = list(resume_stops or [])
    equity = float(resume_equity if resume_equity is not None else ACCOUNT)

    if daily:
        last = str(daily[-1]["date"])
        if last in eligible:
            start_i = eligible.index(last) + 1
            print(f"  [{name}] resume after {last} equity=${equity:,.0f}", flush=True)

    oos_total = len(eligible) - TRAIN
    for index in range(start_i, len(eligible)):
        test_date = eligible[index]
        day_index = index - TRAIN
        train_dates = eligible[index - TRAIN : index]
        day_dir = PROCESSED / "symbol=SPXW" / f"date={test_date}"

        k = k_of(equity, day_index)
        cfg = scaled_day_config(k)
        policy = scaled_day_policy(k)

        quotes = read_quotes_csv(day_dir / "normalized_option_quotes.csv")
        # Per-shard filename avoids cross-process races on signals_unconditional.csv
        # when sharding by variant (all shards touch the same dates).
        apply_rolling_baseline(PROCESSED, "SPXW", train_dates, test_date, signals_filename)
        signals = read_signals_csv(day_dir / signals_filename)
        result = simulate_day(quotes, signals, config=cfg, policy=policy)

        day = datetime.strptime(test_date, "%Y-%m-%d").date()
        era = era_for_date(day, eras)
        net = float(result.net_pnl)
        contracts_sold = sum(int(t.contracts) for t in result.trades)
        daily.append(
            {
                "date": test_date,
                "weekday": day.strftime("%a"),
                "era": era,
                "eligible": True,
                "traded": len(result.trades) > 0,
                "trades": len(result.trades),
                "contracts_sold": contracts_sold,
                "stopped_trades": sum(1 for t in result.trades if t.stopped),
                "net_pnl": round(net, 2),
                "halted": bool(result.halted),
                "equity_open": round(equity, 2),
                "k": round(k, 6),
                "baseline_contracts": int(cfg.baseline_contracts),
                "account_equity": round(cfg.account_equity, 2),
            }
        )

        if keep_trades:
            for row in trades_to_rows(result.trades):
                row["date"] = test_date
                all_trades.append(row)
            for row in stop_diagnostics_to_rows(result.trades):
                row["date"] = test_date
                stop_rows.append(row)

        equity += net

        done = index - TRAIN + 1
        if done % 50 == 0 or done == oos_total:
            print(
                f"  [{name}] {done}/{oos_total} ({test_date}) "
                f"k={k:.2f} equity=${equity:,.0f}",
                flush=True,
            )

        if checkpoint_path and checkpoint_every > 0 and (
            done % checkpoint_every == 0 or done == oos_total
        ):
            payload = {
                "version": CHECKPOINT_VERSION,
                "complete": done == oos_total,
                "variant": name,
                "oos_done": done,
                "oos_total": oos_total,
                "last_date": test_date,
                "equity": equity,
                "daily": daily,
                "selection_end": SELECTION_END,
                "holdout_start": HOLDOUT_START,
                **(shard_meta or {}),
            }
            if keep_trades:
                payload["trades"] = all_trades
                payload["stops"] = stop_rows
            save_checkpoint(checkpoint_path, payload)

    return {
        "variant": name,
        "label": variant.label,
        "daily": daily,
        "trades": all_trades,
        "stops": stop_rows,
        "ending_equity": equity,
        "export_dashboard": variant.export_dashboard,
    }


def export_dashboard(result: dict, eligible_meta: dict) -> None:
    """Write dashboard_runs/p3_poststop_compounding_f1/ in export_dashboard_run shape."""
    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    daily = result["daily"]
    trades = result["trades"]
    stops = result["stops"]
    write_csv(DASHBOARD_DIR / "daily_summary.csv", daily)
    write_csv(DASHBOARD_DIR / "trades.csv", trades)
    write_csv(DASHBOARD_DIR / "stop_diagnostics.csv", stops)

    spread = [r for r in trades if r.get("model") != "net_long_overlay"]
    ts = trade_stats(spread)
    headline = portfolio_stats(daily, ACCOUNT, metrics_mode="eligible_only")
    # Correct worst-day % for compounding (vs grown equity, not E0).
    eo_worst = [
        float(r["net_pnl"]) / float(r["equity_open"]) * 100.0
        for r in daily
        if float(r.get("equity_open") or 0) > 0
    ]
    if eo_worst:
        headline = {**headline, "worst_day_pct": round(min(eo_worst), 2)}
    peak_k = max((float(r.get("k") or 1) for r in daily), default=1.0)
    summary = {
        "generated_at": datetime.now().isoformat(),
        "preset": "p3_poststop_compounding_f1",
        "start_date": eligible_meta.get("start_date", ""),
        "end_date": eligible_meta.get("end_date", ""),
        "config_overrides": {
            "compounding": "full_f1",
            "k_policy": "equity_open / E0",
            "scales": ["account_equity", "baseline_contracts", "max_contracts_per_tranche"],
        },
        "sizing_scheme": "linear_decay_downsize",
        "incremental": False,
        "vix_policy": {
            "skip_above": 35.0,
            "elevated_band": [25.0, 35.0],
            "elevated_scale": 1.25,
            "max_contracts_per_tranche": "48 * k (scaled)",
        },
        "compounding": {
            "mode": "full",
            "e0": ACCOUNT,
            "peak_k": round(peak_k, 4),
            "ending_equity": round(float(result["ending_equity"]), 2),
        },
        "eligible_dates": eligible_meta.get("eligible_dates", 0),
        "oos_eligible_days": len(daily),
        "first_oos_date": daily[0]["date"] if daily else "",
        "last_oos_date": daily[-1]["date"] if daily else "",
        "headline": headline,
        "trade_stats": ts,
    }
    (DASHBOARD_DIR / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"Dashboard export -> {DASHBOARD_DIR}", flush=True)
    print(
        f"  CAGR {headline['cagr_pct']:.1f}%  Sharpe {headline['sharpe']:.2f}  "
        f"maxDD {headline['max_drawdown_pct']:.1f}%  peak_k {peak_k:.2f}x",
        flush=True,
    )


def run_suite(
    *,
    shard: int = 0,
    shards: int = 1,
    max_oos: int = 0,
    resume: bool = False,
    checkpoint_every: int = 10,
    variants_filter: Optional[List[str]] = None,
) -> None:
    all_variants = build_variants(ACCOUNT)
    names = list(all_variants.keys())
    if variants_filter:
        names = [n for n in names if n in set(variants_filter)]
    mine = shard_variant_names(names, shard, shards)

    floor, eras = load_era_rules(DEFAULT_RULES)
    processed_dates = discover_dates(PROCESSED, "SPXW")
    resolved_start = resolve_start_date(processed_dates, floor, require_mon_and_wed=True)
    eligible = discover_eligible_dates(
        processed_dates, floor=resolved_start, end=processed_dates[-1], eras=eras
    )
    if max_oos > 0:
        eligible = eligible[: TRAIN + max_oos]

    out_dir = work_dir(shard, shards)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(
        f"Compounding suite shard={shard}/{shards}: variants={mine} "
        f"OOS={len(eligible) - TRAIN}",
        flush=True,
    )

    eligible_meta = {
        "start_date": resolved_start,
        "end_date": eligible[-1] if eligible else "",
        "eligible_dates": len(eligible),
    }

    completed: Dict[str, dict] = {}
    for name in mine:
        ckpt_path = out_dir / f"checkpoint_{name}.json"
        resume_daily = None
        resume_equity = None
        resume_trades = None
        resume_stops = None
        if resume:
            ckpt = load_checkpoint(ckpt_path)
            if ckpt and ckpt.get("version") == CHECKPOINT_VERSION and ckpt.get("variant") == name:
                if ckpt.get("complete") and int(ckpt.get("oos_total", 0)) == len(eligible) - TRAIN:
                    print(f"  [{name}] already complete", flush=True)
                    completed[name] = {
                        "variant": name,
                        "label": all_variants[name].label,
                        "daily": ckpt["daily"],
                        "trades": ckpt.get("trades") or [],
                        "stops": ckpt.get("stops") or [],
                        "ending_equity": float(ckpt.get("equity") or ACCOUNT),
                        "export_dashboard": all_variants[name].export_dashboard,
                    }
                    if all_variants[name].export_dashboard and completed[name]["trades"]:
                        export_dashboard(completed[name], eligible_meta)
                    continue
                resume_daily = ckpt.get("daily")
                resume_equity = ckpt.get("equity")
                resume_trades = ckpt.get("trades")
                resume_stops = ckpt.get("stops")

        keep_trades = all_variants[name].export_dashboard
        signals_filename = f"signals_unconditional_compound_s{shard}.csv"
        result = run_variant(
            name,
            eligible=eligible,
            eras=eras,
            keep_trades=keep_trades,
            signals_filename=signals_filename,
            resume_daily=resume_daily,
            resume_equity=resume_equity,
            resume_trades=resume_trades,
            resume_stops=resume_stops,
            checkpoint_path=ckpt_path,
            checkpoint_every=checkpoint_every,
            shard_meta={"shard": shard, "shards": shards},
        )
        completed[name] = result
        # Persist final daily CSV per variant in shard dir.
        write_csv(out_dir / f"daily_{name}.csv", result["daily"])
        if result["export_dashboard"]:
            export_dashboard(result, eligible_meta)

    # Shard-level manifest for merge.
    save_checkpoint(
        out_dir / "checkpoint.json",
        {
            "version": CHECKPOINT_VERSION,
            "shard": shard,
            "shards": shards,
            "complete": True,
            "variants": list(completed.keys()),
            "oos_total": len(eligible) - TRAIN,
            "daily_by": {n: completed[n]["daily"] for n in completed},
            "ending_equity_by": {n: completed[n]["ending_equity"] for n in completed},
            "selection_end": SELECTION_END,
            "holdout_start": HOLDOUT_START,
            "eligible_meta": eligible_meta,
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Compounding sizing suite")
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--shards", type=int, default=1)
    parser.add_argument("--max-oos-days", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--checkpoint-every", type=int, default=10)
    parser.add_argument(
        "--variants",
        default="",
        help="Comma-separated subset (default: all).",
    )
    args = parser.parse_args()
    filt = [v.strip() for v in args.variants.split(",") if v.strip()] or None
    t0 = time_mod.time()
    run_suite(
        shard=args.shard,
        shards=args.shards,
        max_oos=args.max_oos_days,
        resume=args.resume,
        checkpoint_every=args.checkpoint_every,
        variants_filter=filt,
    )
    print(f"Done shard={args.shard} in {(time_mod.time() - t0) / 60:.1f} min", flush=True)


if __name__ == "__main__":
    main()
