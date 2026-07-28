"""Stop/fee stress battery on production profile (Phase 3 remediation).

Runs a small matrix of fill/cost knobs against ``p3_poststop_cooldown_120``,
reports full-sample and sealed holdout metrics, plus calendar-time CAGR.

  python scripts/run_stop_fill_stress.py --max-days 60
  python scripts/run_stop_fill_stress.py --full

Outputs: data/stop_fill_stress/
"""
from __future__ import annotations

import argparse
import json
import sys
import time as time_mod
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
SIM = ROOT / "simulator"
sys.path.insert(0, str(SIM))

from expiry_calendar import (  # noqa: E402
    discover_eligible_dates,
    load_era_rules,
    resolve_start_date,
)
from mbh_simulator import read_quotes_csv, read_signals_csv, simulate_day  # noqa: E402
from portfolio_metrics import portfolio_stats  # noqa: E402
from profiles import (  # noqa: E402
    PRODUCTION_ACCOUNT_EQUITY,
    PRODUCTION_TRAIN_COUNT,
    SCHEMES,
    build_p3_poststop_cooldown_config,
)
from regime_validation import apply_rolling_baseline, discover_dates  # noqa: E402
from vix_sizing_policies import build_production_vix_policy  # noqa: E402

PROCESSED = ROOT / "data" / "processed"
OUT = ROOT / "data" / "stop_fill_stress"
ACCOUNT = PRODUCTION_ACCOUNT_EQUITY
TRAIN = PRODUCTION_TRAIN_COUNT
SELECTION_END = "2023-12-29"
HOLDOUT_START = "2024-01-02"


def build_stress_variants() -> Dict[str, object]:
    """Named stress configs on top of production."""
    base = build_p3_poststop_cooldown_config(account_equity=ACCOUNT)
    return {
        "baseline_slip005_stop0_fee079": replace(
            base, entry_fill_slippage=0.05, stop_fill_slippage=0.0, fee_per_contract=0.79
        ),
        "entry010_stop0_fee079": replace(
            base, entry_fill_slippage=0.10, stop_fill_slippage=0.0, fee_per_contract=0.79
        ),
        "entry005_stop010_fee079": replace(
            base, entry_fill_slippage=0.05, stop_fill_slippage=0.10, fee_per_contract=0.79
        ),
        "entry005_stop025_fee079": replace(
            base, entry_fill_slippage=0.05, stop_fill_slippage=0.25, fee_per_contract=0.79
        ),
        "entry005_stop050_fee079": replace(
            base, entry_fill_slippage=0.05, stop_fill_slippage=0.50, fee_per_contract=0.79
        ),
        "stressed_base_entry005_stop025_fee125": replace(
            base, entry_fill_slippage=0.05, stop_fill_slippage=0.25, fee_per_contract=1.25
        ),
        "stressed_harsh_entry010_stop050_fee175": replace(
            base, entry_fill_slippage=0.10, stop_fill_slippage=0.50, fee_per_contract=1.75
        ),
    }


def _load_day(date: str):
    qpath = PROCESSED / f"symbol=SPXW/date={date}/normalized_option_quotes.csv"
    spath = PROCESSED / f"symbol=SPXW/date={date}/signals_unconditional.csv"
    if not qpath.is_file():
        return None, None
    quotes = read_quotes_csv(qpath)
    signals = read_signals_csv(spath) if spath.is_file() else []
    return quotes, signals


def _stats_slice(daily: List[dict], start: Optional[str], end: Optional[str], equity: float) -> dict:
    rows = daily
    if start:
        rows = [r for r in rows if str(r["date"]) >= start]
    if end:
        rows = [r for r in rows if str(r["date"]) <= end]
    eligible = portfolio_stats(rows, equity, metrics_mode="eligible_only")
    calendar = portfolio_stats(rows, equity, metrics_mode="all_rows")
    cagr_e = float(eligible.get("cagr") or 0.0)
    dd = float(eligible.get("max_drawdown") or 0.0)
    return {
        "days": eligible.get("days", 0),
        "cagr_eligible": cagr_e,
        "cagr_calendar": float(calendar.get("cagr") or 0.0),
        "max_drawdown": dd,
        "calmar_eligible": (cagr_e / dd) if dd > 1e-9 else None,
        "sharpe": eligible.get("sharpe"),
        "worst_day_pnl": eligible.get("worst_day"),
        "stop_rate": eligible.get("stop_rate"),
        "trades": eligible.get("trades"),
    }


