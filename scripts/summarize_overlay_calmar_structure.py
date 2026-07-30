"""Rank overlay Calmar structure results; freeze winners; write promotion memo."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from overlay_calmar_variants import (  # noqa: E402
    HOLDOUT_START,
    SELECTION_END,
    save_winners,
)

OUT = ROOT / "data" / "overlay_calmar_structure"

# Promotion gates vs prod-only (holdout)
CALMAR_TOL = 0.05
CAGR_TOL_PP = 0.25
WORST_TOL_PP = 0.50
MAXDD_TOL_PP = 0.50
MIN_OVERLAY_N = 20


def _load(path: Path) -> List[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def _rank_key(row: dict) -> Tuple:
    return (
        -float(row.get("calmar") or 0),
        -float(row.get("cagr_pct") or 0),
        -float(row.get("worst_day_pct") or 0),  # less negative is larger
        -int(row.get("overlay_trades") or 0),
    )


def _is_ic(name: str) -> bool:
    n = name.lower()
    return n.startswith("ic_") or n.startswith("p0_ic") or "_ic_" in n


def _is_straddle(name: str) -> bool:
    n = name.lower()
    return n.startswith("s_") or n.startswith("p0_s")


def _width_from_name(name: str) -> Optional[float]:
    m = re.search(r"w(\d+)", name)
    return float(m.group(1)) if m else None


def _stop_from_name(name: str) -> Optional[float]:
    if name in ("P0_s_eod", "S_eod") or name.endswith("_eod") or "_eod_" in name:
        return None
    if "2x" in name or name.endswith("_2x"):
        return 2.0
    m = re.search(r"_k([0-9p]+)", name)
    if not m:
        m = re.search(r"^S_k([0-9p]+)", name)
    if not m:
        return None
    token = m.group(1).replace("p", ".")
    try:
        return float(token)
    except ValueError:
        return None


def _passes_floors(row: dict, *, require_overlay: bool) -> bool:
    if require_overlay and int(row.get("overlay_trades") or 0) < MIN_OVERLAY_N:
        return False
    return True


def _promotion_ok(cand: dict, prod: dict) -> Tuple[bool, List[str]]:
    reasons = []
    ok = True
    if float(cand["calmar"]) < float(prod["calmar"]) - CALMAR_TOL:
        ok = False
        reasons.append(
            f"Calmar {cand['calmar']:.3f} < prod {prod['calmar']:.3f} - {CALMAR_TOL}"
        )
    if float(cand["cagr_pct"]) < float(prod["cagr_pct"]) - CAGR_TOL_PP:
        ok = False
        reasons.append(
            f"CAGR {cand['cagr_pct']:.2f} < prod {prod['cagr_pct']:.2f} - {CAGR_TOL_PP}"
        )
    # worst day: cand more negative than prod by > tol fails
    if float(cand["worst_day_pct"]) < float(prod["worst_day_pct"]) - WORST_TOL_PP:
        ok = False
        reasons.append(
            f"Worst {cand['worst_day_pct']:.2f} < prod {prod['worst_day_pct']:.2f} - {WORST_TOL_PP}"
        )
    if float(cand["max_drawdown_pct"]) > float(prod["max_drawdown_pct"]) + MAXDD_TOL_PP:
        ok = False
        reasons.append(
            f"MaxDD {cand['max_drawdown_pct']:.2f} > prod {prod['max_drawdown_pct']:.2f} + {MAXDD_TOL_PP}"
        )
    if ok:
        reasons.append("PASS")
    return ok, reasons


def _fmt_row(r: dict) -> str:
    return (
        f"| `{r['variant']}` | {r.get('calmar', 0):.3f} | {r.get('cagr_pct', 0):.2f}% | "
        f"{r.get('max_drawdown_pct', 0):.2f}% | {r.get('worst_day_pct', 0):.2f}% | "
        f"{r.get('sharpe', 0):.2f} | {r.get('overlay_trades', 0)} | "
        f"${r.get('overlay_pnl', 0):,.0f} | {r.get('calmar_delta_vs_ref', 0):+.3f} |"
    )


def summarize_phase(phase: str) -> Dict[str, Any]:
    root = OUT / phase.lower()
    sel = _load(root / "summary_selection.json")
    hold = _load(root / "summary_holdout.json")
    full = _load(root / "summary_full.json")
    by_sel = {r["variant"]: r for r in sel}
    by_hold = {r["variant"]: r for r in hold}
    by_full = {r["variant"]: r for r in full}

    prod = by_sel.get("P0_prod") or next(r for r in sel if r["variant"] in ("P0_prod", "B0_prod_only"))
    prod_hold = by_hold.get(prod["variant"], prod)

    ic_rows = [
        r
        for r in sel
        if _is_ic(r["variant"]) and _passes_floors(r, require_overlay=True)
    ]
    s_rows = [
        r
        for r in sel
        if _is_straddle(r["variant"]) and _passes_floors(r, require_overlay=True)
    ]
    ic_rows.sort(key=_rank_key)
    s_rows.sort(key=_rank_key)

    best_ic = ic_rows[0]["variant"] if ic_rows else None
    best_s = s_rows[0]["variant"] if s_rows else None

    # Top 2 IC widths from P1-style names
    width_best: Dict[float, dict] = {}
    for r in ic_rows:
        w = _width_from_name(r["variant"])
        if w is None:
            continue
        if w not in width_best or _rank_key(r) < _rank_key(width_best[w]):
            width_best[w] = r
    top_widths = [
        w
        for w, _ in sorted(width_best.items(), key=lambda kv: _rank_key(kv[1]))[:2]
    ]

    stop_best: Dict[Any, dict] = {}
    for r in s_rows:
        if "_c2" in r["variant"] or "tp50" in r["variant"] or "flat14" in r["variant"]:
            continue  # base stop family only for P2 freeze
        if not r["variant"].startswith("S_") and not r["variant"].startswith("P0_s"):
            continue
        stop = _stop_from_name(r["variant"])
        key = stop if stop is not None else "eod"
        if key not in stop_best or _rank_key(r) < _rank_key(stop_best[key]):
            stop_best[key] = r
    top_stop_rows = sorted(stop_best.values(), key=_rank_key)[:2]
    top_stops: List[Optional[float]] = []
    for r in top_stop_rows:
        top_stops.append(_stop_from_name(r["variant"]))

    freeze_ic = [r["variant"] for r in ic_rows[:2]]
    freeze_s = [r["variant"] for r in s_rows[:2]]

    # Preserve prior GRID freezes when a later phase has no IC/straddle base names.
    prior_path = OUT / "winners.json"
    prior: Dict[str, Any] = {}
    if prior_path.is_file():
        try:
            prior = json.loads(prior_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            prior = {}

    if not best_ic:
        best_ic = prior.get("best_ic")
    if not best_s:
        best_s = prior.get("best_straddle")
    if not freeze_ic:
        freeze_ic = list(prior.get("freeze_ic_variants") or [])
    if not freeze_s:
        freeze_s = list(prior.get("freeze_straddle_variants") or [])
    if not top_widths:
        top_widths = list(prior.get("p1_top_widths") or [])
    if not top_stops:
        top_stops = list(prior.get("p2_top_stops") or [])

    winners = {
        "selection_end": SELECTION_END,
        "holdout_start": HOLDOUT_START,
        "prod_variant": prod["variant"],
        "best_ic": best_ic,
        "best_straddle": best_s,
        "freeze_ic_variants": freeze_ic,
        "freeze_straddle_variants": freeze_s,
        "p1_top_widths": top_widths,
        "p2_top_stops": top_stops,
        "p2_soft_stops": [s for s in top_stops if s is not None][:2] or [1.5, 2.0],
        "selection_top_ic": ic_rows[:10],
        "selection_top_straddle": s_rows[:10],
        "source_phase": phase,
    }

    # Holdout promotion on frozen
    promo = []
    for name in [best_ic, best_s]:
        if not name or name not in by_hold:
            continue
        ok, reasons = _promotion_ok(by_hold[name], prod_hold)
        prefer_note = ""
        if name == best_s and best_ic and best_ic in by_hold:
            if abs(float(by_hold[name]["calmar"]) - float(by_hold[best_ic]["calmar"])) <= 0.05:
                prefer_note = "IC preferred on Calmar tie (±0.05)"
                if ok:
                    ok = False
                    reasons.append(prefer_note)
        promo.append(
            {
                "variant": name,
                "promote": ok,
                "reasons": reasons,
                "holdout": by_hold[name],
                "selection": by_sel.get(name),
            }
        )

    decide = "KEEP P0_ic50 / current production overlay"
    promoted = [p for p in promo if p["promote"]]
    if promoted:
        # prefer IC if both
        ic_p = [p for p in promoted if _is_ic(p["variant"])]
        decide = f"PROMOTE {ic_p[0]['variant']}" if ic_p else f"PROMOTE {promoted[0]['variant']}"

    lines = [
        f"# Overlay Calmar Structure — Phase `{phase}`",
        "",
        f"Selection ≤ `{SELECTION_END}` | Holdout ≥ `{HOLDOUT_START}`",
        "",
        f"**Decision bias output:** {decide}",
        "",
        "## Production baseline (selection)",
        "",
        f"- Variant: `{prod['variant']}`",
        f"- CAGR {prod['cagr_pct']:.2f}% | Sharpe {prod['sharpe']:.2f} | "
        f"MaxDD {prod['max_drawdown_pct']:.2f}% | Worst {prod['worst_day_pct']:.2f}% | "
        f"Calmar {prod['calmar']:.3f}",
        "",
        "## Top IC (selection Calmar)",
        "",
        "| Variant | Calmar | CAGR | MaxDD | Worst | Sharpe | Ov n | Ov PnL | ΔCalmar |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in ic_rows[:15]:
        lines.append(_fmt_row(r))
    lines += [
        "",
        "## Top Straddle (selection Calmar)",
        "",
        "| Variant | Calmar | CAGR | MaxDD | Worst | Sharpe | Ov n | Ov PnL | ΔCalmar |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in s_rows[:15]:
        lines.append(_fmt_row(r))

    lines += ["", "## Holdout — frozen candidates", ""]
    for p in promo:
        h = p["holdout"]
        lines.append(
            f"### `{p['variant']}` — {'PROMOTE' if p['promote'] else 'NO'}"
        )
        lines.append("")
        lines.append(
            f"- Holdout Calmar {h['calmar']:.3f} | CAGR {h['cagr_pct']:.2f}% | "
            f"MaxDD {h['max_drawdown_pct']:.2f}% | Worst {h['worst_day_pct']:.2f}% | "
            f"Ov PnL ${h.get('overlay_pnl', 0):,.0f}"
        )
        lines.append(f"- Gates: {'; '.join(p['reasons'])}")
        lines.append("")

    lines += [
        "## Full-sample snapshot (info only)",
        "",
        "| Variant | Calmar | CAGR | MaxDD | Worst | Ov PnL |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name in [prod["variant"], best_ic, best_s, "P0_ic50", "IC_w50_d12"]:
        if name and name in by_full:
            r = by_full[name]
            lines.append(
                f"| `{name}` | {r['calmar']:.3f} | {r['cagr_pct']:.2f}% | "
                f"{r['max_drawdown_pct']:.2f}% | {r['worst_day_pct']:.2f}% | "
                f"${r.get('overlay_pnl', 0):,.0f} |"
            )

    summary_md = "\n".join(lines) + "\n"
    (root / "SUMMARY.md").write_text(summary_md, encoding="utf-8")
    report = {
        "phase": phase,
        "decision": decide,
        "winners": winners,
        "promotion": promo,
        "prod_selection": prod,
        "prod_holdout": prod_hold,
    }
    (root / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    save_winners(OUT / "winners.json", winners)
    print(summary_md, flush=True)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True)
    args = parser.parse_args()
    summarize_phase(args.phase)


if __name__ == "__main__":
    main()
