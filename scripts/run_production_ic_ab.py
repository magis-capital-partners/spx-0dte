"""Production-path IC A/B: wing width × short delta via select_condor_entries.

Uses real ``build_p3_poststop_cooldown_config`` + ``simulate_day`` (not the
selective-overlay picker). Rank on selection Calmar; sealed holdout.

  python scripts/run_production_ic_ab.py --shard 0 --shards 8 --resume
  python scripts/merge_production_ic_ab_shards.py --shards 8
  python scripts/summarize_production_ic_ab.py
"""
from __future__ import annotations

import argparse
import json
import sys
import time as time_mod
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
SIM = ROOT / "simulator"
sys.path.insert(0, str(SIM))

from expiry_calendar import (  # noqa: E402
    DEFAULT_RULES,
    discover_eligible_dates,
    era_for_date,
    load_era_rules,
    resolve_start_date,
)
from mbh_simulator import read_quotes_csv, read_signals_csv, simulate_day  # noqa: E402
from portfolio_metrics import portfolio_stats  # noqa: E402
from profiles import (  # noqa: E402
    PRODUCTION_ACCOUNT_EQUITY,
    PRODUCTION_MAX_CONTRACTS_PER_TRANCHE,
    PRODUCTION_SIZING_SCHEME,
    PRODUCTION_TRAIN_COUNT,
    SCHEMES,
    VIX_ELEVATED_SCALE,
    build_p3_poststop_cooldown_config,
)
from regime_validation import apply_rolling_baseline, discover_dates  # noqa: E402
from vix_sizing_policies import build_production_vix_policy  # noqa: E402

PROCESSED = ROOT / "data" / "processed"
OUT = ROOT / "data" / "production_ic_ab"
ACCOUNT = PRODUCTION_ACCOUNT_EQUITY
TRAIN = PRODUCTION_TRAIN_COUNT
SELECTION_END = "2023-12-29"
HOLDOUT_START = "2024-01-02"
CHECKPOINT_VERSION = 1


def _ic_cfg(wing: float, delta: float):
    base = build_p3_poststop_cooldown_config(account_equity=ACCOUNT)
    return replace(
        base,
        use_condor_sleeve=True,
        condor_wing_width=float(wing),
        condor_target_abs_delta=float(delta),
        condor_min_abs_delta=float(delta) - 0.04,
        condor_max_abs_delta=float(delta) + 0.04,
    )


def build_variants() -> Dict[str, object]:
    """Production-path IC A/B grid (conservative → stretch)."""
    no_ic = replace(build_p3_poststop_cooldown_config(account_equity=ACCOUNT), use_condor_sleeve=False)
    return {
        "no_ic": no_ic,
        "ic_w50_d12": _ic_cfg(50.0, 0.12),  # current production
        "ic_w75_d12": _ic_cfg(75.0, 0.12),
        "ic_w100_d12": _ic_cfg(100.0, 0.12),
        "ic_w150_d12": _ic_cfg(150.0, 0.12),
        "ic_w50_d16": _ic_cfg(50.0, 0.16),
        "ic_w75_d16": _ic_cfg(75.0, 0.16),
        "ic_w100_d16": _ic_cfg(100.0, 0.16),
        "ic_w150_d16": _ic_cfg(150.0, 0.16),
    }


def work_dir(shard: int, shards: int) -> Path:
    if shards <= 1:
        return OUT
    return OUT / f"shard_{shard}"


def shard_bounds(oos_total: int, shard: int, shards: int) -> Tuple[int, int]:
    chunk = (oos_total + shards - 1) // shards
    start = shard * chunk
    end = min(oos_total, start + chunk)
    return start, end


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
    out = []
    for row in daily:
        d = str(row.get("date") or "")
        if end is not None and d > end:
            continue
        if start is not None and d < start:
            continue
        out.append(row)
    return out


def summarize_variant(name: str, daily: List[dict], period: str) -> dict:
    port = portfolio_stats(daily, ACCOUNT, metrics_mode="eligible_only")
    max_dd = float(port.get("max_drawdown_pct") or 0)
    cagr = float(port.get("cagr_pct") or 0)
    calmar = round(cagr / max_dd, 4) if max_dd > 0 else 0.0
    ic_pnl = sum(float(r.get("ic_pnl") or 0) for r in daily)
    ic_days = sum(1 for r in daily if int(r.get("ic_trades") or 0) > 0)
    return {
        "period": period,
        "variant": name,
        "n_days": len(daily),
        **port,
        "calmar": calmar,
        "ic_pnl": round(ic_pnl, 2),
        "ic_days": ic_days,
    }