def run_variant(name: str, cfg, dates: List[str], policy) -> dict:
    daily: List[dict] = []
    for date in dates:
        quotes, signals = _load_day(date)
        if quotes is None:
            continue
        day_cfg = replace(cfg, target_expiry=date)
        # Rolling baselines already in signals_unconditional when present.
        result = simulate_day(quotes, signals or [], config=day_cfg, policy=policy)
        stopped = sum(1 for t in result.trades if t.stopped)
        daily.append({
            "date": date,
            "net_pnl": result.net_pnl,
            "trades": len(result.trades),
            "stopped_trades": stopped,
            "eligible": True,
        })
    full = _stats_slice(daily, None, None, ACCOUNT)
    selection = _stats_slice(daily, None, SELECTION_END, ACCOUNT)
    holdout = _stats_slice(daily, HOLDOUT_START, None, ACCOUNT)
    return {
        "name": name,
        "config": {
            "entry_fill_slippage": cfg.entry_fill_slippage,
            "stop_fill_slippage": cfg.stop_fill_slippage,
            "fee_per_contract": cfg.fee_per_contract,
        },
        "full": full,
        "selection": selection,
        "holdout": holdout,
        "daily_rows": len(daily),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-days", type=int, default=0, help="Limit eligible days (0=all)")
    parser.add_argument("--full", action="store_true", help="Run all days (ignore --max-days default)")
    parser.add_argument("--variant", type=str, default="", help="Run a single named variant")
    args = parser.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    variants = build_stress_variants()
    if args.variant:
        if args.variant not in variants:
            raise SystemExit(f"unknown variant {args.variant!r}; choose from {list(variants)}")
        variants = {args.variant: variants[args.variant]}

    rules = load_era_rules()
    all_dates = discover_dates(PROCESSED)
    eligible = discover_eligible_dates(all_dates, rules)
    start = resolve_start_date(eligible, TRAIN)
    oos = [d for d in eligible if d >= start]
    if args.max_days > 0 and not args.full:
        oos = oos[-args.max_days :]

    scheme = SCHEMES["linear_decay_downsize"]
    policy = build_production_vix_policy(scheme)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "account_equity": ACCOUNT,
        "oos_days": len(oos),
        "oos_start": oos[0] if oos else None,
        "oos_end": oos[-1] if oos else None,
        "selection_end": SELECTION_END,
        "holdout_start": HOLDOUT_START,
        "variants": [],
    }

    t0 = time_mod.time()
    for name, cfg in variants.items():
        print(f"[{datetime.now().isoformat()}] running {name} over {len(oos)} days…")
        row = run_variant(name, cfg, oos, policy)
        report["variants"].append({k: v for k, v in row.items() if k != "daily_rows"})
        (OUT / f"{name}.json").write_text(json.dumps(row, indent=2), encoding="utf-8")
        h = row["holdout"]
        print(
            f"  holdout CAGR={h['cagr_eligible']:.2%} DD={h['max_drawdown']:.2%} "
            f"Calmar={h['calmar_eligible']}"
        )

    stressed = next(
        (v for v in report["variants"] if v["name"] == "stressed_base_entry005_stop025_fee125"),
        report["variants"][0] if report["variants"] else None,
    )
    report["stressed_baseline"] = stressed
    report["elapsed_seconds"] = round(time_mod.time() - t0, 1)
    (OUT / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    lines = [
        "# Stop / fee stress FINAL_REPORT",
        "",
        f"Generated: {report['generated_at']}",
        f"OOS days: {report['oos_days']} ({report['oos_start']} → {report['oos_end']})",
        f"Selection ≤ {SELECTION_END}; holdout ≥ {HOLDOUT_START}",
        "",
        "Use `stressed_base_entry005_stop025_fee125` as the sizing reference until a fuller",
        "run completes. Headline 19.85% CAGR / 8.29% DD is the optimistic upper bound.",
        "",
        "| Variant | Holdout CAGR | Holdout DD | Holdout Calmar | Full CAGR |",
        "|---|---:|---:|---:|---:|",
    ]
    for v in report["variants"]:
        h, f = v["holdout"], v["full"]
        cal = h["calmar_eligible"]
        cal_s = f"{cal:.2f}" if cal is not None else "n/a"
        lines.append(
            f"| `{v['name']}` | {h['cagr_eligible']:.2%} | {h['max_drawdown']:.2%} | "
            f"{cal_s} | {f['cagr_eligible']:.2%} |"
        )
    (OUT / "FINAL_REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {OUT / 'report.json'} and FINAL_REPORT.md in {report['elapsed_seconds']}s")


if __name__ == "__main__":
    main()
