"""Score, attribute, and report overnight Calmar suite results."""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))

from vix_daily import DEFAULT_VIX_CSV, load_vix_daily, regime_bucket  # noqa: E402

OUT = ROOT / "data" / "overnight_calmar_suite"
BASELINE_RUN = ROOT / "data" / "dashboard_runs" / "p3_poststop_cooldown_120"


def constrained_score(row: dict) -> float:
    cagr = float(row.get("cagr_pct") or 0)
    max_dd = float(row.get("max_drawdown_pct") or 0)
    worst = float(row.get("worst_day_pct") or 0)
    sharpe = float(row.get("sharpe") or 0)
    calmar = cagr / max_dd if max_dd > 0 else 0.0
    penalty_dd = 2.0 * max(0.0, max_dd - 9.5)
    penalty_worst = 3.0 * max(0.0, abs(worst) - 6.82)
    penalty_cagr = 5.0 * max(0.0, 16.0 - cagr)
    return calmar - penalty_dd - penalty_worst - penalty_cagr + 0.1 * sharpe


def hard_reject(row: dict) -> str:
    worst = float(row.get("worst_day_pct") or 0)
    max_dd = float(row.get("max_drawdown_pct") or 0)
    cagr = float(row.get("cagr_pct") or 0)
    if worst < -7.5:
        return "worst_day"
    if max_dd > 11.0:
        return "max_dd"
    if cagr < 16.0:
        return "cagr_floor"
    return ""


def run_attribution() -> dict:
    import pandas as pd

    daily_path = BASELINE_RUN / "daily_summary.csv"
    if not daily_path.exists():
        return {"error": "baseline daily_summary missing"}
    vix_by = load_vix_daily(DEFAULT_VIX_CSV)
    daily = pd.read_csv(daily_path)
    daily["date_str"] = daily["date"].astype(str).str[:10]
    daily["vix_open"] = daily["date_str"].map(
        lambda d: vix_by[d].open if d in vix_by else None
    )
    daily["vix_bucket"] = daily["vix_open"].map(
        lambda v: regime_bucket(v) if pd.notna(v) else "missing"
    )
    worst = daily.nsmallest(20, "net_pnl")
    by_bucket = (
        daily.groupby("vix_bucket")
        .agg(days=("date", "count"), mean_pnl=("net_pnl", "mean"), worst=("net_pnl", "min"))
        .reset_index()
    )
    return {
        "worst_20_dates": worst[["date_str", "net_pnl", "vix_open", "vix_bucket", "stopped_trades"]].to_dict("records"),
        "pnl_by_vix_bucket": by_bucket.to_dict("records"),
    }


def era_breakdown(variant_name: str, shard_ckpts: List[dict]) -> List[dict]:
    rows = []
    for ckpt in shard_ckpts:
        for day in ckpt.get("daily_by", {}).get(variant_name, []):
            rows.append(day)
    by_era: Dict[str, dict] = defaultdict(lambda: {"days": 0, "pnl": 0.0})
    for row in rows:
        era = row.get("era") or "unknown"
        by_era[era]["days"] += 1
        by_era[era]["pnl"] += float(row.get("net_pnl") or 0)
    out = []
    for era, agg in sorted(by_era.items()):
        out.append({"era": era, "days": agg["days"], "net_pnl": round(agg["pnl"], 0)})
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=15)
    args = parser.parse_args()

    summary_path = OUT / "summary.json"
    if not summary_path.exists():
        raise SystemExit(f"Missing {summary_path} — run merge first.")

    rows: List[dict] = json.loads(summary_path.read_text(encoding="utf-8"))
    for row in rows:
        row["constrained_score"] = round(constrained_score(row), 4)
        row["reject_reason"] = hard_reject(row)

    passing = [r for r in rows if not r["reject_reason"]]
    passing.sort(key=lambda r: r["constrained_score"], reverse=True)

    by_phase: Dict[str, List[dict]] = defaultdict(list)
    for row in passing:
        by_phase[row["phase"]].append(row)

    phase_winners = {
        phase: sorted(v, key=lambda r: r["constrained_score"], reverse=True)[:3]
        for phase, v in by_phase.items()
    }

    attribution = run_attribution()
    report = {
        "generated_at": "",
        "reference": next((r for r in rows if r["variant"] == "baseline_vix125"), rows[0]),
        "attribution": attribution,
        "total_variants": len(rows),
        "passing_variants": len(passing),
        "top_overall": passing[: args.top],
        "phase_winners": {
            phase: [
                {
                    "variant": w["variant"],
                    "cagr_pct": w["cagr_pct"],
                    "max_drawdown_pct": w["max_drawdown_pct"],
                    "worst_day_pct": w["worst_day_pct"],
                    "calmar": w.get("calmar"),
                    "constrained_score": w["constrained_score"],
                }
                for w in winners
            ]
            for phase, winners in phase_winners.items()
        },
    }
    report["generated_at"] = __import__("datetime").datetime.now().isoformat()

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("\n=== OVERNIGHT CALMAR SUITE REPORT ===")
    ref = report["reference"]
    print(
        f"Reference {ref['variant']}: CAGR {ref['cagr_pct']}%  Calmar {ref.get('calmar')}  "
        f"maxDD {ref['max_drawdown_pct']}%  worst {ref['worst_day_pct']}%"
    )
    print(f"\nTop {args.top} (constrained score, passing filters):")
    for i, row in enumerate(passing[: args.top], 1):
        print(
            f"  {i:2d}. [{row['phase']}] {row['variant']:<32} "
            f"CAGR {row['cagr_pct']:5.1f}%  Calmar {row.get('calmar', 0):4.2f}  "
            f"DD {row['max_drawdown_pct']:5.2f}%  worst {row['worst_day_pct']:5.2f}%  "
            f"score {row['constrained_score']:.3f}"
        )
    print(f"\nWrote {OUT / 'report.json'}")


if __name__ == "__main__":
    main()
