"""Phase A: counterfactual short ATM straddle PnL sliced by feature quintiles.

Uses productionized book features (z-scored walk-forward). Does not change
vertical trading — diagnostic only. Rank/report on selection; holdout sealed.

  python scripts/run_phase_a_straddle_diagnostics.py --resume
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time as time_mod
from collections import defaultdict
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
SIM = ROOT / "simulator"
sys.path.insert(0, str(SIM))

from expiry_calendar import (  # noqa: E402
    DEFAULT_RULES,
    discover_eligible_dates,
    load_era_rules,
    resolve_start_date,
)
from long_vol_overlay import choose_atm_straddle, group_quotes, spot as overlay_spot  # noqa: E402
from mbh_simulator import OptionQuote, read_quotes_csv, read_signals_csv  # noqa: E402
from profiles import PRODUCTION_TRAIN_COUNT  # noqa: E402
from regime_validation import apply_rolling_baseline, discover_dates  # noqa: E402

PROCESSED = ROOT / "data" / "processed"
OUT = ROOT / "data" / "selective_straddle_overlay" / "phase_a"
TRAIN = PRODUCTION_TRAIN_COUNT
SELECTION_END = "2023-12-29"
HOLDOUT_START = "2024-01-02"
FEATURES = ["straddle_residual_z", "term_ratio_z", "trend_score", "realized_vs_implied_z"]


def _intrinsic(option_type: str, strike: float, close_spot: float) -> float:
    if option_type == "CALL":
        return max(close_spot - strike, 0.0)
    return max(strike - close_spot, 0.0)


def counterfactual_straddle(
    quotes: Sequence[OptionQuote],
    entry_ts: datetime,
) -> Optional[dict]:
    by_ts = group_quotes(quotes)
    if entry_ts not in by_ts:
        # nearest at/after
        later = [t for t in sorted(by_ts) if t >= entry_ts]
        if not later:
            return None
        entry_ts = later[0]
    selected = choose_atm_straddle(by_ts[entry_ts])
    if selected is None:
        return None
    call_q, put_q = selected
    entry_credit = call_q.bid + put_q.bid
    if entry_credit <= 0:
        return None
    close_ts = sorted(by_ts)[-1]
    close_spot = overlay_spot(by_ts[close_ts])
    exit_debit = _intrinsic("CALL", call_q.strike, close_spot) + _intrinsic("PUT", put_q.strike, close_spot)
    # also mark stop-style path
    stopped = False
    stop_debit = None
    stop_level = 2.0 * entry_credit
    for ts in sorted(by_ts):
        if ts < entry_ts:
            continue
        snap = by_ts[ts]
        calls = [q for q in snap if q.option_type == "CALL" and q.strike == call_q.strike]
        puts = [q for q in snap if q.option_type == "PUT" and q.strike == put_q.strike]
        if not calls or not puts:
            continue
        debit = calls[0].ask + puts[0].ask
        if debit >= stop_level:
            stopped = True
            stop_debit = debit
            break
    pnl_eod = entry_credit - exit_debit
    pnl_stop = entry_credit - (stop_debit if stop_debit is not None else exit_debit)
    return {
        "entry_ts": entry_ts.isoformat(),
        "entry_credit": round(entry_credit, 4),
        "pnl_eod": round(pnl_eod, 4),
        "pnl_stop2x": round(pnl_stop, 4),
        "stopped": stopped,
    }


def signal_at(signals, ts: datetime):
    by = {s.timestamp: s for s in signals}
    if ts in by:
        return by[ts]
    earlier = [t for t in sorted(by) if t <= ts]
    return by[earlier[-1]] if earlier else None


def quintile_edges(values: List[float]) -> List[float]:
    if not values:
        return [0.0, 0.0, 0.0, 0.0]
    xs = sorted(values)
    n = len(xs)

    def q(p: float) -> float:
        idx = min(n - 1, max(0, int(p * (n - 1))))
        return xs[idx]

    return [q(0.2), q(0.4), q(0.6), q(0.8)]


def assign_quintile(value: float, edges: List[float]) -> int:
    for i, e in enumerate(edges):
        if value <= e:
            return i + 1
    return 5


def save_ckpt(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)


def load_ckpt(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def summarize_slices(rows: List[dict], period: str) -> List[dict]:
    out = []
    for feat in FEATURES:
        vals = [float(r[feat]) for r in rows]
        edges = quintile_edges(vals)
        buckets: Dict[int, List[dict]] = defaultdict(list)
        for r in rows:
            buckets[assign_quintile(float(r[feat]), edges)].append(r)
        for q in range(1, 6):
            grp = buckets.get(q, [])
            if not grp:
                continue
            n = len(grp)
            out.append(
                {
                    "period": period,
                    "feature": feat,
                    "quintile": q,
                    "n": n,
                    "edge_hi": edges[q - 1] if q <= 4 else None,
                    "mean_feature": round(sum(float(r[feat]) for r in grp) / n, 4),
                    "mean_pnl_eod": round(sum(float(r["pnl_eod"]) for r in grp) / n, 4),
                    "mean_pnl_stop2x": round(sum(float(r["pnl_stop2x"]) for r in grp) / n, 4),
                    "win_rate_eod": round(sum(1 for r in grp if float(r["pnl_eod"]) > 0) / n, 4),
                    "stop_rate": round(sum(1 for r in grp if r["stopped"]) / n, 4),
                }
            )
    return out


def run(*, resume: bool = False, checkpoint_every: int = 25, max_oos: int = 0) -> None:
    floor, eras = load_era_rules(DEFAULT_RULES)
    processed_dates = discover_dates(PROCESSED, "SPXW")
    resolved_start = resolve_start_date(processed_dates, floor, require_mon_and_wed=True)
    eligible = discover_eligible_dates(
        processed_dates, floor=resolved_start, end=processed_dates[-1], eras=eras
    )
    oos_total = len(eligible) - TRAIN
    if max_oos > 0:
        oos_total = min(oos_total, max_oos)

    OUT.mkdir(parents=True, exist_ok=True)
    ckpt_path = OUT / "checkpoint.json"
    rows: List[dict] = []
    start = 0
    if resume:
        ckpt = load_ckpt(ckpt_path)
        if ckpt and ckpt.get("complete") and int(ckpt.get("oos_total", 0)) == oos_total:
            print("Phase A already complete.", flush=True)
            _finalize(ckpt["rows"])
            return
        if ckpt and int(ckpt.get("oos_done", 0)) > 0:
            rows = ckpt["rows"]
            start = int(ckpt["oos_done"])
            print(f"Resume Phase A at {start}/{oos_total}", flush=True)

    print(f"Phase A straddle diagnostics: {oos_total} OOS days", flush=True)
    t0 = time_mod.time()
    for oos_i in range(start, oos_total):
        index = TRAIN + oos_i
        test_date = eligible[index]
        train_dates = eligible[index - TRAIN : index]
        apply_rolling_baseline(PROCESSED, "SPXW", train_dates, test_date, "signals_unconditional.csv")
        day_dir = PROCESSED / "symbol=SPXW" / f"date={test_date}"
        quotes = read_quotes_csv(day_dir / "normalized_option_quotes.csv")
        signals = read_signals_csv(day_dir / "signals_unconditional.csv")
        # Enter at first signal >= 10:00
        entry_ts = None
        for s in signals:
            if s.timestamp.time() >= time(10, 0):
                entry_ts = s.timestamp
                break
        if entry_ts is None:
            continue
        sig = signal_at(signals, entry_ts)
        cf = counterfactual_straddle(quotes, entry_ts)
        if cf is None or sig is None:
            continue
        rows.append(
            {
                "date": test_date,
                "straddle_residual_z": round(sig.straddle_residual_z, 6),
                "term_ratio_z": round(sig.term_ratio_z, 6),
                "trend_score": round(sig.trend_score, 6),
                "realized_vs_implied_z": round(sig.realized_vs_implied_z, 6),
                "abs_trend": round(abs(sig.trend_score), 6),
                **cf,
            }
        )
        done = oos_i + 1
        if done % 50 == 0 or done == oos_total:
            print(f"  {done}/{oos_total} ({test_date})", flush=True)
        if checkpoint_every > 0 and (done % checkpoint_every == 0 or done == oos_total):
            save_ckpt(
                ckpt_path,
                {
                    "oos_done": done,
                    "oos_total": oos_total,
                    "complete": done == oos_total,
                    "last_date": test_date,
                    "rows": rows,
                },
            )
    _finalize(rows)
    print(f"Phase A done in {(time_mod.time() - t0) / 60:.1f} min", flush=True)


def _finalize(rows: List[dict]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    sel = [r for r in rows if r["date"] <= SELECTION_END]
    hold = [r for r in rows if r["date"] >= HOLDOUT_START]
    slices = summarize_slices(sel, "selection") + summarize_slices(hold, "holdout")
    with (OUT / "straddle_rows.csv").open("w", newline="", encoding="utf-8") as handle:
        if rows:
            w = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    with (OUT / "quintile_summary.csv").open("w", newline="", encoding="utf-8") as handle:
        if slices:
            w = csv.DictWriter(handle, fieldnames=list(slices[0].keys()))
            w.writeheader()
            w.writerows(slices)
    (OUT / "quintile_summary.json").write_text(json.dumps(slices, indent=2), encoding="utf-8")

    lines = [
        "# Phase A — Counterfactual short ATM straddle by feature quintile",
        "",
        f"Selection <= `{SELECTION_END}` | Holdout >= `{HOLDOUT_START}` (holdout for transparency only)",
        f"Rows: selection {len(sel)}, holdout {len(hold)}",
        "",
        "Positive mean_pnl_eod = short straddle made money to settlement.",
        "",
    ]
    for period in ("selection", "holdout"):
        lines.append(f"## {period}")
        lines.append("")
        lines.append("| Feature | Q | N | Mean feat | Mean PnL EOD | Mean PnL stop2x | Win% | Stop% |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
        for row in slices:
            if row["period"] != period:
                continue
            lines.append(
                f"| {row['feature']} | {row['quintile']} | {row['n']} | {row['mean_feature']} | "
                f"{row['mean_pnl_eod']} | {row['mean_pnl_stop2x']} | {row['win_rate_eod']} | {row['stop_rate']} |"
            )
        lines.append("")
    # Highlight monotonic-ish residual on selection
    lines.append("## Selection takeaways (auto)")
    for feat in FEATURES:
        feat_rows = [r for r in slices if r["period"] == "selection" and r["feature"] == feat]
        if len(feat_rows) >= 2:
            first, last = feat_rows[0]["mean_pnl_eod"], feat_rows[-1]["mean_pnl_eod"]
            direction = "higher feature → better short-straddle" if last > first else "higher feature → worse short-straddle"
            lines.append(f"- `{feat}`: Q1 PnL {first} vs Q5 {last} ({direction})")
    (OUT / "SUMMARY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--checkpoint-every", type=int, default=25)
    parser.add_argument("--max-oos-days", type=int, default=0)
    args = parser.parse_args()
    run(resume=args.resume, checkpoint_every=args.checkpoint_every, max_oos=args.max_oos_days)


if __name__ == "__main__":
    main()
