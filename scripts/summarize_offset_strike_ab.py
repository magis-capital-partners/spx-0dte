"""Summarize offset-strike A/B and write FINAL_REPORT.md."""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "data" / "offset_strike_ab"
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


def _fmt(r: dict) -> str:
    credit = r.get("mean_vert_credit")
    credit_s = f"{credit:.3f}" if credit is not None else "—"
    return (
        f"| `{r['variant']}` | {r['calmar']:.3f} | {r['cagr_pct']:.2f}% | "
        f"{r['max_drawdown_pct']:.2f}% | {r['worst_day_pct']:.2f}% | {r['sharpe']:.2f} | "
        f"{r['vert_trades']} | {r['vert_stop_rate']:.2%} | {credit_s} | "
        f"{r.get('calmar_delta_vs_prod', 0):+.3f} |"
    )


def main() -> None:
    sel = _load("summary_selection.json")
    hold = _load("summary_holdout.json")
    full = _load("summary_full.json")

    prod_s = sel["prod"]
    off_s = sel["offset_1_otm"]
    prod_h = hold["prod"]
    off_h = hold["offset_1_otm"]

    promote = _ok(off_h, prod_h) and float(off_s["calmar"]) >= float(prod_s["calmar"]) - CALMAR_TOL
    decision = (
        "PROCEED toward unique-strike + native 3x STP (`offset_1_otm` acceptable)"
        if promote
        else "DO NOT force OTM offset — keep same-strike synthetic stops"
    )

    lines = [
        "# Offset-Strike Vertical A/B — Final Report",
        "",
        "Substrate: production `p3_poststop_cooldown_120` (IC Δ0.16 / 10-lot unchanged).",
        "Treatment: after delta short pick, put short −1 listed strike / call short +1 "
        "(further OTM), re-pick fixed-width wing.",
        "",
        f"Selection ≤ `{SELECTION_END}` | Holdout ≥ `{HOLDOUT_START}`",
        "",
        f"**Decision: {decision}**",
        "",
        "## Selection",
        "",
        "| Variant | Calmar | CAGR | MaxDD | Worst | Sharpe | Vert n | Stop% | Mean credit | ΔCalmar |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        _fmt(prod_s),
        _fmt(off_s),
        "",
        f"- Puts/calls (prod): {prod_s['put_trades']} / {prod_s['call_trades']}",
        f"- Puts/calls (offset): {off_s['put_trades']} / {off_s['call_trades']}",
        "",
        "## Holdout",
        "",
        "| Variant | Calmar | CAGR | MaxDD | Worst | Vert n | Stop% | vs prod gates |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
        (
            f"| `prod` | {prod_h['calmar']:.3f} | {prod_h['cagr_pct']:.2f}% | "
            f"{prod_h['max_drawdown_pct']:.2f}% | {prod_h['worst_day_pct']:.2f}% | "
            f"{prod_h['vert_trades']} | {prod_h['vert_stop_rate']:.2%} | — |"
        ),
        (
            f"| `offset_1_otm` | {off_h['calmar']:.3f} | {off_h['cagr_pct']:.2f}% | "
            f"{off_h['max_drawdown_pct']:.2f}% | {off_h['worst_day_pct']:.2f}% | "
            f"{off_h['vert_trades']} | {off_h['vert_stop_rate']:.2%} | "
            f"{'PASS' if _ok(off_h, prod_h) else 'FAIL'} |"
        ),
        "",
        "## Full sample",
        "",
        "| Variant | Calmar | CAGR | MaxDD | Worst | Vert n | Stop% |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name in ("prod", "offset_1_otm"):
        f = full[name]
        lines.append(
            f"| `{name}` | {f['calmar']:.3f} | {f['cagr_pct']:.2f}% | "
            f"{f['max_drawdown_pct']:.2f}% | {f['worst_day_pct']:.2f}% | "
            f"{f['vert_trades']} | {f['vert_stop_rate']:.2%} |"
        )

    lines += [
        "",
        "## Gates (holdout vs prod)",
        "",
        f"- Calmar ≥ prod − {CALMAR_TOL}: "
        f"{off_h['calmar']:.3f} vs {prod_h['calmar']:.3f}",
        f"- CAGR ≥ prod − {CAGR_TOL}pp: "
        f"{off_h['cagr_pct']:.2f} vs {prod_h['cagr_pct']:.2f}",
        f"- Worst not worse by > {WORST_TOL}pp: "
        f"{off_h['worst_day_pct']:.2f} vs {prod_h['worst_day_pct']:.2f}",
        f"- MaxDD not worse by > {MAXDD_TOL}pp: "
        f"{off_h['max_drawdown_pct']:.2f} vs {prod_h['max_drawdown_pct']:.2f}",
        "",
    ]

    text = "\n".join(lines) + "\n"
    (OUT / "FINAL_REPORT.md").write_text(text, encoding="utf-8")
    (OUT / "report.json").write_text(
        json.dumps(
            {
                "decision": decision,
                "promote": promote,
                "selection": {"prod": prod_s, "offset_1_otm": off_s},
                "holdout": {"prod": prod_h, "offset_1_otm": off_h},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(text)


if __name__ == "__main__":
    main()
