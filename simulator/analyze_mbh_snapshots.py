"""Reverse-engineer MBH structure from posted position snapshots.

Parses position_snapshots.csv and, for each snapshot, infers:
- short vs long legs per side (sign convention: negative = short, positive = long)
- short-strike band and implied spot (gap between top short put and bottom short call)
- wing width per side (short leg to its protective long leg)
- long/short contract ratio (detects net-long tail-hedge skew)
- approx structure description vs our 25-wide single-vertical default
"""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path
from statistics import mean, median
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOTS = ROOT / "position_snapshots.csv"


def read_rows() -> List[dict]:
    with SNAPSHOTS.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def legs_for_source(rows: List[dict]):
    """Return (short_puts, long_puts, short_calls, long_calls) as {strike: qty}."""
    short_puts: Dict[float, int] = {}
    long_puts: Dict[float, int] = {}
    short_calls: Dict[float, int] = {}
    long_calls: Dict[float, int] = {}
    for row in rows:
        strike = float(row["strike"])
        calls = int(row["call_contracts"])
        puts = int(row["put_contracts"])
        if puts < 0:
            short_puts[strike] = short_puts.get(strike, 0) + abs(puts)
        elif puts > 0:
            long_puts[strike] = long_puts.get(strike, 0) + puts
        if calls < 0:
            short_calls[strike] = short_calls.get(strike, 0) + abs(calls)
        elif calls > 0:
            long_calls[strike] = long_calls.get(strike, 0) + calls
    return short_puts, long_puts, short_calls, long_calls


def weighted_mean_strike(legs: Dict[float, int]) -> float:
    total = sum(legs.values())
    if not total:
        return 0.0
    return sum(strike * qty for strike, qty in legs.items()) / total


def analyze_source(source: str, rows: List[dict]) -> str:
    short_puts, long_puts, short_calls, long_calls = legs_for_source(rows)
    out: List[str] = [f"\n{'='*70}", f"SNAPSHOT: {source}", f"{'='*70}"]

    n_sp = sum(short_puts.values())
    n_lp = sum(long_puts.values())
    n_sc = sum(short_calls.values())
    n_lc = sum(long_calls.values())

    # Implied spot: between top short put and bottom short call
    top_short_put = max(short_puts) if short_puts else None
    bot_short_call = min(short_calls) if short_calls else None
    implied_spot = None
    if top_short_put and bot_short_call:
        implied_spot = (top_short_put + bot_short_call) / 2

    out.append(f"\nContract totals:")
    out.append(f"  Short puts : {n_sp:>6}   across {len(short_puts)} strikes")
    out.append(f"  Long puts  : {n_lp:>6}   across {len(long_puts)} strikes")
    out.append(f"  Short calls: {n_sc:>6}   across {len(short_calls)} strikes")
    out.append(f"  Long calls : {n_lc:>6}   across {len(long_calls)} strikes")
    out.append(f"  Total distinct strikes (legs): {len(short_puts)+len(long_puts)+len(short_calls)+len(long_calls)}")

    out.append(f"\nLong/short ratio (tail-hedge skew):")
    out.append(f"  Put side  long/short: {n_lp/n_sp:.2f}x" if n_sp else "  Put side: no shorts")
    out.append(f"  Call side long/short: {n_lc/n_sc:.2f}x" if n_sc else "  Call side: no shorts")

    if implied_spot:
        out.append(f"\nImplied spot ~ {implied_spot:.0f}  (top short put {top_short_put:.0f} / bottom short call {bot_short_call:.0f}, gap {bot_short_call-top_short_put:.0f})")

    # Short-strike distance from implied spot
    if implied_spot:
        if short_puts:
            sp_strikes = sorted(short_puts)
            wm_sp = weighted_mean_strike(short_puts)
            out.append(f"\nShort PUT band: {min(sp_strikes):.0f} – {max(sp_strikes):.0f}")
            out.append(f"  wtd-avg short put strike {wm_sp:.0f}  => {(implied_spot-wm_sp)/implied_spot*100:.2f}% below spot")
            out.append(f"  nearest short put {max(sp_strikes):.0f} => {(implied_spot-max(sp_strikes))/implied_spot*100:.2f}% OTM")
            out.append(f"  furthest short put {min(sp_strikes):.0f} => {(implied_spot-min(sp_strikes))/implied_spot*100:.2f}% OTM")
        if short_calls:
            sc_strikes = sorted(short_calls)
            wm_sc = weighted_mean_strike(short_calls)
            out.append(f"\nShort CALL band: {min(sc_strikes):.0f} – {max(sc_strikes):.0f}")
            out.append(f"  wtd-avg short call strike {wm_sc:.0f}  => {(wm_sc-implied_spot)/implied_spot*100:.2f}% above spot")
            out.append(f"  nearest short call {min(sc_strikes):.0f} => {(min(sc_strikes)-implied_spot)/implied_spot*100:.2f}% OTM")

    # Wing width inference: distance from short band to long band per side
    if short_puts and long_puts:
        wm_sp = weighted_mean_strike(short_puts)
        wm_lp = weighted_mean_strike(long_puts)
        out.append(f"\nPut wing width (wtd-avg short {wm_sp:.0f} - wtd-avg long {wm_lp:.0f}) = {wm_sp-wm_lp:.0f} pts")
        out.append(f"  nearest-long-to-nearest-short: {max(short_puts)-max(long_puts):.0f} pts" )
    if short_calls and long_calls:
        wm_sc = weighted_mean_strike(short_calls)
        wm_lc = weighted_mean_strike(long_calls)
        out.append(f"Call wing width (wtd-avg long {wm_lc:.0f} - wtd-avg short {wm_sc:.0f}) = {wm_lc-wm_sc:.0f} pts")
        out.append(f"  nearest-long-to-nearest-short: {min(long_calls)-min(short_calls):.0f} pts")

    return "\n".join(out)


def main() -> None:
    rows = read_rows()
    by_source: Dict[str, List[dict]] = defaultdict(list)
    for row in rows:
        by_source[row["source"]].append(row)

    print("MBH POSITION SNAPSHOT ANALYSIS")
    print("Sign convention: negative contracts = SHORT (sold), positive = LONG (owned)")

    for source in by_source:
        print(analyze_source(source, by_source[source]))

    print(f"\n{'='*70}")
    print("OUR CURRENT DEFAULT (for comparison)")
    print(f"{'='*70}")
    print("  Structure : single bull-put OR bear-call vertical per tranche")
    print("  Short delta: 0.20 (band 0.15-0.25)")
    print("  Wing      : target 0.05-0.08 long delta, 25-400 pt width")
    print("  Long/short ratio: 1.0x (no extra tail hedge)")
    print("  Stop      : short-leg ask at 2.0x entry credit, retain long wing")


if __name__ == "__main__":
    main()
