# MBH Snapshot Reverse-Engineering — 2026-06-30

Follow-up to Test 1 (unconditional baseline). Goal: determine whether our stop-loss
mechanism and strike selection match what MBH actually does, using the three posted
position snapshots in `position_snapshots.csv`.

Analyzer: `simulator/analyze_mbh_snapshots.py`
Sign convention: negative contracts = SHORT (sold), positive = LONG (owned).

## Snapshot structure

| Snapshot | Short puts | Long puts | Short calls | Long calls | Put L/S | Call L/S | Legs |
|---|---:|---:|---:|---:|---:|---:|---:|
| user_snapshot | 308 | 520 | 338 | 513 | 1.69x | 1.52x | 33 |
| DDQ 2026-03-02 11:00 | 264 | 264 | 66 | 66 | 1.00x | 1.00x | 13 |
| DDQ 2026-03-02 15:00 | 1,204 | 1,820 | 206 | 379 | 1.51x | 1.84x | 49 |

## Findings

### 1. MBH runs a NET-LONG book (1.5–1.8x more longs than shorts)
By midday the book holds far more long options than short. This is the "4–8
long-volatility strategies layered daily as dynamic hedges" from the spec, made
concrete. The book starts balanced (1.0x at 11:00) and accumulates net-long
convexity through the day (1.5–1.8x by 15:00). **Our simulator runs 1.0x — pure
defined-risk verticals with no convex overlay.**

### 2. Wings are WIDE and asymmetric
- Put wing width: 176–289 pts (wtd-avg short to wtd-avg long)
- Call wing width: 49–122 pts
- Put wings are 3–10x wider than our ~25-pt default, and much wider than call wings.

MBH buys bulk far-OTM put protection (cheap downside convexity) and keeps call
wings tighter. Our symmetric ~25-wide vertical does not resemble this.

### 3. Short strikes ≈ right on delta, but MBH does NOT stop out
- Morning shorts sit ~1.1–1.3% OTM (consistent with ~15–25Δ on 0DTE; our 20Δ is fine).
- 15:00 snapshot: short puts sitting 0.15% OTM (≈ ATM) and STILL OPEN.

A short that drifts from ~1.3% OTM to 0.15% OTM has moved hard against the seller.
Our 2.0x short-leg-ask stop would have closed nearly all of those — MBH did not.
**MBH is not running a tight per-trade stop the way we model it.** Their risk
control is the net-long wing structure + delta-neutral laddering + daily loss
limit, not aggressive per-position stops.

### 4. Heavy laddering / scale-into-day
Legs grow 13 → 49 and short puts grow 264 → 1,204 from 11:00 to 15:00. MBH ladders
across many strikes and scales the book up through the session. We place one 2-leg
vertical per tranche.

## Connection to Test 1
Test 1 showed 100% of losses come from the 2.0x short-leg stop (3,670 stopped
trades at -$8,683 each; settled-at-close trades were +$6,033 at 99.5% win). The
snapshots independently confirm MBH avoids that failure mode: they do not stop
near-money shorts; they carry net-long wings that gain as spot approaches the short.

## Proposed Test 2 (stop + structure matrix)
Test on the full 391 OOS days, unconditional cadence:
1. **No per-trade stop** (hold defined-risk to settlement) — baseline upper bound.
2. **2.0x / 2.5x / 3.0x** short-leg stop — current vs looser.
3. **Net-long overlay**: add extra long wings to reach ~1.5x put / ~1.8x call L/S.
4. **Wide asymmetric wings**: put wing ~200 pts, call wing ~75 pts.

Acceptance: improve expectancy/trade and Sharpe without worst-day exceeding
MBH's claimed ~4–5%.
