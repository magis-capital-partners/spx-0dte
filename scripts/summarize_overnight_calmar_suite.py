"""Score overnight Calmar Wave 3 with selection ranking + sealed holdout validation."""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))
sys.path.insert(0, str(ROOT / "scripts"))

from overnight_calmar_variants import (  # noqa: E402
    HOLDOUT_START,
    PROMO_CANDIDATE_NAMES,
    SELECTION_END,
)
from vix_daily import DEFAULT_VIX_CSV, load_vix_daily, regime_bucket  # noqa: E402

OUT = ROOT / "data" / "overnight_calmar_suite"
BASELINE_RUN = ROOT / "data" / "dashboard_runs" / "p3_poststop_cooldown_120"

# Slightly tighter floors vs Wave 2 because substrate already includes put_wing_150.
WORST_DAY_FLOOR = -7.5
MAX_DD_CEILING = 11.0
CAGR_FLOOR = 16.0
# Soft penalties relative to Wave 2 baseline shape on put-150 substrate.
SOFT_DD = 9.0
SOFT_WORST = 6.82


def constrained_score(row: dict) -> float:
    cagr = float(row.get("cagr_pct") or 0)
    max_dd = float(row.get("max_drawdown_pct") or 0)
    worst = float(row.get("worst_day_pct") or 0)
    sharpe = float(row.get("sharpe") or 0)
    calmar = cagr / max_dd if max_dd > 0 else 0.0
    penalty_dd = 2.0 * max(0.0, max_dd - SOFT_DD)
    penalty_worst = 3.0 * max(0.0, abs(worst) - SOFT_WORST)
    penalty_cagr = 5.0 * max(0.0, CAGR_FLOOR - cagr)
    return calmar - penalty_dd - penalty_worst - penalty_cagr + 0.1 * sharpe


def hard_reject(row: dict) -> str:
    worst = float(row.get("worst_day_pct") or 0)
    max_dd = float(row.get("max_drawdown_pct") or 0)
    cagr = float(row.get("cagr_pct") or 0)
    if worst < WORST_DAY_FLOOR:
        return "worst_day"
    if max_dd > MAX_DD_CEILING:
        return "max_dd"
    if cagr < CAGR_FLOOR:
        return "cagr_floor"
    return ""


def annotate(rows: List[dict]) -> List[dict]:
    out = []
    for row in rows:
        r = dict(row)
        r["constrained_score"] = round(constrained_score(r), 4)
        r["reject_reason"] = hard_reject(r)
        out.append(r)
    return out


def by_name(rows: List[dict]) -> Dict[str, dict]:
    return {r["variant"]: r for r in rows}


def run_attribution() -> dict:
    import pandas as pd

    daily_path = BASELINE_RUN / "daily_summary.csv"
    if not daily_path.exists():
        return {"error": "baseline daily_summary missing"}
    vix_by = load_vix_daily(DEFAULT_VIX_CSV)
    daily = pd.read_csv(daily_path)
    daily["date_str"] = daily["date"].astype(str).str[:10]
    daily["vix_open"] = daily["date_str"].map(lambda d: vix_by[d].open if d in vix_by else None)
    daily["vix_bucket"] = daily["vix_open"].map(lambda v: regime_bucket(v) if pd.notna(v) else "missing")
    worst = daily.nsmallest(20, "net_pnl")
    by_bucket = (
        daily.groupby("vix_bucket")
        .agg(days=("date", "count"), mean_pnl=("net_pnl", "mean"), worst=("net_pnl", "min"))
        .reset_index()
    )
    return {
        "worst_20_dates": worst[["date_str", "net_pnl", "vix_open", "vix_bucket", "stopped_trades"]].to_dict(
            "records"
        ),
        "pnl_by_vix_bucket": by_bucket.to_dict("records"),
    }


