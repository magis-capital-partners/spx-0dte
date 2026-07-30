"""Rank selective overlay variants on selection; validate on sealed holdout.

Writes winners.json for Phase C/D and SUMMARY.md promotion memo.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from selective_overlay_variants import HOLDOUT_START, SELECTION_END  # noqa: E402

OUT = ROOT / "data" / "selective_straddle_overlay"

MIN_OVERLAY_TRADES = 20
CALMAR_TOL = 0.05
WORST_DAY_TOL = 0.5  # pp


def load_rows(path: Path) -> List[dict]:
    if not path.is_file():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def by_variant(rows: List[dict]) -> Dict[str, dict]:
    return {r["variant"]: r for r in rows}


def holdout_pass(sel: dict, hold: dict, ref_hold: dict) -> dict:
    checks = {
        "calmar_ge_ref_minus_005": float(hold.get("calmar") or 0)
        >= float(ref_hold.get("calmar") or 0) - CALMAR_TOL,
        "worst_not_worse_0_5pp": float(hold.get("worst_day_pct") or 0)
        >= float(ref_hold.get("worst_day_pct") or 0) - WORST_DAY_TOL,
        "overlay_trades_sel_ge_20": int(sel.get("overlay_trades") or 0) >= MIN_OVERLAY_TRADES,
    }
    return {"holdout_pass": all(checks.values()), "checks": checks}


def rank_selection(rows: List[dict], ref_name: str = "B0_prod_only") -> List[dict]:
    scored = []
    for r in rows:
        if r["variant"] == ref_name:
            continue
        if int(r.get("overlay_trades") or 0) < MIN_OVERLAY_TRADES and r.get("structure") != "none":
            continue
        rr = dict(r)
        # Prefer higher combined Calmar, then overlay pnl, penalize worse worst-day vs ref later
        rr["rank_score"] = float(r.get("calmar") or 0) + 0.0001 * float(r.get("overlay_pnl") or 0)
        scored.append(rr)
    scored.sort(key=lambda x: -x["rank_score"])
    return scored


def pick_winners(ranked: List[dict]) -> List[dict]:
    """One best straddle + one best IC (fee-aware structures only)."""
    winners = []
    best_s = next((r for r in ranked if str(r.get("structure") or "").startswith("straddle")), None)
    best_ic = next(
        (
            r
            for r in ranked
            if str(r.get("structure") or "").startswith("ic")
            and "novix" not in str(r.get("structure") or "")
        ),
        None,
    )
    if best_s:
        winners.append({"variant": best_s["variant"], "structure": best_s["structure"], "role": "straddle"})
    if best_ic:
        winners.append({"variant": best_ic["variant"], "structure": best_ic["structure"], "role": "ic"})
    return winners


def compact(row: Optional[dict]) -> Optional[dict]:
    if not row:
        return None
    keys = [
        "variant",
        "phase",
        "structure",
        "gate",
        "n_days",
        "cagr_pct",
        "calmar",
        "max_drawdown_pct",
        "worst_day_pct",
        "overlay_trades",
        "overlay_pnl",
        "overlay_win_rate",
        "fee_blocked_days",
        "low_vol_blocked_days",
        "hit_max_loss_days",
        "cagr_delta_vs_ref",
        "calmar_delta_vs_ref",
        "worst_day_delta_vs_ref",
    ]
    return {k: row.get(k) for k in keys}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True)
    parser.add_argument("--promote", action="store_true", help="Open holdout and write promotion memo")
    args = parser.parse_args()

    root = OUT / args.phase.lower()
    sel_rows = load_rows(root / "summary_selection.json")
    hold_rows = load_rows(root / "summary_holdout.json")
    if not sel_rows:
        raise SystemExit(f"Missing {root / 'summary_selection.json'}")

    sel_by = by_variant(sel_rows)
    hold_by = by_variant(hold_rows)
    ref_sel = sel_by.get("B0_prod_only") or sel_rows[0]
    ref_hold = hold_by.get("B0_prod_only") or (hold_rows[0] if hold_rows else {})

    ranked = rank_selection(sel_rows, ref_name=ref_sel["variant"])
    winners = pick_winners(ranked)

    report = {
        "phase": args.phase.upper(),
        "selection_end": SELECTION_END,
        "holdout_start": HOLDOUT_START,
        "ref_selection": compact(ref_sel),
        "top_selection": [compact(r) for r in ranked[:15]],
        "winners_frozen": winners,
        "holdout": {},
        "promotion": {},
    }

    if args.promote and hold_rows:
        promo = []
        for w in winners:
            name = w["variant"]
            h = hold_by.get(name)
            s = sel_by.get(name)
            if not h or not s:
                continue
            result = holdout_pass(s, h, ref_hold)
            entry = {
                "variant": name,
                "structure": w["structure"],
                "selection": compact(s),
                "holdout": compact(h),
                "ref_holdout": compact(ref_hold),
                **result,
            }
            promo.append(entry)
        # Prefer IC on ties
        promoted = None
        ic_ok = next((p for p in promo if p["structure"].startswith("ic") and p["holdout_pass"]), None)
        s_ok = next((p for p in promo if p["structure"] == "straddle" and p["holdout_pass"]), None)
        if ic_ok and s_ok:
            ic_c = float(ic_ok["holdout"].get("calmar") or 0)
            s_c = float(s_ok["holdout"].get("calmar") or 0)
            promoted = ic_ok if ic_c >= s_c - CALMAR_TOL else s_ok
        else:
            promoted = ic_ok or s_ok
        report["holdout"] = {"candidates": promo}
        report["promotion"] = {
            "promoted_variant": promoted["variant"] if promoted else None,
            "reason": (
                "IC preferred when holdout Calmar within 0.05 of straddle"
                if promoted and str(promoted.get("structure", "")).startswith("ic")
                else ("straddle holdout pass" if promoted else "no variant passed holdout gates")
            ),
            "detail": promoted,
        }

    (root / "winners.json").write_text(
        json.dumps({"winners": winners, "selection_end": SELECTION_END}, indent=2), encoding="utf-8"
    )
    (root / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    lines = [
        f"# Selective Overlay — Phase {args.phase.upper()} Summary",
        "",
        f"Selection <= `{SELECTION_END}` | Holdout >= `{HOLDOUT_START}` (sealed until --promote)",
        "",
        "## Production reference (selection)",
        "",
        f"- CAGR {ref_sel.get('cagr_pct')} | Calmar {ref_sel.get('calmar')} | "
        f"MaxDD {ref_sel.get('max_drawdown_pct')} | Worst {ref_sel.get('worst_day_pct')}",
        "",
        "## Top selection (overlay n≥20)",
        "",
        "| Rank | Variant | Struct | Calmar | ΔCalmar | Overlay n | Overlay PnL | LowVolBlk | FeeBlk |",
        "|---:|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for i, r in enumerate(ranked[:20], 1):
        lines.append(
            f"| {i} | {r['variant']} | {r.get('structure')} | {r.get('calmar')} | "
            f"{r.get('calmar_delta_vs_ref')} | {r.get('overlay_trades')} | {r.get('overlay_pnl')} | "
            f"{r.get('low_vol_blocked_days')} | {r.get('fee_blocked_days')} |"
        )
    lines += ["", "## Frozen winners (for Phase C/D)", ""]
    for w in winners:
        s = sel_by.get(w["variant"], {})
        lines.append(
            f"- **{w['role']}**: `{w['variant']}` — sel Calmar {s.get('calmar')}, "
            f"overlay n={s.get('overlay_trades')}, overlay PnL={s.get('overlay_pnl')}"
        )

    if report.get("promotion"):
        lines += ["", "## Holdout promotion", ""]
        p = report["promotion"]
        lines.append(f"- Promoted: `{p.get('promoted_variant')}`")
        lines.append(f"- Reason: {p.get('reason')}")
        for c in report.get("holdout", {}).get("candidates", []):
            lines.append(
                f"- `{c['variant']}` holdout_pass={c['holdout_pass']} checks={c['checks']} "
                f"hold Calmar={c['holdout'].get('calmar') if c.get('holdout') else None}"
            )

    lines += [
        "",
        "## Notes",
        "",
        "- IC variants enforce VIX≥15 and min credit vs fees by default (low-vol fee drag).",
        "- Rank on selection only; holdout used solely with `--promote`.",
        "",
    ]
    text = "\n".join(lines)
    (root / "SUMMARY.md").write_text(text, encoding="utf-8")
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", errors="replace").decode("ascii"))
    print(f"\nWrote {root / 'winners.json'}, {root / 'report.json'}, {root / 'SUMMARY.md'}")


if __name__ == "__main__":
    main()
