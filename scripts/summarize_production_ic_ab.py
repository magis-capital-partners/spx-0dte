"""Summarize production-path IC A/B and write FINAL_REPORT.md."""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "data" / "production_ic_ab"
SELECTION_END = "2023-12-29"
HOLDOUT_START = "2024-01-02"

CALMAR_TOL = 0.05
CAGR_TOL = 0.25
WORST_TOL = 0.50
MAXDD_TOL = 0.50


def _load(name: str):
    return {r["variant"]: r for r in json.loads((OUT / name).read_text(encoding="utf-8"))}


def _ok(cand: dict, ctrl: dict) -> bool:
    return (
        float(cand["calmar"]) >= float(ctrl["calmar"]) - CALMAR_TOL
        and float(cand["cagr_pct"]) >= float(ctrl["cagr_pct"]) - CAGR_TOL
        and float(cand["worst_day_pct"]) >= float(ctrl["worst_day_pct"]) - WORST_TOL
        and float(cand["max_drawdown_pct"]) <= float(ctrl["max_drawdown_pct"]) + MAXDD_TOL
    )


def _row(r: dict) -> str:
    return (
        f"| `{r['variant']}` | {r['calmar']:.3f} | {r['cagr_pct']:.2f}% | "
        f"{r['max_drawdown_pct']:.2f}% | {r['worst_day_pct']:.2f}% | {r['sharpe']:.2f} | "
        f"${r['ic_pnl']:,.0f} | {r['ic_days']} | "
        f"{r.get('calmar_delta_vs_ic50', 0):+.3f} |"
    )


def main() -> None:
    sel = _load("summary_selection.json")
    hold = _load("summary_holdout.json")
    full = _load("summary_full.json")

    no_ic = sel["no_ic"]
    ctrl = sel["ic_w50_d12"]
    ranked = sorted(sel.values(), key=lambda r: (-r["calmar"], -r["cagr_pct"]))

    # Best that beats current IC50 on selection Calmar and passes holdout vs no_ic + vs ic50
    promo = None
    for r in ranked:
        if r["variant"] in ("no_ic",):
            continue
        h = hold[r["variant"]]
        if r["calmar"] + 1e-9 < ctrl["calmar"]:
            continue
        if _ok(h, hold["no_ic"]) and _ok(h, hold["ic_w50_d12"]):
            promo = r["variant"]
            break
    if promo is None:
        # looser: pass vs no_ic only and beat ic50 on selection
        for r in ranked:
            if r["variant"] == "no_ic":
                continue
            if r["calmar"] <= ctrl["calmar"]:
                continue
            if _ok(hold[r["variant"]], hold["no_ic"]):
                promo = r["variant"]
                break

    decision = (
        f"PROMOTE `{promo}` on production path"
        if promo and promo != "ic_w50_d12"
        else "KEEP `ic_w50_d12` (current production IC)"
    )

    lines = [
        "# Production-path IC A/B — Final Report",
        "",
        "Engine: `build_p3_poststop_cooldown_config` + `simulate_day` / `select_condor_entries` "
        "(live-parity path). Not the selective-overlay picker.",
        "",
        f"Selection ≤ `{SELECTION_END}` | Holdout ≥ `{HOLDOUT_START}`",
        "",
        f"**Decision: {decision}**",
        "",
        "## Selection ranking (Calmar)",
        "",
        "| Variant | Calmar | CAGR | MaxDD | Worst | Sharpe | IC PnL | IC days | ΔCalmar vs IC50 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in ranked:
        lines.append(_row(r))

    lines += [
        "",
        "## Holdout (sealed)",
        "",
        "| Variant | Calmar | CAGR | MaxDD | Worst | IC PnL | vs no_ic | vs IC50 |",
        "|---|---:|---:|---:|---:|---:|---|---|",
    ]
    for name in [r["variant"] for r in ranked]:
        h = hold[name]
        vs_no = "PASS" if _ok(h, hold["no_ic"]) else "FAIL"
        vs50 = "PASS" if _ok(h, hold["ic_w50_d12"]) else "FAIL"
        lines.append(
            f"| `{name}` | {h['calmar']:.3f} | {h['cagr_pct']:.2f}% | {h['max_drawdown_pct']:.2f}% | "
            f"{h['worst_day_pct']:.2f}% | ${h['ic_pnl']:,.0f} | {vs_no} | {vs50} |"
        )

    lines += [
        "",
        "## Full sample (info)",
        "",
        "| Variant | Calmar | CAGR | MaxDD | Worst | IC PnL |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name in [r["variant"] for r in ranked]:
        f = full[name]
        lines.append(
            f"| `{name}` | {f['calmar']:.3f} | {f['cagr_pct']:.2f}% | {f['max_drawdown_pct']:.2f}% | "
            f"{f['worst_day_pct']:.2f}% | ${f['ic_pnl']:,.0f} |"
        )

    lines += [
        "",
        "## Interpretation",
        "",
        f"- Verticals-only Calmar (selection): **{no_ic['calmar']:.3f}**",
        f"- Current IC50 Δ0.12 Calmar (selection): **{ctrl['calmar']:.3f}** "
        f"(IC sleeve ${ctrl['ic_pnl']:,.0f} over {ctrl['ic_days']} days)",
        "",
    ]
    if promo and promo != "ic_w50_d12":
        p = sel[promo]
        ph = hold[promo]
        lines.append(
            f"- Winner `{promo}`: selection Calmar {p['calmar']:.3f} "
            f"(+{p['calmar']-ctrl['calmar']:+.3f} vs IC50), holdout Calmar {ph['calmar']:.3f}."
        )
        lines.append(f"- Recommend updating production `condor_wing_width` / `condor_target_abs_delta` to match `{promo}`.")
    else:
        lines.append(
            "- No width/delta beat current IC50 on selection Calmar **and** cleared holdout gates "
            "vs both no-IC and IC50 — **keep production IC at 50pt / Δ0.12**."
        )
        # note best selection even if not promoted
        best = next(r for r in ranked if r["variant"] != "no_ic")
        if best["variant"] != "ic_w50_d12":
            lines.append(
                f"- Best selection Calmar was `{best['variant']}` ({best['calmar']:.3f}) but "
                f"failed promotion gates or did not clear holdout vs IC50."
            )

    lines.append("")
    text = "\n".join(lines) + "\n"
    (OUT / "FINAL_REPORT.md").write_text(text, encoding="utf-8")
    (OUT / "report.json").write_text(
        json.dumps(
            {
                "decision": decision,
                "promo": promo,
                "selection_ranked": [r["variant"] for r in ranked],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(text)


if __name__ == "__main__":
    main()