def compact(row: Optional[dict]) -> Optional[dict]:
    if not row:
        return None
    return {
        "variant": row["variant"],
        "phase": row.get("phase"),
        "n_days": row.get("n_days"),
        "cagr_pct": row.get("cagr_pct"),
        "calmar": row.get("calmar"),
        "max_drawdown_pct": row.get("max_drawdown_pct"),
        "worst_day_pct": row.get("worst_day_pct"),
        "sharpe": row.get("sharpe"),
        "constrained_score": row.get("constrained_score"),
        "reject_reason": row.get("reject_reason"),
        "cagr_delta_vs_ref": row.get("cagr_delta_vs_ref"),
        "max_dd_delta_vs_ref": row.get("max_dd_delta_vs_ref"),
        "worst_day_delta_vs_ref": row.get("worst_day_delta_vs_ref"),
        "calmar_delta_vs_ref": row.get("calmar_delta_vs_ref"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=15)
    args = parser.parse_args()

    sel_path = OUT / "summary_selection.json"
    hold_path = OUT / "summary_holdout.json"
    full_path = OUT / "summary.json"
    if not sel_path.exists() or not hold_path.exists() or not full_path.exists():
        raise SystemExit(
            f"Missing period summaries under {OUT} — run merge_overnight_calmar_shards.py first."
        )

    selection = annotate(json.loads(sel_path.read_text(encoding="utf-8")))
    holdout = annotate(json.loads(hold_path.read_text(encoding="utf-8")))
    full = annotate(json.loads(full_path.read_text(encoding="utf-8")))

    hold_by = by_name(holdout)
    full_by = by_name(full)

    passing_sel = [r for r in selection if not r["reject_reason"]]
    passing_sel.sort(key=lambda r: r["constrained_score"], reverse=True)

    # Promotion gate: only pre-registered PROMO_CANDIDATE_NAMES; holdout sealed.
    ref_sel = next(r for r in selection if r["variant"] == "baseline_vix125")
    ref_hold = hold_by["baseline_vix125"]
    promo_candidates = [r for r in passing_sel if r["variant"] in PROMO_CANDIDATE_NAMES]
    if not promo_candidates:
        promo_candidates = [r for r in selection if r["variant"] in PROMO_CANDIDATE_NAMES]

    validated = []
    for cand in promo_candidates:
        h = hold_by.get(cand["variant"])
        if not h:
            continue
        hold_ok = not h["reject_reason"]
        calmar_ok = float(h.get("calmar") or 0) >= float(ref_hold.get("calmar") or 0) - 0.05
        cagr_ok = float(h.get("cagr_pct") or 0) >= float(ref_hold.get("cagr_pct") or 0) - 1.0
        dd_ok = float(h.get("max_drawdown_pct") or 99) <= float(ref_hold.get("max_drawdown_pct") or 0) + 1.0
        worst_ok = float(h.get("worst_day_pct") or -99) >= float(ref_hold.get("worst_day_pct") or 0) - 0.75
        validated.append(
            {
                "variant": cand["variant"],
                "selection": compact(cand),
                "holdout": compact(h),
                "full": compact(full_by.get(cand["variant"])),
                "holdout_pass": hold_ok and calmar_ok and cagr_ok and dd_ok and worst_ok,
                "holdout_checks": {
                    "hard_floors": hold_ok,
                    "calmar_vs_ref": calmar_ok,
                    "cagr_vs_ref": cagr_ok,
                    "dd_vs_ref": dd_ok,
                    "worst_vs_ref": worst_ok,
                },
            }
        )

    validated.sort(
        key=lambda v: (
            1 if v["holdout_pass"] else 0,
            float((v["selection"] or {}).get("constrained_score") or 0),
        ),
        reverse=True,
    )

    by_phase: Dict[str, List[dict]] = defaultdict(list)
    for row in passing_sel:
        by_phase[row["phase"]].append(row)

    report = {
        "generated_at": datetime.now().isoformat(),
        "protocol": {
            "selection_end": SELECTION_END,
            "holdout_start": HOLDOUT_START,
            "rank_on": "selection_only",
            "validate_on": "holdout_sealed",
            "note": "Do not retune thresholds using holdout metrics.",
        },
        "reference_selection": compact(ref_sel),
        "reference_holdout": compact(ref_hold),
        "reference_full": compact(full_by.get("baseline_vix125")),
        "attribution": run_attribution(),
        "total_variants": len(selection),
        "passing_selection": len(passing_sel),
        "top_selection": [compact(r) for r in passing_sel[: args.top]],
        "top_selection_with_holdout": [
            {
                "selection": compact(r),
                "holdout": compact(hold_by.get(r["variant"])),
                "full": compact(full_by.get(r["variant"])),
            }
            for r in passing_sel[: args.top]
        ],
        "phase_winners_selection": {
            phase: [compact(w) for w in sorted(v, key=lambda r: r["constrained_score"], reverse=True)[:3]]
            for phase, v in by_phase.items()
        },
        "promotion_validation": validated,
        "recommended_promotion": next((v for v in validated if v["holdout_pass"]), None),
    }

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (OUT / "summary_selection_scored.json").write_text(json.dumps(selection, indent=2), encoding="utf-8")
    (OUT / "summary_holdout_scored.json").write_text(json.dumps(holdout, indent=2), encoding="utf-8")

    print("\n=== OVERNIGHT CALMAR WAVE 3 (selection → holdout) ===")
    print(f"Selection <= {SELECTION_END} | Holdout >= {HOLDOUT_START}")
    print(
        f"Ref SELECT  CAGR {ref_sel['cagr_pct']}%  Calmar {ref_sel.get('calmar')}  "
        f"DD {ref_sel['max_drawdown_pct']}%  worst {ref_sel['worst_day_pct']}%"
    )
    print(
        f"Ref HOLDOUT CAGR {ref_hold['cagr_pct']}%  Calmar {ref_hold.get('calmar')}  "
        f"DD {ref_hold['max_drawdown_pct']}%  worst {ref_hold['worst_day_pct']}%"
    )
    print(f"\nTop {args.top} on SELECTION (holdout shown for transparency only):")
    for i, row in enumerate(passing_sel[: args.top], 1):
        h = hold_by.get(row["variant"], {})
        print(
            f"  {i:2d}. [{row['phase']}] {row['variant']:<28} "
            f"SEL CAGR {row['cagr_pct']:5.1f}% Calmar {row.get('calmar', 0):4.2f} "
            f"DD {row['max_drawdown_pct']:5.2f}% worst {row['worst_day_pct']:5.2f}% "
            f"| HO CAGR {h.get('cagr_pct', 0):5.1f}% Calmar {h.get('calmar', 0):4.2f} "
            f"DD {h.get('max_drawdown_pct', 0):5.2f}% worst {h.get('worst_day_pct', 0):5.2f}%"
        )

    print("\nPre-specified promo holdout validation:")
    for v in validated:
        status = "PASS" if v["holdout_pass"] else "FAIL"
        s, h = v["selection"], v["holdout"]
        print(
            f"  [{status}] {v['variant']:<28} "
            f"SEL Calmar {s.get('calmar')} -> HO Calmar {h.get('calmar')} "
            f"(HO CAGR {h.get('cagr_pct')}% DD {h.get('max_drawdown_pct')}% "
            f"worst {h.get('worst_day_pct')}%)"
        )

    rec = report["recommended_promotion"]
    if rec:
        print(f"\nRecommended promotion: {rec['variant']} (passed sealed holdout checks)")
    else:
        print("\nNo pre-specified promo passed sealed holdout checks — do not promote.")
    print(f"\nWrote {OUT / 'report.json'}")


if __name__ == "__main__":
    main()
