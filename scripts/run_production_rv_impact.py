"""Re-run productionized promo with real RV feature; optional RV gate sensitivity.

Variants:
  - promo_fomc_vixwing: current production (FOMC 13:30 + VIX>=20 put+25), RV gates still off (99)
  - promo_plus_rv_gate_1_5: same + candidate_max_abs_realized_z=1.5 / hard skip 1.75

Always recomputes signals_unconditional from refreshed signals.csv (walk-forward).

  python scripts/run_production_rv_impact.py --resume --checkpoint-every 25
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time as time_mod
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

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
from mbh_simulator import read_quotes_csv, read_signals_csv, simulate_day, trades_to_rows  # noqa: E402
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
OUT = ROOT / "data" / "production_rv_impact"
ACCOUNT = PRODUCTION_ACCOUNT_EQUITY
TRAIN = PRODUCTION_TRAIN_COUNT
SELECTION_END = "2023-12-29"
HOLDOUT_START = "2024-01-02"


def empty_agg() -> dict:
    return {"trades": 0, "wins": 0, "stopped": 0, "total_pnl": 0.0}


def update_agg(agg: dict, rows: List[dict]) -> None:
    for t in rows:
        if t.get("model") == "net_long_overlay":
            continue
        agg["trades"] += 1
        pnl = float(t.get("net_pnl") or 0)
        agg["total_pnl"] += pnl
        if pnl > 0:
            agg["wins"] += 1
        if t.get("stopped"):
            agg["stopped"] += 1


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


def build_variants():
    promo = build_p3_poststop_cooldown_config(account_equity=ACCOUNT)
    return {
        "promo_fomc_vixwing": promo,
        "promo_plus_rv_gate_1_5": replace(
            promo,
            candidate_max_abs_realized_z=1.5,
            hard_realized_skip_threshold=1.75,
            realized_extreme_threshold=1.5,
        ),
    }


def summarize(name, daily, agg, ref_daily, ref_agg, period):
    port = portfolio_stats(daily, ACCOUNT, metrics_mode="eligible_only")
    ref = portfolio_stats(ref_daily, ACCOUNT, metrics_mode="eligible_only")
    max_dd = float(port.get("max_drawdown_pct") or 0)
    cagr = float(port.get("cagr_pct") or 0)
    calmar = round(cagr / max_dd, 4) if max_dd > 0 else 0.0
    ref_cagr = float(ref.get("cagr_pct") or 0)
    ref_dd = float(ref.get("max_drawdown_pct") or 0)
    ref_calmar = round(ref_cagr / ref_dd, 4) if ref_dd > 0 else 0.0
    trades = agg["trades"] if period == "full" else sum(int(r.get("trades") or 0) for r in daily)
    ref_trades = (
        ref_agg["trades"] if period == "full" else sum(int(r.get("trades") or 0) for r in ref_daily)
    )
    n = agg["trades"]
    return {
        "period": period,
        "variant": name,
        "n_days": len(daily),
        **port,
        "calmar": calmar,
        "total_trades": trades,
        "win_rate": round(agg["wins"] / n, 4) if n and period == "full" else None,
        "stop_rate": round(agg["stopped"] / n, 4) if n and period == "full" else None,
        "cagr_delta_vs_promo": round(cagr - ref_cagr, 2),
        "calmar_delta_vs_promo": round(calmar - ref_calmar, 4),
        "worst_day_delta_vs_promo": round(
            float(port.get("worst_day_pct") or 0) - float(ref.get("worst_day_pct") or 0), 2
        ),
        "trade_delta_vs_promo": trades - ref_trades,
    }


def save_ckpt(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)


def load_ckpt(path: Path):
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def run(*, resume: bool = False, checkpoint_every: int = 25, max_oos: int = 0) -> None:
    variants = build_variants()
    names = list(variants)
    policy = build_production_vix_policy(
        SCHEMES[PRODUCTION_SIZING_SCHEME],
        elevated_scale=VIX_ELEVATED_SCALE,
        max_contracts=PRODUCTION_MAX_CONTRACTS_PER_TRANCHE,
    )
    floor, eras = load_era_rules(DEFAULT_RULES)
    processed_dates = discover_dates(PROCESSED, "SPXW")
    resolved_start = resolve_start_date(processed_dates, floor, require_mon_and_wed=True)
    eligible = discover_eligible_dates(
        processed_dates, floor=resolved_start, end=processed_dates[-1], eras=eras
    )
    oos_total = len(eligible) - TRAIN
    if max_oos > 0:
        oos_total = min(oos_total, max_oos)

    OUT.mkdir(parents=True, exist_ok=True)
    ckpt_path = OUT / "checkpoint.json"
    daily_by: Dict[str, List[dict]] = {n: [] for n in names}
    trade_agg = {n: empty_agg() for n in names}
    start = 0
    if resume:
        ckpt = load_ckpt(ckpt_path)
        if ckpt and ckpt.get("complete") and int(ckpt.get("oos_total", 0)) == oos_total:
            print("Already complete.", flush=True)
            _finalize(daily_by_from=ckpt["daily_by"], trade_agg_from=ckpt["trade_agg"], variants=variants)
            return
        if ckpt and int(ckpt.get("oos_done", 0)) > 0:
            daily_by = ckpt["daily_by"]
            trade_agg = ckpt["trade_agg"]
            start = int(ckpt["oos_done"])
            print(f"Resume at {start}/{oos_total}", flush=True)

    print(f"Production RV impact: {names} x {oos_total} OOS (re-zscoring each day)", flush=True)
    t0 = time_mod.time()
    for oos_i in range(start, oos_total):
        index = TRAIN + oos_i
        test_date = eligible[index]
        train_dates = eligible[index - TRAIN : index]
        apply_rolling_baseline(PROCESSED, "SPXW", train_dates, test_date, "signals_unconditional.csv")
        day_dir = PROCESSED / "symbol=SPXW" / f"date={test_date}"
        quotes = read_quotes_csv(day_dir / "normalized_option_quotes.csv")
        signals = read_signals_csv(day_dir / "signals_unconditional.csv")
        era = era_for_date(datetime.strptime(test_date, "%Y-%m-%d").date(), eras)
        for name, cfg in variants.items():
            result = simulate_day(quotes, signals, config=cfg, policy=policy)
            rows = trades_to_rows(result.trades)
            update_agg(trade_agg[name], rows)
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
        done = oos_i + 1
        if done % 50 == 0 or done == oos_total:
            print(f"  {done}/{oos_total} ({test_date})", flush=True)
        if checkpoint_every > 0 and (done % checkpoint_every == 0 or done == oos_total):
            save_ckpt(
                ckpt_path,
                {
                    "oos_done": done,
                    "oos_total": oos_total,
                    "complete": done == oos_total,
                    "last_date": test_date,
                    "daily_by": daily_by,
                    "trade_agg": trade_agg,
                },
            )
    _finalize(daily_by_from=daily_by, trade_agg_from=trade_agg, variants=variants)
    print(f"Done in {(time_mod.time() - t0) / 60:.1f} min", flush=True)


def _finalize(*, daily_by_from, trade_agg_from, variants) -> None:
    ref = "promo_fomc_vixwing"
    rows = []
    for period, end, start in (
        ("selection", SELECTION_END, None),
        ("holdout", None, HOLDOUT_START),
        ("full", None, None),
    ):
        ref_daily = filter_daily(daily_by_from[ref], end=end, start=start)
        ref_agg = trade_agg_from[ref]
        for name in variants:
            daily = filter_daily(daily_by_from[name], end=end, start=start)
            agg = trade_agg_from[name] if period == "full" else empty_agg()
            rows.append(summarize(name, daily, agg, ref_daily, ref_agg, period))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "summary.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    with (OUT / "summary.csv").open("w", newline="", encoding="utf-8") as handle:
        w = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    # Compare to prior promo run without real RV (if available)
    prior_path = ROOT / "data" / "promo_fomc_vix_wing" / "summary.json"
    prior_note = ""
    if prior_path.exists():
        prior = json.loads(prior_path.read_text(encoding="utf-8"))
        prior_full = next(
            (r for r in prior if r.get("period") == "full" and "promo" in r.get("variant", "")),
            None,
        )
        cur_full = next(r for r in rows if r["period"] == "full" and r["variant"] == ref)
        if prior_full:
            prior_note = (
                f"- vs prior promo (pre-RV feature backfill) full CAGR {prior_full.get('cagr_pct')} -> "
                f"{cur_full.get('cagr_pct')}, trades {prior_full.get('total_trades')} -> {cur_full.get('total_trades')}"
            )

    lines = [
        "# Production RV impact",
        "",
        "Productionized book = FOMC no entries after 13:30 + put wing +25 when VIX>=20.",
        "Note: production still has realized gates at 99 (off) unless using promo_plus_rv_gate_1_5.",
        "",
        "| Period | Variant | CAGR | Calmar | MaxDD | Worst | Trades | dCAGR vs promo | dTrades |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        lines.append(
            f"| {r['period']} | {r['variant']} | {r.get('cagr_pct')} | {r.get('calmar')} | "
            f"{r.get('max_drawdown_pct')} | {r.get('worst_day_pct')} | {r.get('total_trades')} | "
            f"{r.get('cagr_delta_vs_promo')} | {r.get('trade_delta_vs_promo')} |"
        )
    lines.extend(["", "## Notes", prior_note or "- no prior promo summary found"])
    (OUT / "SUMMARY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--checkpoint-every", type=int, default=25)
    parser.add_argument("--max-oos-days", type=int, default=0)
    args = parser.parse_args()
    run(resume=args.resume, checkpoint_every=args.checkpoint_every, max_oos=args.max_oos_days)


if __name__ == "__main__":
    main()
