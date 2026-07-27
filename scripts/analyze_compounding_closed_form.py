"""Closed-form compounding sizing preview from the production daily path.

Uses homogeneity: pnl_compound(t) = k(E_t) * pnl_fixed(t), with k from each policy.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))

from compounding_sizing import analytic_path, build_variants  # noqa: E402
from portfolio_metrics import portfolio_stats  # noqa: E402
from profiles import PRODUCTION_ACCOUNT_EQUITY  # noqa: E402

SRC = ROOT / "data" / "dashboard_runs" / "p3_poststop_cooldown_120" / "daily_summary.csv"
OUT = ROOT / "data" / "compounding_sizing"
ACCOUNT = PRODUCTION_ACCOUNT_EQUITY


def main() -> None:
    if not SRC.is_file():
        raise SystemExit(f"Missing production daily path: {SRC}")
    rows = [
        r
        for r in csv.DictReader(SRC.open(encoding="utf-8"))
        if str(r.get("eligible", "true")).lower() in {"true", "1"}
    ]
    fixed_returns = [float(r["net_pnl"]) / ACCOUNT for r in rows]
    variants = build_variants(ACCOUNT)

    results = []
    for name, variant in variants.items():
        pnls, equities, ks = analytic_path(fixed_returns, variant.k_of, e0=ACCOUNT)
        daily = [
            {
                "date": rows[i]["date"],
                "eligible": True,
                "trades": int(float(rows[i].get("trades") or 0)),
                "stopped_trades": int(float(rows[i].get("stopped_trades") or 0)),
                "net_pnl": pnl,
                "k": k,
                "equity_open": eq,
            }
            for i, (pnl, eq, k) in enumerate(zip(pnls, equities, ks))
        ]
        port = portfolio_stats(daily, ACCOUNT, metrics_mode="eligible_only")
        max_dd = float(port.get("max_drawdown_pct") or 0)
        cagr = float(port.get("cagr_pct") or 0)
        results.append(
            {
                "variant": name,
                "label": variant.label,
                **port,
                "calmar": round(cagr / max_dd, 4) if max_dd > 0 else 0.0,
                "peak_k": round(max(ks), 4),
                "ending_k": round(ks[-1], 4),
            }
        )

    results.sort(key=lambda r: (-r["calmar"], -r["cagr_pct"]))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "closed_form_preview.json").write_text(json.dumps(results, indent=2), encoding="utf-8")

    print(f"{len(rows)} eligible OOS days, E0=${ACCOUNT:,.0f}\n")
    print(f"{'variant':<22} {'mult':>6}  {'CAGR':>7}  {'MaxDD':>7}  {'Calmar':>6}  {'peak_k':>7}")
    for r in results:
        mult = float(r["ending_equity"]) / ACCOUNT
        print(
            f"{r['variant']:<22} {mult:6.2f}x  {r['cagr_pct']:6.2f}%  "
            f"{r['max_drawdown_pct']:6.2f}%  {r['calmar']:6.2f}  {r['peak_k']:7.2f}"
        )


if __name__ == "__main__":
    main()
