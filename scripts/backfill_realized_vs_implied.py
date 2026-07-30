"""Backfill raw realized_vs_implied into processed signals.csv from quotes.

Does not re-download data. Updates signals.csv in place and deletes stale
signals_unconditional.csv so walk-forward z-scores are recomputed.

  python scripts/backfill_realized_vs_implied.py
  python scripts/backfill_realized_vs_implied.py --workers 8
"""
from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
SIM = ROOT / "simulator"
sys.path.insert(0, str(SIM))

from rv_feature import atm_iv_from_pair, realized_vs_implied_raw  # noqa: E402

PROCESSED = ROOT / "data" / "processed"


def discover_dates(processed_dir: Path, symbol: str) -> List[str]:
    root = processed_dir / f"symbol={symbol}"
    dates = []
    for path in root.iterdir():
        if path.is_dir() and path.name.startswith("date=") and (path / "signals.csv").exists():
            dates.append(path.name.split("=", 1)[1])
    return sorted(dates)


def _mid(bid: float, ask: float) -> float:
    if bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    return math.nan


def _spot_and_atm_by_ts(quotes_path: Path, session: str) -> Dict[str, Tuple[float, float, Optional[float]]]:
    """timestamp -> (spot, straddle, atm_iv) for 0DTE rows."""
    by_ts: Dict[str, List[dict]] = defaultdict(list)
    with quotes_path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            if str(row.get("expiry") or "")[:10] != session:
                continue
            by_ts[row["timestamp"]].append(row)

    out: Dict[str, Tuple[float, float, Optional[float]]] = {}
    for ts, rows in by_ts.items():
        spots = [float(r["underlying_price"]) for r in rows if r.get("underlying_price") not in ("", None)]
        if not spots:
            continue
        spot = spots[0]
        calls = [r for r in rows if str(r.get("option_type") or "").upper() in {"CALL", "C"}]
        puts = [r for r in rows if str(r.get("option_type") or "").upper() in {"PUT", "P"}]
        if not calls or not puts:
            continue
        atm = min({float(r["strike"]) for r in rows}, key=lambda k: abs(k - spot))
        call = min(calls, key=lambda r: (abs(float(r["strike"]) - atm), abs(abs(float(r.get("delta") or 0)) - 0.5)))
        put = min(puts, key=lambda r: (abs(float(r["strike"]) - atm), abs(abs(float(r.get("delta") or 0)) - 0.5)))
        straddle = _mid(float(call["bid"]), float(call["ask"])) + _mid(float(put["bid"]), float(put["ask"]))
        if not math.isfinite(straddle) or straddle <= 0:
            continue
        iv = atm_iv_from_pair(
            float(call["iv"]) if call.get("iv") not in ("", None) else None,
            float(put["iv"]) if put.get("iv") not in ("", None) else None,
        )
        out[ts] = (spot, straddle, iv)
    return out


def backfill_one(day_dir: Path) -> Tuple[str, int, float, float]:
    session = day_dir.name.split("=", 1)[1]
    signals_path = day_dir / "signals.csv"
    quotes_path = day_dir / "normalized_option_quotes.csv"
    if not quotes_path.is_file():
        return session, 0, 0.0, 0.0

    atm_by_ts = _spot_and_atm_by_ts(quotes_path, session)
    with signals_path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
        fieldnames = list(rows[0].keys()) if rows else []
    if not rows:
        return session, 0, 0.0, 0.0
    if "realized_vs_implied_z" not in fieldnames:
        fieldnames.append("realized_vs_implied_z")

    spot_history: List[float] = []
    values: List[float] = []
    # Walk in timestamp order matching signals
    for row in sorted(rows, key=lambda r: r["timestamp"]):
        ts = row["timestamp"]
        info = atm_by_ts.get(ts)
        if info is None:
            # fallback spot from signal row
            try:
                spot = float(row.get("underlying_price") or 0)
                straddle = float(row.get("straddle") or 0)
            except (TypeError, ValueError):
                spot, straddle = 0.0, 0.0
            iv = None
        else:
            spot, straddle, iv = info
        if spot > 0:
            spot_history.append(spot)
        raw = realized_vs_implied_raw(spot_history, spot=spot, straddle=straddle, atm_iv=iv)
        row["realized_vs_implied_z"] = f"{raw:.8f}"
        values.append(raw)

    with signals_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda r: r["timestamp"]))

    # Force re-zscore on next walk-forward
    unc = day_dir / "signals_unconditional.csv"
    if unc.exists():
        unc.unlink()

    nonzero = [v for v in values if abs(v) > 1e-12]
    mean_abs = sum(abs(v) for v in nonzero) / len(nonzero) if nonzero else 0.0
    return session, len(nonzero), (min(values) if values else 0.0), (max(values) if values else 0.0)


def _worker(date_str: str) -> Tuple[str, int, float, float]:
    return backfill_one(PROCESSED / "symbol=SPXW" / f"date={date_str}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="SPXW")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--max-dates", type=int, default=0)
    args = parser.parse_args()

    dates = discover_dates(PROCESSED, args.symbol)
    if args.max_dates > 0:
        dates = dates[: args.max_dates]
    print(f"Backfilling realized_vs_implied on {len(dates)} days with {args.workers} workers...", flush=True)

    ok = 0
    nonzero_days = 0
    if args.workers <= 1:
        results = [_worker(d) for d in dates]
    else:
        results = []
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            futs = {pool.submit(_worker, d): d for d in dates}
            for i, fut in enumerate(as_completed(futs), 1):
                results.append(fut.result())
                if i % 100 == 0:
                    print(f"  {i}/{len(dates)}", flush=True)

    for session, n_nz, mn, mx in results:
        ok += 1
        if n_nz > 0:
            nonzero_days += 1
    print(
        f"Done: {ok} days, {nonzero_days} with non-zero RV feature. "
        f"Deleted stale signals_unconditional.csv where present.",
        flush=True,
    )


if __name__ == "__main__":
    main()
