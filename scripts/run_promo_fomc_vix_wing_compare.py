"""Compare pre-WNLA production vs FOMC 13:30 + VIX>=20 put-wing+25.

Runs both variants in one pass (quotes loaded once per day), with checkpoints
and selection/holdout summaries.

  python scripts/run_promo_fomc_vix_wing_compare.py --resume --checkpoint-every 25
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
from historical_baselines import processed_signal_path, read_csv, transform_rows  # noqa: E402
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
from regime_validation import discover_dates  # noqa: E402
from vix_sizing_policies import build_production_vix_policy  # noqa: E402

PROCESSED = ROOT / "data" / "processed"
OUT = ROOT / "data" / "promo_fomc_vix_wing"
ACCOUNT = PRODUCTION_ACCOUNT_EQUITY
TRAIN = PRODUCTION_TRAIN_COUNT
SELECTION_END = "2023-12-29"
HOLDOUT_START = "2024-01-02"
CHECKPOINT_VERSION = 1
DEFAULT_SIGNALS = "signals_unconditional.csv"


def load_rolled_signals(processed_dir: Path, symbol: str, train_dates: List[str], test_date: str):
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


def build_variants() -> Dict[str, object]:
    """baseline = production before WNLA promos; promo = current production profile."""
    promo = build_p3_poststop_cooldown_config(account_equity=ACCOUNT)
    baseline = replace(
        promo,
        vix_widen_put_wing_above=0.0,
        vix_widen_put_wing_extra=25.0,
        use_fomc_entry_cutoff=False,
    )
    return {
        "baseline_pre_wnla": baseline,
        "promo_fomc1330_vix20_put175": promo,
    }


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


def summarize_period(
    name: str,
    daily: List[dict],
    agg: dict,
    ref_daily: List[dict],
    *,
    period: str,
) -> dict:
    port = portfolio_stats(daily, ACCOUNT, metrics_mode="eligible_only")
    ref = portfolio_stats(ref_daily, ACCOUNT, metrics_mode="eligible_only")
    max_dd = float(port.get("max_drawdown_pct") or 0)
    cagr = float(port.get("cagr_pct") or 0)
    calmar = round(cagr / max_dd, 4) if max_dd > 0 else 0.0
    ref_cagr = float(ref.get("cagr_pct") or 0)
    ref_dd = float(ref.get("max_drawdown_pct") or 0)
    ref_calmar = round(ref_cagr / ref_dd, 4) if ref_dd > 0 else 0.0
    n = agg["trades"]
    return {
        "period": period,
        "variant": name,
        "n_days": len(daily),
        **port,
        "calmar": calmar,
        "total_trades": n,
        "win_rate": round(agg["wins"] / n, 4) if n else 0.0,
        "stop_rate": round(agg["stopped"] / n, 4) if n else 0.0,
        "cagr_delta_vs_baseline": round(cagr - ref_cagr, 2),
        "calmar_delta_vs_baseline": round(calmar - ref_calmar, 4),
        "worst_day_delta_vs_baseline": round(
            float(port.get("worst_day_pct") or 0) - float(ref.get("worst_day_pct") or 0), 2
        ),
        "max_dd_delta_vs_baseline": round(max_dd - ref_dd, 2),
    }


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


def write_csv(path: Path, rows: List[dict]) -> None:
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def run(*, resume: bool = False, checkpoint_every: int = 25, max_oos: int = 0) -> None:
    variants = build_variants()
    names = list(variants.keys())
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
    trade_agg: Dict[str, dict] = {n: empty_agg() for n in names}
    start_oos = 0

    if resume:
        ckpt = load_checkpoint(ckpt_path)
        if ckpt and ckpt.get("version") == CHECKPOINT_VERSION:
            if ckpt.get("complete") and int(ckpt.get("oos_total", 0)) == oos_total:
                print("Already complete.", flush=True)
                _finalize(variants, daily_by_from_ckpt=ckpt["daily_by"], trade_agg_from_ckpt=ckpt["trade_agg"])
                return
            if int(ckpt.get("oos_done", 0)) > 0:
                daily_by = ckpt["daily_by"]
                trade_agg = ckpt["trade_agg"]
                start_oos = int(ckpt["oos_done"])
                print(f"Resume at {start_oos}/{oos_total}", flush=True)

    print(
        f"Promo compare: {names} × {oos_total} OOS days "
        f"(selection<={SELECTION_END}, holdout>={HOLDOUT_START})",
        flush=True,
    )
    t0 = time_mod.time()

    for oos_i in range(start_oos, oos_total):
        index = TRAIN + oos_i
        test_date = eligible[index]
        train_dates = eligible[index - TRAIN : index]
        day_dir = PROCESSED / "symbol=SPXW" / f"date={test_date}"
        quotes = read_quotes_csv(day_dir / "normalized_option_quotes.csv")
        signals = load_rolled_signals(PROCESSED, "SPXW", train_dates, test_date)
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
            save_checkpoint(
                ckpt_path,
                {
                    "version": CHECKPOINT_VERSION,
                    "oos_done": done,
                    "oos_total": oos_total,
                    "last_date": test_date,
                    "complete": done == oos_total,
                    "daily_by": daily_by,
                    "trade_agg": trade_agg,
                },
            )

    _finalize(variants, daily_by_from_ckpt=daily_by, trade_agg_from_ckpt=trade_agg)
    print(f"Done in {(time_mod.time() - t0) / 60:.1f} min", flush=True)


def _finalize(variants, *, daily_by_from_ckpt, trade_agg_from_ckpt) -> None:
    daily_by = daily_by_from_ckpt
    trade_agg = trade_agg_from_ckpt
    ref = "baseline_pre_wnla"
    periods: List[Tuple[str, Optional[str], Optional[str]]] = [
        ("full", None, None),
        ("selection", SELECTION_END, None),
        ("holdout", None, HOLDOUT_START),
    ]
    all_rows = []
    for period, end, start in periods:
        ref_daily = filter_daily(daily_by[ref], end=end, start=start)
        for name in variants:
            daily = filter_daily(daily_by[name], end=end, start=start)
            # Trade aggs are full-run only; zero them for period slices except full.
            agg = trade_agg[name] if period == "full" else empty_agg()
            if period != "full":
                agg = {
                    "trades": sum(int(r.get("trades") or 0) for r in daily),
                    "wins": 0,
                    "stopped": sum(int(r.get("stopped_trades") or 0) for r in daily),
                    "total_pnl": sum(float(r.get("net_pnl") or 0) for r in daily),
                }
            all_rows.append(summarize_period(name, daily, agg, ref_daily, period=period))

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "summary.json").write_text(json.dumps(all_rows, indent=2), encoding="utf-8")
    write_csv(OUT / "summary.csv", all_rows)
    for name, rows in daily_by.items():
        write_csv(OUT / f"daily_{name}.csv", rows)

    by_period = {p: [r for r in all_rows if r["period"] == p] for p in ("full", "selection", "holdout")}
    lines = [
        "# Promo compare: FOMC 13:30 + VIX>=20 put+25 vs pre-WNLA baseline",
        "",
        f"Selection <= `{SELECTION_END}` | Holdout >= `{HOLDOUT_START}`",
        "",
        "| Period | Variant | CAGR | Calmar | MaxDD | Worst | Trades | dCAGR | dCalmar |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for period in ("selection", "holdout", "full"):
        for row in by_period[period]:
            lines.append(
                f"| {period} | {row['variant']} | {row.get('cagr_pct')} | {row.get('calmar')} | "
                f"{row.get('max_drawdown_pct')} | {row.get('worst_day_pct')} | {row.get('total_trades')} | "
                f"{row.get('cagr_delta_vs_baseline')} | {row.get('calmar_delta_vs_baseline')} |"
            )
    sel_promo = next(r for r in by_period["selection"] if r["variant"] != ref)
    ho_promo = next(r for r in by_period["holdout"] if r["variant"] != ref)
    lines.extend(
        [
            "",
            "## Verdict",
            f"- Selection: promo Calmar {sel_promo.get('calmar')} (d {sel_promo.get('calmar_delta_vs_baseline')})",
            f"- Holdout: promo Calmar {ho_promo.get('calmar')} (d {ho_promo.get('calmar_delta_vs_baseline')})",
        ]
    )
    holdout_ok = (
        float(ho_promo.get("calmar_delta_vs_baseline") or 0) >= -0.05
        and float(ho_promo.get("cagr_delta_vs_baseline") or 0) >= -1.0
        and float(ho_promo.get("worst_day_delta_vs_baseline") or 0) >= -0.5
    )
    lines.append(
        f"- Holdout promotion gate: **{'PASS' if holdout_ok else 'FAIL'}** "
        f"(Calmar d>=-0.05, CAGR d>=-1pp, worst d>=-0.5pp)"
    )
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