def run_suite(
    *,
    shard: int = 0,
    shards: int = 1,
    max_oos: int = 0,
    resume: bool = False,
    checkpoint_every: int = 10,
) -> None:
    variants = build_variants()
    names = list(variants.keys())

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

    tod = SCHEMES[PRODUCTION_SIZING_SCHEME]
    policy = build_production_vix_policy(
        tod, elevated_scale=VIX_ELEVATED_SCALE, max_contracts=PRODUCTION_MAX_CONTRACTS_PER_TRANCHE
    )

    daily_by: Dict[str, List[dict]] = {n: [] for n in names}
    start_oos_offset = oos_start

    if resume:
        ckpt = load_checkpoint(ckpt_path)
        if ckpt and ckpt.get("version") == CHECKPOINT_VERSION:
            if ckpt.get("complete") and int(ckpt.get("oos_total", 0)) == shard_days:
                if ckpt.get("variant_names") == names:
                    print(f"Shard {shard}/{shards} already complete.", flush=True)
                    return
            if (
                int(ckpt.get("oos_done", 0)) > 0
                and ckpt.get("variant_names") == names
                and int(ckpt.get("oos_total", 0)) == shard_days
            ):
                daily_by = ckpt["daily_by"]
                start_oos_offset = oos_start + int(ckpt["oos_done"])
                print(
                    f"Resume shard {shard}/{shards} at {start_oos_offset - oos_start}/{shard_days}",
                    flush=True,
                )

    print(
        f"Production IC A/B shard={shard}/{shards}: {len(names)} variants × {shard_days} OOS days",
        flush=True,
    )

    for oos_i in range(start_oos_offset, oos_end):
        index = TRAIN + oos_i
        test_date = eligible[index]
        train_dates = eligible[index - TRAIN : index]
        day_dir = PROCESSED / "symbol=SPXW" / f"date={test_date}"
        quotes = read_quotes_csv(day_dir / "normalized_option_quotes.csv")
        apply_rolling_baseline(PROCESSED, "SPXW", train_dates, test_date, "signals_unconditional.csv")
        signals = read_signals_csv(day_dir / "signals_unconditional.csv")
        era = era_for_date(datetime.strptime(test_date, "%Y-%m-%d").date(), eras)

        for name, cfg in variants.items():
            result = simulate_day(quotes, signals, config=cfg, policy=policy)
            ic_trades = [t for t in result.trades if t.model == "candidate_condor"]
            ic_pnl = sum(float(t.net_pnl) for t in ic_trades)
            daily_by[name].append(
                {
                    "date": test_date,
                    "eligible": True,
                    "era": era,
                    "trades": len(result.trades),
                    "stopped_trades": sum(1 for t in result.trades if t.stopped),
                    "net_pnl": float(result.net_pnl),
                    "ic_pnl": round(ic_pnl, 2),
                    "ic_trades": len(ic_trades),
                    "halted": bool(result.halted),
                }
            )

        done = oos_i - oos_start + 1
        if done % 25 == 0 or oos_i == oos_end - 1:
            print(f"  shard {shard}: {done}/{shard_days} ({test_date})", flush=True)
        if checkpoint_every > 0 and (done % checkpoint_every == 0 or oos_i == oos_end - 1):
            save_checkpoint(
                ckpt_path,
                {
                    "version": CHECKPOINT_VERSION,
                    "shard": shard,
                    "shards": shards,
                    "oos_done": done,
                    "oos_total": shard_days,
                    "last_date": test_date,
                    "complete": oos_i == oos_end - 1,
                    "variant_names": names,
                    "daily_by": daily_by,
                    "selection_end": SELECTION_END,
                    "holdout_start": HOLDOUT_START,
                },
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Production-path IC wing/delta A/B")
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--shards", type=int, default=1)
    parser.add_argument("--max-oos-days", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--checkpoint-every", type=int, default=10)
    args = parser.parse_args()
    t0 = time_mod.time()
    run_suite(
        shard=args.shard,
        shards=args.shards,
        max_oos=args.max_oos_days,
        resume=args.resume,
        checkpoint_every=args.checkpoint_every,
    )
    print(f"Done shard={args.shard} in {(time_mod.time() - t0) / 60:.1f} min", flush=True)


if __name__ == "__main__":
    main()
