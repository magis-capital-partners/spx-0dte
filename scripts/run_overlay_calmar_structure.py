"""Overlay Calmar structure suite: IC wing widths × straddle stops on production verticals.

Vertical substrate: p3_poststop_cooldown_120 + FOMC, IC sleeve DISABLED (overlay added here).

  python scripts/run_overlay_calmar_structure.py --phase GRID --shard 0 --shards 8 --resume
  python scripts/run_overlay_calmar_structure.py --phase P3 --winners-json data/overlay_calmar_structure/winners.json
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
sys.path.insert(0, str(ROOT / "scripts"))

from expiry_calendar import (  # noqa: E402
    DEFAULT_RULES,
    discover_eligible_dates,
    era_for_date,
    load_era_rules,
    resolve_start_date,
)
from mbh_simulator import read_quotes_csv, read_signals_csv, simulate_day  # noqa: E402
from overlay_calmar_variants import (  # noqa: E402
    ACCOUNT,
    HOLDOUT_START,
    SELECTION_END,
    Variant,
    build_phase0,
    build_phase1,
    build_phase1b,
    build_phase2,
    build_phase2b,
    build_phase2c,
    build_phase3,
    build_phase5_salvage,
    build_structure_grid,
    dedupe_variants,
    load_winners,
    variants_by_name,
)
from profiles import (  # noqa: E402
    PRODUCTION_MAX_CONTRACTS_PER_TRANCHE,
    PRODUCTION_SIZING_SCHEME,
    PRODUCTION_TRAIN_COUNT,
    SCHEMES,
    VIX_ELEVATED_SCALE,
    build_p3_poststop_cooldown_config,
)
from regime_validation import apply_rolling_baseline, discover_dates  # noqa: E402
from run_selective_straddle_overlay import (  # noqa: E402
    build_summaries,
    empty_trade_agg,
    load_checkpoint,
    save_checkpoint,
    simulate_overlay,
    update_overlay_agg,
)
from selective_overlay_variants import Structure  # noqa: E402
from vix_daily import DEFAULT_VIX_CSV, load_vix_daily  # noqa: E402
from vix_sizing_policies import build_production_vix_policy  # noqa: E402
from why_not_look_at_variants import load_fomc_dates  # noqa: E402

PROCESSED = ROOT / "data" / "processed"
OUT = ROOT / "data" / "overlay_calmar_structure"
PROD_CACHE = OUT / "prod_cache_verts"
TRAIN = PRODUCTION_TRAIN_COUNT
CHECKPOINT_VERSION = 1


def _prod_cache_path(test_date: str) -> Path:
    return PROD_CACHE / f"{test_date}.json"


def load_prod_day(test_date: str) -> Optional[dict]:
    path = _prod_cache_path(test_date)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_prod_day(test_date: str, payload: dict) -> None:
    PROD_CACHE.mkdir(parents=True, exist_ok=True)
    path = _prod_cache_path(test_date)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)


def out_root(phase: str) -> Path:
    return OUT / phase.lower()


def work_dir(phase: str, shard: int, shards: int) -> Path:
    root = out_root(phase)
    if shards <= 1:
        return root
    return root / f"shard_{shard}"


def shard_bounds(oos_total: int, shard: int, shards: int) -> Tuple[int, int]:
    chunk = (oos_total + shards - 1) // shards
    start = shard * chunk
    end = min(oos_total, start + chunk)
    return start, end


def load_variants(phase: str, winners_json: Optional[Path]) -> List[Variant]:
    phase_u = phase.upper()
    if phase_u in ("GRID", "P0P2", "STRUCTURE"):
        return build_structure_grid()
    if phase_u == "P0":
        return build_phase0()
    if phase_u == "P1":
        return dedupe_variants(build_phase0() + build_phase1())
    if phase_u == "P1B":
        widths = None
        if winners_json and winners_json.is_file():
            w = load_winners(winners_json)
            widths = w.get("p1_top_widths")
        return build_phase1b(widths)
    if phase_u == "P2":
        return build_phase2()
    if phase_u == "P2B":
        stops = None
        if winners_json and winners_json.is_file():
            w = load_winners(winners_json)
            stops = w.get("p2_top_stops")
        return build_phase2b(stops)
    if phase_u == "P2C":
        stops = None
        if winners_json and winners_json.is_file():
            w = load_winners(winners_json)
            stops = w.get("p2_soft_stops") or w.get("p2_top_stops")
        return build_phase2c(stops)
    if phase_u == "P3":
        if not winners_json or not winners_json.is_file():
            raise SystemExit("--winners-json required for P3")
        w = load_winners(winners_json)
        grid = variants_by_name(build_structure_grid())
        ic_structs: List[Structure] = []
        for name in w.get("freeze_ic_variants", []):
            if name in grid:
                ic_structs.append(grid[name].structure)
            else:
                # reconstruct from name IC_w50_d12
                import re

                m = re.match(r"IC_w(\d+)_d(\d+)", name)
                if not m:
                    raise SystemExit(f"Cannot resolve IC winner {name}")
                width = float(m.group(1))
                delta = float(m.group(2)) / 100.0
                ic_structs.append(
                    Structure(f"ic_w{int(width)}_d{int(round(delta*100))}", "ic", target_delta=delta, wing_width=width)
                )
        straddles: List[Variant] = []
        for name in w.get("freeze_straddle_variants", []):
            if name in grid:
                straddles.append(grid[name])
            else:
                raise SystemExit(f"Unknown straddle winner {name}")
        # always include prod ref
        return dedupe_variants(build_phase0()[:1] + build_phase3(ic_structs, straddles))
    if phase_u == "P5":
        if not winners_json or not winners_json.is_file():
            raise SystemExit("--winners-json required for P5")
        w = load_winners(winners_json)
        grid = variants_by_name(build_structure_grid())
        ic_name = w.get("best_ic")
        s_name = w.get("best_straddle")
        if not ic_name or ic_name not in grid:
            raise SystemExit("best_ic missing in winners")
        best_s = grid.get(s_name) if s_name else None
        return dedupe_variants(build_phase0()[:1] + build_phase5_salvage(grid[ic_name].structure, best_s))
    raise SystemExit(f"Unknown phase {phase}")


def run_suite(
    *,
    phase: str,
    shard: int = 0,
    shards: int = 1,
    max_oos: int = 0,
    resume: bool = False,
    checkpoint_every: int = 10,
    winners_json: Optional[Path] = None,
) -> bool:
    variants = load_variants(phase, winners_json)
    names = [v.name for v in variants]

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

    out_dir = work_dir(phase, shard, shards)
    out_dir.mkdir(parents=True, exist_ok=True)
    ckpt_path = out_dir / "checkpoint.json"

    fomc_dates = load_fomc_dates()
    vix_by = load_vix_daily(DEFAULT_VIX_CSV)

    # Verticals only — production IC overlay is tested as an explicit variant here.
    base_cfg = replace(
        build_p3_poststop_cooldown_config(account_equity=ACCOUNT),
        use_condor_sleeve=False,
    )
    tod = SCHEMES[PRODUCTION_SIZING_SCHEME]
    policy = build_production_vix_policy(
        tod, elevated_scale=VIX_ELEVATED_SCALE, max_contracts=PRODUCTION_MAX_CONTRACTS_PER_TRANCHE
    )

    daily_by: Dict[str, List[dict]] = {n: [] for n in names}
    trade_agg: Dict[str, dict] = {n: empty_trade_agg() for n in names}
    start_oos_offset = oos_start

    if resume:
        ckpt = load_checkpoint(ckpt_path)
        if ckpt and ckpt.get("version") == CHECKPOINT_VERSION and ckpt.get("phase") == phase.upper():
            if ckpt.get("complete") and int(ckpt.get("oos_total", 0)) == shard_days:
                print(f"Shard {shard}/{shards} phase {phase} already complete.", flush=True)
                return True
            if int(ckpt.get("oos_done", 0)) > 0 and ckpt.get("variant_names") == names:
                daily_by = ckpt["daily_by"]
                trade_agg = ckpt["trade_agg"]
                start_oos_offset = oos_start + int(ckpt.get("oos_done", 0))
                print(
                    f"Resume shard {shard}/{shards} phase {phase} at "
                    f"{start_oos_offset - oos_start}/{shard_days}",
                    flush=True,
                )

    print(
        f"Overlay Calmar phase={phase} shard={shard}/{shards}: "
        f"{len(variants)} variants × {shard_days} OOS days "
        f"(sel<={SELECTION_END}, hold>={HOLDOUT_START})",
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
        is_fomc = test_date in fomc_dates
        vix_day = vix_by.get(test_date)
        vix_open = float(vix_day.open) if vix_day else None

        cached = load_prod_day(test_date)
        if cached is not None:
            prod_pnl = float(cached["prod_pnl"])
            prod_trades = int(cached["prod_trades"])
            prod_stopped = int(cached["prod_stopped"])
            prod_halted = bool(cached["prod_halted"])
        else:
            result = simulate_day(quotes, signals, config=base_cfg, policy=policy)
            prod_pnl = float(result.net_pnl)
            prod_trades = len(result.trades)
            prod_stopped = sum(1 for t in result.trades if t.stopped)
            prod_halted = bool(result.halted)
            save_prod_day(
                test_date,
                {
                    "prod_pnl": prod_pnl,
                    "prod_trades": prod_trades,
                    "prod_stopped": prod_stopped,
                    "prod_halted": prod_halted,
                },
            )

        for v in variants:
            ov = simulate_overlay(
                quotes,
                signals,
                v,
                vix=vix_open,
                is_fomc=is_fomc,
                vertical_halted=prod_halted,
                vertical_stopped=prod_stopped > 0,
                vertical_trades=prod_trades,
            )
            net = round(prod_pnl + float(ov["overlay_pnl"]), 2)
            update_overlay_agg(trade_agg[v.name], ov)
            daily_by[v.name].append(
                {
                    "date": test_date,
                    "eligible": True,
                    "era": era,
                    "trades": prod_trades + int(ov["overlay_trades"]),
                    "stopped_trades": prod_stopped + int(ov["overlay_stopped"]),
                    "net_pnl": net,
                    "prod_pnl": round(prod_pnl, 2),
                    "overlay_pnl": ov["overlay_pnl"],
                    "overlay_trades": ov["overlay_trades"],
                    "halted": prod_halted,
                    "is_fomc": is_fomc,
                    "vix_open": vix_open,
                    "overlay_skipped": ov.get("overlay_skipped"),
                    "hit_max_loss": ov.get("hit_max_loss"),
                    "credit": ov.get("credit"),
                    "max_loss": ov.get("max_loss"),
                }
            )

        done_in_shard = oos_i - oos_start + 1
        if done_in_shard % 25 == 0 or oos_i == oos_end - 1:
            print(f"  shard {shard}: {done_in_shard}/{shard_days} ({test_date})", flush=True)
        if checkpoint_every > 0 and (done_in_shard % checkpoint_every == 0 or oos_i == oos_end - 1):
            save_checkpoint(
                ckpt_path,
                {
                    "version": CHECKPOINT_VERSION,
                    "phase": phase.upper(),
                    "shard": shard,
                    "shards": shards,
                    "oos_done": done_in_shard,
                    "oos_total": shard_days,
                    "last_date": test_date,
                    "complete": oos_i == oos_end - 1,
                    "variant_names": names,
                    "daily_by": daily_by,
                    "trade_agg": trade_agg,
                    "selection_end": SELECTION_END,
                    "holdout_start": HOLDOUT_START,
                },
            )

    # patch build_summaries ref to use P0_prod if present
    summaries = build_summaries(variants, daily_by, trade_agg)
    # rebuild with P0_prod as ref when available
    if "P0_prod" in daily_by:
        from portfolio_metrics import portfolio_stats  # noqa: E402
        from run_selective_straddle_overlay import filter_daily  # noqa: E402

        ref_daily = filter_daily(daily_by["P0_prod"])
        ref_stats = portfolio_stats(ref_daily, ACCOUNT, metrics_mode="eligible_only")
        ref_calmar = float(ref_stats.get("cagr_pct") or 0) / max(
            float(ref_stats.get("max_drawdown_pct") or 1), 0.01
        )
        for row in summaries:
            port_cagr = float(row.get("cagr_pct") or 0)
            port_dd = float(row.get("max_drawdown_pct") or 0)
            port_worst = float(row.get("worst_day_pct") or 0)
            calmar = float(row.get("calmar") or 0)
            row["cagr_delta_vs_ref"] = round(port_cagr - float(ref_stats.get("cagr_pct") or 0), 2)
            row["worst_day_delta_vs_ref"] = round(
                port_worst - float(ref_stats.get("worst_day_pct") or 0), 2
            )
            row["max_dd_delta_vs_ref"] = round(port_dd - float(ref_stats.get("max_drawdown_pct") or 0), 2)
            row["calmar_delta_vs_ref"] = round(calmar - ref_calmar, 4)
            row["ref_variant"] = "P0_prod"
    (out_dir / "shard_summary.json").write_text(json.dumps(summaries, indent=2), encoding="utf-8")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Overlay Calmar structure suite")
    parser.add_argument(
        "--phase",
        required=True,
        help="GRID | P0 | P1 | P1b | P2 | P2b | P2c | P3 | P5",
    )
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--shards", type=int, default=1)
    parser.add_argument("--max-oos-days", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--checkpoint-every", type=int, default=10)
    parser.add_argument("--winners-json", type=Path, default=None)
    args = parser.parse_args()
    t0 = time_mod.time()
    run_suite(
        phase=args.phase,
        shard=args.shard,
        shards=args.shards,
        max_oos=args.max_oos_days,
        resume=args.resume,
        checkpoint_every=args.checkpoint_every,
        winners_json=args.winners_json,
    )
    print(
        f"Done phase={args.phase} shard={args.shard} in {(time_mod.time() - t0) / 60:.1f} min",
        flush=True,
    )


if __name__ == "__main__":
    main()
