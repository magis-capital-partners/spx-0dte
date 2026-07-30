"""Rank Why-Not-Look-At variants on selection; validate on sealed holdout.

Also writes diagnostic slices (W1-5, W3-D, W6-D, W7-D, W4-D stub) from baseline days.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))
sys.path.insert(0, str(ROOT / "scripts"))

from vix_daily import DEFAULT_VIX_CSV, load_vix_daily, regime_bucket, regime_bucket_5  # noqa: E402
from why_not_look_at_variants import (  # noqa: E402
    DIAGNOSTIC_IDS,
    HOLDOUT_START,
    SELECTION_END,
    load_fomc_dates,
)

OUT = ROOT / "data" / "why_not_look_at"

WORST_DAY_FLOOR = -8.0
MAX_DD_CEILING = 12.0
CAGR_FLOOR = 12.0


def constrained_score(row: dict) -> float:
    cagr = float(row.get("cagr_pct") or 0)
    max_dd = float(row.get("max_drawdown_pct") or 0)
    worst = float(row.get("worst_day_pct") or 0)
    sharpe = float(row.get("sharpe") or 0)
    calmar = cagr / max_dd if max_dd > 0 else 0.0
    penalty_dd = 2.0 * max(0.0, max_dd - 9.0)
    penalty_worst = 3.0 * max(0.0, abs(worst) - 7.0)
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
    out.sort(key=lambda r: (0 if not r["reject_reason"] else 1, -r["constrained_score"]))
    return out


def compact(row: Optional[dict]) -> Optional[dict]:
    if not row:
        return None
    keys = [
        "variant",
        "phase",
        "n_days",
        "cagr_pct",
        "calmar",
        "max_drawdown_pct",
        "worst_day_pct",
        "sharpe",
        "constrained_score",
        "reject_reason",
        "cagr_delta_vs_ref",
        "max_dd_delta_vs_ref",
        "worst_day_delta_vs_ref",
        "calmar_delta_vs_ref",
        "total_trades",
        "stop_rate",
    ]
    return {k: row.get(k) for k in keys}


def holdout_pass(sel: dict, hold: dict, ref_hold: dict) -> dict:
    if not hold or not sel:
        return {"holdout_pass": False, "checks": {}}
    checks = {
        "calmar_ge_ref": float(hold.get("calmar") or 0) >= float(ref_hold.get("calmar") or 0) - 0.05,
        "cagr_not_worse_1pp": float(hold.get("cagr_pct") or 0) >= float(ref_hold.get("cagr_pct") or 0) - 1.0,
        "worst_not_worse_0_5pp": float(hold.get("worst_day_pct") or 0)
        >= float(ref_hold.get("worst_day_pct") or 0) - 0.5,
        "no_hard_reject_sel": not sel.get("reject_reason"),
    }
    return {"holdout_pass": all(checks.values()), "checks": checks}


def attribution_from_baseline() -> dict:
    daily_path = OUT / "daily_by_compact.json"
    if not daily_path.exists():
        return {"error": "daily_by_compact.json missing — run merge first"}
    daily_by = json.loads(daily_path.read_text(encoding="utf-8"))
    baseline = daily_by.get("baseline") or []
    if not baseline:
        return {"error": "baseline daily missing"}

    vix_by = load_vix_daily(DEFAULT_VIX_CSV)
    fomc = load_fomc_dates()

    by_vix5: Dict[str, dict] = defaultdict(lambda: {"days": 0, "pnl": 0.0, "worst": 0.0})
    by_vix6: Dict[str, dict] = defaultdict(lambda: {"days": 0, "pnl": 0.0, "worst": 0.0})
    fomc_stats = {"days": 0, "pnl": 0.0, "worst": 0.0, "dates": []}
    non_fomc = {"days": 0, "pnl": 0.0, "worst": 0.0}

    for row in baseline:
        d = str(row["date"])[:10]
        pnl = float(row.get("net_pnl") or 0)
        vix = vix_by[d].open if d in vix_by else None
        if vix is not None:
            b5 = regime_bucket_5(vix)
            b6 = regime_bucket(vix)
            for store, key in ((by_vix5, b5), (by_vix6, b6)):
                store[key]["days"] += 1
                store[key]["pnl"] += pnl
                store[key]["worst"] = min(store[key]["worst"], pnl)
        if d in fomc or row.get("is_fomc"):
            fomc_stats["days"] += 1
            fomc_stats["pnl"] += pnl
            fomc_stats["worst"] = min(fomc_stats["worst"], pnl)
            if len(fomc_stats["dates"]) < 30:
                fomc_stats["dates"].append({"date": d, "pnl": pnl})
        else:
            non_fomc["days"] += 1
            non_fomc["pnl"] += pnl
            non_fomc["worst"] = min(non_fomc["worst"], pnl)

    def finalize(store: Dict[str, dict]) -> List[dict]:
        rows = []
        for k, v in sorted(store.items()):
            rows.append(
                {
                    "bucket": k,
                    "days": v["days"],
                    "total_pnl": round(v["pnl"], 2),
                    "mean_pnl": round(v["pnl"] / v["days"], 2) if v["days"] else 0,
                    "worst_day_pnl": round(v["worst"], 2),
                }
            )
        return rows

    return {
        "diagnostics_covered": DIAGNOSTIC_IDS,
        "W1_5_pnl_by_vix5": finalize(by_vix5),
        "W1_5_pnl_by_vix6_legacy": finalize(by_vix6),
        "W6_D_fomc_vs_other": {
            "fomc": {
                "days": fomc_stats["days"],
                "total_pnl": round(fomc_stats["pnl"], 2),
                "mean_pnl": round(fomc_stats["pnl"] / fomc_stats["days"], 2) if fomc_stats["days"] else 0,
                "worst_day_pnl": round(fomc_stats["worst"], 2),
                "sample_dates": fomc_stats["dates"],
            },
            "non_fomc": {
                "days": non_fomc["days"],
                "total_pnl": round(non_fomc["pnl"], 2),
                "mean_pnl": round(non_fomc["pnl"] / non_fomc["days"], 2) if non_fomc["days"] else 0,
                "worst_day_pnl": round(non_fomc["worst"], 2),
            },
        },
        "protocol": {
            "selection_end": SELECTION_END,
            "holdout_start": HOLDOUT_START,
            "note": "Rank on selection only; holdout is sealed validation.",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=15)
    args = parser.parse_args()

    sel_path = OUT / "summary_selection.json"
    hold_path = OUT / "summary_holdout.json"
    if not sel_path.exists() or not hold_path.exists():
        raise SystemExit("Missing summary_selection.json / summary_holdout.json — run merge first")

    selection = annotate(json.loads(sel_path.read_text(encoding="utf-8")))
    holdout = annotate(json.loads(hold_path.read_text(encoding="utf-8")))
    hold_by = {r["variant"]: r for r in holdout}
    ref_sel = next(r for r in selection if r["variant"] == "baseline")
    ref_hold = hold_by["baseline"]

    phase_winners = {}
    for row in selection:
        if row.get("reject_reason"):
            continue
        phase = row["phase"]
        if phase == "ref":
            continue
        cur = phase_winners.get(phase)
        if cur is None or row["constrained_score"] > cur["constrained_score"]:
            phase_winners[phase] = row

    validated = []
    for phase, cand in phase_winners.items():
        h = hold_by.get(cand["variant"])
        result = holdout_pass(cand, h, ref_hold)
        validated.append(
            {
                "phase": phase,
                "variant": cand["variant"],
                "selection": compact(cand),
                "holdout": compact(h),
                **result,
            }
        )
    validated.sort(key=lambda v: (0 if v["holdout_pass"] else 1, -float((v["selection"] or {}).get("constrained_score") or 0)))

    attribution = attribution_from_baseline()

    report = {
        "protocol": {
            "selection_end": SELECTION_END,
            "holdout_start": HOLDOUT_START,
            "rank_on": "selection_only",
            "validate_on": "holdout_sealed",
            "note": "Do not retune thresholds using holdout metrics.",
        },
        "reference_selection": compact(ref_sel),
        "reference_holdout": compact(ref_hold),
        "total_variants": len(selection),
        "passing_selection": sum(1 for r in selection if not r["reject_reason"]),
        "top_selection": [compact(r) for r in selection[: args.top]],
        "top_selection_with_holdout": [
            {
                "selection": compact(r),
                "holdout": compact(hold_by.get(r["variant"])),
                **holdout_pass(r, hold_by.get(r["variant"]), ref_hold),
            }
            for r in selection[: args.top]
        ],
        "phase_winners_selection": {k: compact(v) for k, v in phase_winners.items()},
        "promotion_validation": validated,
        "recommended_promotion": next((v for v in validated if v["holdout_pass"]), None),
        "diagnostics": attribution,
    }

    (OUT / "summary_selection_scored.json").write_text(json.dumps(selection, indent=2), encoding="utf-8")
    (OUT / "summary_holdout_scored.json").write_text(json.dumps(holdout, indent=2), encoding="utf-8")
    (OUT / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    # Human-readable markdown summary
    lines = [
        "# Why-Not-Look-At Suite Results",
        "",
        f"Selection end: `{SELECTION_END}` · Holdout start: `{HOLDOUT_START}`",
        "",
        "## Protocol",
        "- Rank / pick winners on **selection only**",
        "- **Holdout sealed** — used only for promotion validation",
        "",
        "## Baseline",
        f"- Selection CAGR {ref_sel.get('cagr_pct')}% Calmar {ref_sel.get('calmar')} "
        f"DD {ref_sel.get('max_drawdown_pct')}% worst {ref_sel.get('worst_day_pct')}%",
        f"- Holdout CAGR {ref_hold.get('cagr_pct')}% Calmar {ref_hold.get('calmar')} "
        f"DD {ref_hold.get('max_drawdown_pct')}% worst {ref_hold.get('worst_day_pct')}%",
        "",
        "## Phase winners (selection → holdout check)",
        "",
        "| Phase | Variant | Sel Calmar | HO Calmar | Pass |",
        "|---|---|---:|---:|---|",
    ]
    for v in validated:
        s = v["selection"] or {}
        h = v["holdout"] or {}
        lines.append(
            f"| {v['phase']} | {v['variant']} | {s.get('calmar')} | {h.get('calmar')} | "
            f"{'YES' if v['holdout_pass'] else 'no'} |"
        )
    promo = report["recommended_promotion"]
    lines.extend(["", "## Recommended promotion", ""])
    if promo:
        lines.append(f"**{promo['variant']}** passed sealed holdout checks.")
    else:
        lines.append("No phase winner passed sealed holdout promotion gates vs baseline.")
    lines.extend(["", "## Top selection (holdout shown for transparency)", ""])
    for item in report["top_selection_with_holdout"][:10]:
        s = item["selection"] or {}
        h = item["holdout"] or {}
        lines.append(
            f"- `{s.get('variant')}` sel Calmar {s.get('calmar')} / HO {h.get('calmar')} "
            f"({'pass' if item['holdout_pass'] else 'fail'})"
        )
    if "W1_5_pnl_by_vix5" in attribution:
        lines.extend(["", "## W1-5 VIX-5 attribution (baseline days)", ""])
        for row in attribution["W1_5_pnl_by_vix5"]:
            lines.append(
                f"- {row['bucket']}: {row['days']}d mean ${row['mean_pnl']:,.0f} "
                f"worst ${row['worst_day_pnl']:,.0f}"
            )
    if "W6_D_fomc_vs_other" in attribution:
        f = attribution["W6_D_fomc_vs_other"]["fomc"]
        n = attribution["W6_D_fomc_vs_other"]["non_fomc"]
        lines.extend(
            [
                "",
                "## W6-D FOMC attribution",
                f"- FOMC: {f['days']}d mean ${f['mean_pnl']:,.0f} worst ${f['worst_day_pnl']:,.0f}",
                f"- Non-FOMC: {n['days']}d mean ${n['mean_pnl']:,.0f} worst ${n['worst_day_pnl']:,.0f}",
            ]
        )
    md_path = OUT / "SUMMARY.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("\n=== WHY-NOT-LOOK-AT (selection -> sealed holdout) ===")
    print(
        f"Ref SELECT  CAGR {ref_sel['cagr_pct']}%  Calmar {ref_sel.get('calmar')}  "
        f"DD {ref_sel.get('max_drawdown_pct')}%  worst {ref_sel.get('worst_day_pct')}%"
    )
    print(
        f"Ref HOLDOUT CAGR {ref_hold['cagr_pct']}%  Calmar {ref_hold.get('calmar')}  "
        f"DD {ref_hold.get('max_drawdown_pct')}%  worst {ref_hold.get('worst_day_pct')}%"
    )
    print(f"\nPhase winners: {len(validated)} | holdout passes: {sum(1 for v in validated if v['holdout_pass'])}")
    for v in validated:
        print(
            f"  {v['phase']:12s} {v['variant']:28s} "
            f"pass={v['holdout_pass']} sel_calmar={(v['selection'] or {}).get('calmar')} "
            f"ho_calmar={(v['holdout'] or {}).get('calmar')}"
        )
    print(f"\nWrote {OUT / 'report.json'} and {md_path}")


if __name__ == "__main__":
    main()
