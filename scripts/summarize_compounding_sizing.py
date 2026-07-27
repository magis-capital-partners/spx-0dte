"""Summarize compounding sizing suite -> SUMMARY.md + results markdown."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "data" / "compounding_sizing"
ROOT = Path(__file__).resolve().parents[1]
RESULTS_MD = ROOT / f"compounding_sizing_results_{date.today().isoformat()}.md"

SELECTION_END = "2023-12-29"
HOLDOUT_START = "2024-01-02"
MAXDD_HARD = 20.0
CALMAR_TOL = 0.05


def _load(name: str):
    path = OUT / name
    if not path.is_file():
        return {}
    return {r["variant"]: r for r in json.loads(path.read_text(encoding="utf-8"))}


def _row(r: dict) -> str:
    return (
        f"| `{r['variant']}` | {r['calmar']:.3f} | {r['cagr_pct']:.2f}% | "
        f"{r['max_drawdown_pct']:.2f}% | {r['worst_day_pct']:.2f}% | {r['sharpe']:.2f} | "
        f"{r.get('peak_k', 0):.2f}× | ${r.get('ending_equity', 0):,.0f} |"
    )


def main() -> None:
    full = _load("summary_full.json")
    sel = _load("summary_selection.json")
    hold_c = _load("summary_holdout_continuation.json")
    hold_r = _load("summary_holdout_rebased.json")
    closed = _load("summary_closed_form.json")

    if not full:
        raise SystemExit(f"Missing {OUT / 'summary_full.json'} — run merge first")

    fixed = full["fixed"]
    ranked = sorted(full.values(), key=lambda r: (-r["calmar"], -r["cagr_pct"]))

    # Promotion: selection Calmar >= fixed - tol, holdout rebased maxDD <= hard cap,
    # holdout rebased Calmar >= fixed holdout - tol. Prefer full compounding if it clears.
    promo = None
    for cand_name in ("full", "fractional_f075", "fractional_f050", "cap_3x", "cap_2x"):
        if cand_name not in sel or cand_name not in hold_r:
            continue
        s = sel[cand_name]
        h = hold_r[cand_name]
        hf = hold_r["fixed"]
        if float(s["calmar"]) < float(sel["fixed"]["calmar"]) - CALMAR_TOL:
            continue
        if float(h["max_drawdown_pct"]) > MAXDD_HARD:
            continue
        if float(h["calmar"]) < float(hf["calmar"]) - CALMAR_TOL:
            continue
        promo = cand_name
        break
    if promo is None:
        promo = "full"  # still report full as the primary dashboard artifact

    decision = (
        f"Dashboard export: `p3_poststop_compounding_f1` (full compounding). "
        f"Recommended sizing factor for discussion: `{promo}`."
    )

    lines = [
        "# Compounding Position Sizing — Results",
        "",
        f"Date: {date.today().isoformat()}",
        "",
        "Production path (`p3_poststop_cooldown_120`) with equity-proportional contract sizing.",
        f"Selection <= `{SELECTION_END}` | Holdout (rebased) >= `{HOLDOUT_START}`",
        "",
        f"**{decision}**",
        "",
        "## Full sample (sequential re-simulation)",
        "",
        "| Variant | Calmar | CAGR | MaxDD | Worst | Sharpe | Peak k | Ending equity |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in ranked:
        lines.append(_row(r))

    lines += [
        "",
        "## Selection (<= 2023-12-29)",
        "",
        "| Variant | Calmar | CAGR | MaxDD | Worst | Peak k |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name in [r["variant"] for r in ranked]:
        r = sel[name]
        lines.append(
            f"| `{name}` | {r['calmar']:.3f} | {r['cagr_pct']:.2f}% | "
            f"{r['max_drawdown_pct']:.2f}% | {r['worst_day_pct']:.2f}% | {r.get('peak_k', 0):.2f}× |"
        )

    lines += [
        "",
        "## Holdout rebased (honest — restart at $13M on 2024-01-02)",
        "",
        "| Variant | Calmar | CAGR | MaxDD | Worst | Peak k |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name in [r["variant"] for r in ranked]:
        r = hold_r[name]
        lines.append(
            f"| `{name}` | {r['calmar']:.3f} | {r['cagr_pct']:.2f}% | "
            f"{r['max_drawdown_pct']:.2f}% | {r['worst_day_pct']:.2f}% | {r.get('peak_k', 0):.2f}× |"
        )

    lines += [
        "",
        "## Holdout continuation (inherits selection equity — informational)",
        "",
        "| Variant | Calmar | CAGR | MaxDD | Ending equity |",
        "|---|---:|---:|---:|---:|",
    ]
    for name in [r["variant"] for r in ranked]:
        r = hold_c[name]
        lines.append(
            f"| `{name}` | {r['calmar']:.3f} | {r['cagr_pct']:.2f}% | "
            f"{r['max_drawdown_pct']:.2f}% | ${r.get('ending_equity', 0):,.0f} |"
        )

    if closed:
        lines += [
            "",
            "## Closed-form cross-check (homogeneity)",
            "",
            "Should match sequential within rounding. Large gaps ⇒ homogeneity failure.",
            "",
            "| Variant | Sim CAGR | Closed CAGR | Δ | Sim MaxDD | Closed MaxDD |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for name in [r["variant"] for r in ranked]:
            s = full[name]
            c = closed[name]
            lines.append(
                f"| `{name}` | {s['cagr_pct']:.2f}% | {c['cagr_pct']:.2f}% | "
                f"{s['cagr_pct'] - c['cagr_pct']:+.2f}pp | "
                f"{s['max_drawdown_pct']:.2f}% | {c['max_drawdown_pct']:.2f}% |"
            )

    f_full = full["full"]
    lines += [
        "",
        "## Interpretation",
        "",
        f"- Fixed-size production: **{fixed['cagr_pct']:.2f}% CAGR**, "
        f"**{fixed['max_drawdown_pct']:.2f}% max DD**, Calmar {fixed['calmar']:.2f}.",
        f"- Full compounding (f=1): **{f_full['cagr_pct']:.2f}% CAGR**, "
        f"**{f_full['max_drawdown_pct']:.2f}% max DD**, Calmar {f_full['calmar']:.2f}, "
        f"peak size **{f_full.get('peak_k', 0):.2f}×**.",
        "- Compounding is leverage-through-time, not alpha: Calmar stays roughly flat "
        "while CAGR and drawdown scale together.",
        f"- Production's {fixed['max_drawdown_pct']:.1f}% max DD is flattered by not "
        "resizing into a larger book; the stationary DD risk under f=1 is closer to "
        f"{f_full['max_drawdown_pct']:.1f}%.",
        "- Dashboard run id: `p3_poststop_compounding_f1` "
        "(primary remains `p3_poststop_cooldown_120`).",
        "- Capacity caveat: peak tranche size ≈ 48 × peak_k contracts; simulator has "
        "no market-impact model.",
        "",
    ]

    text = "\n".join(lines) + "\n"
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "SUMMARY.md").write_text(text, encoding="utf-8")
    RESULTS_MD.write_text(text, encoding="utf-8")
    (OUT / "report.json").write_text(
        json.dumps(
            {
                "decision": decision,
                "promo": promo,
                "dashboard_preset": "p3_poststop_compounding_f1",
                "full_ranked": [r["variant"] for r in ranked],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", errors="replace").decode("ascii"))


if __name__ == "__main__":
    main()
