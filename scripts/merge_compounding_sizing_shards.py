"""Merge compounding sizing shard checkpoints into summary tables."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))
sys.path.insert(0, str(ROOT / "scripts"))

from compounding_sizing import analytic_path, build_variants  # noqa: E402
from portfolio_metrics import portfolio_stats  # noqa: E402
from profiles import PRODUCTION_ACCOUNT_EQUITY  # noqa: E402
from run_compounding_sizing_suite import (  # noqa: E402
    HOLDOUT_START,
    OUT,
    SELECTION_END,
    load_checkpoint,
    work_dir,
)

ACCOUNT = PRODUCTION_ACCOUNT_EQUITY


def filter_daily(
    daily: List[dict], *, end: Optional[str] = None, start: Optional[str] = None
) -> List[dict]:
    out = []
    for row in daily:
        d = str(row.get("date") or "")
        if end is not None and d > end:
            continue
        if start is not None and d < start:
            continue
        out.append(row)
    return out


def summarize(name: str, daily: List[dict], period: str) -> dict:
    port = portfolio_stats(daily, ACCOUNT, metrics_mode="eligible_only")
    max_dd = float(port.get("max_drawdown_pct") or 0)
    cagr = float(port.get("cagr_pct") or 0)
    calmar = round(cagr / max_dd, 4) if max_dd > 0 else 0.0
    ks = [float(r.get("k") or 1) for r in daily]
    peak_k = max(ks) if ks else 1.0
    end_k = ks[-1] if ks else 1.0
    # Prefer equity-open-relative worst day when available (compounding paths).
    worst_pct = float(port.get("worst_day_pct") or 0)
    eo_worst = []
    for r in daily:
        eo = float(r.get("equity_open") or 0)
        if eo > 0:
            eo_worst.append(float(r.get("net_pnl") or 0) / eo * 100.0)
    if eo_worst:
        worst_pct = round(min(eo_worst), 2)
        port = {**port, "worst_day_pct": worst_pct}
    return {
        "period": period,
        "variant": name,
        "n_days": len(daily),
        **port,
        "calmar": calmar,
        "peak_k": round(peak_k, 4),
        "ending_k": round(end_k, 4),
    }


def holdout_rebased(name: str, fixed_holdout_returns: List[float]) -> dict:
    """Honest sealed holdout: restart k at 1.0× from HOLDOUT_START via closed form."""
    k_of = build_variants(ACCOUNT)[name].k_of
    pnls, equities, ks = analytic_path(fixed_holdout_returns, k_of, e0=ACCOUNT)
    daily = [
        {
            "date": f"holdout_{i:04d}",
            "eligible": True,
            "trades": 1,
            "stopped_trades": 0,
            "net_pnl": pnl,
            "k": k,
            "equity_open": eq,
        }
        for i, (pnl, eq, k) in enumerate(zip(pnls, equities, ks))
    ]
    row = summarize(name, daily, "holdout_rebased")
    row["n_days"] = len(fixed_holdout_returns)
    return row


def write_summary(path: Path, rows: List[dict]) -> None:
    path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    if not rows:
        return
    fields: List[str] = []
    seen = set()
    for row in rows:
        for k in row:
            if k not in seen:
                seen.add(k)
                fields.append(k)
    with path.with_suffix(".csv").open("w", newline="", encoding="utf-8") as handle:
        w = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shards", type=int, default=8)
    args = parser.parse_args()

    names = list(build_variants(ACCOUNT).keys())
    daily_by: Dict[str, List[dict]] = {}

    if args.shards <= 1:
        ckpt = load_checkpoint(OUT / "checkpoint.json")
        if not ckpt or not ckpt.get("complete"):
            raise SystemExit("Incomplete single-process checkpoint")
        daily_by = ckpt["daily_by"]
    else:
        for shard in range(args.shards):
            ckpt = load_checkpoint(work_dir(shard, args.shards) / "checkpoint.json")
            if not ckpt or not ckpt.get("complete"):
                raise SystemExit(
                    f"Shard {shard} incomplete "
                    f"(variants={ckpt.get('variants') if ckpt else 'missing'})"
                )
            for name, daily in ckpt["daily_by"].items():
                daily_by[name] = daily

    missing = [n for n in names if n not in daily_by]
    if missing:
        raise SystemExit(f"Missing variants after merge: {missing}")

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "daily_by.json").write_text(
        json.dumps({k: daily_by[k] for k in names}, separators=(",", ":")),
        encoding="utf-8",
    )
    for name in names:
        fields = list(daily_by[name][0].keys()) if daily_by[name] else []
        with (OUT / f"daily_{name}.csv").open("w", newline="", encoding="utf-8") as handle:
            w = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(daily_by[name])

    fixed_full = daily_by["fixed"]
    fixed_returns = [
        float(r["net_pnl"]) / ACCOUNT for r in fixed_full if ACCOUNT
    ]
    holdout_fixed = filter_daily(fixed_full, start=HOLDOUT_START)
    holdout_returns = [float(r["net_pnl"]) / ACCOUNT for r in holdout_fixed]

    for period, start, end in (
        ("full", None, None),
        ("selection", None, SELECTION_END),
        ("holdout_continuation", HOLDOUT_START, None),
    ):
        rows = []
        ref_period = summarize(
            "fixed", filter_daily(fixed_full, start=start, end=end), period
        )
        for name in names:
            daily = filter_daily(daily_by[name], start=start, end=end)
            row = summarize(name, daily, period)
            row["calmar_delta_vs_fixed"] = round(row["calmar"] - ref_period["calmar"], 4)
            row["cagr_delta_vs_fixed"] = round(
                float(row["cagr_pct"]) - float(ref_period["cagr_pct"]), 2
            )
            rows.append(row)
        rows.sort(key=lambda r: (-float(r["calmar"]), -float(r["cagr_pct"])))
        write_summary(OUT / f"summary_{period}.json", rows)

    # Sealed holdout: rebased at E0 using fixed returns × each k policy.
    rebased = []
    for name in names:
        row = holdout_rebased(name, holdout_returns)
        rebased.append(row)
    rebased.sort(key=lambda r: (-float(r["calmar"]), -float(r["cagr_pct"])))
    write_summary(OUT / "summary_holdout_rebased.json", rebased)

    # Closed-form cross-check on full sample.
    closed = []
    for name in names:
        k_of = build_variants(ACCOUNT)[name].k_of
        pnls, _, ks = analytic_path(fixed_returns, k_of, e0=ACCOUNT)
        daily = [
            {
                "date": fixed_full[i]["date"],
                "eligible": True,
                "trades": int(fixed_full[i].get("trades") or 0),
                "stopped_trades": int(fixed_full[i].get("stopped_trades") or 0),
                "net_pnl": pnl,
                "k": k,
            }
            for i, (pnl, k) in enumerate(zip(pnls, ks))
        ]
        closed.append(summarize(name, daily, "closed_form"))
    closed.sort(key=lambda r: (-float(r["calmar"]), -float(r["cagr_pct"])))
    write_summary(OUT / "summary_closed_form.json", closed)

    print(f"Merged compounding sizing -> {OUT}", flush=True)


if __name__ == "__main__":
    main()
