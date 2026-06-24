# Phase 1-6 Implementation Notes

Date recorded: 2026-06-21

## What Changed

Implemented phases 1-6 of the MBH reconstruction plan:

1. Side-and-strike candidate engine
2. Hard MBH-style expected-value gates
3. Side-specific put/call entry logic
4. Stop-path diagnostics
5. Rare long-volatility convexity overlay
6. March 2025 retest on the same failure window

## Simulator Changes

Updated `simulator\mbh_simulator.py`:

- Added `CandidateRecord` diagnostics for every evaluated spread candidate.
- Added side-specific candidate scoring for bull-put and bear-call spreads.
- Evaluates multiple short strikes at each entry time instead of selecting only by target delta proximity.
- Scores candidates using:
  - straddle residual / premium richness
  - put/call skew alignment
  - 0DTE vs next-expiration term-ratio dislocation
  - trend alignment
  - realized-vs-implied shock
  - credit-to-width
  - distance from spot
  - delta fit
  - stop-loss-to-credit ratio
- Added hard gates for:
  - cheap premium
  - term-structure dislocation
  - realized-volatility shock
  - extreme trend
  - adverse side-specific trend
  - adverse side-specific skew
- Default candidate hurdle is now `candidate_min_score = 2.00`.
- Default side selection now chooses the single best side per tranche unless explicitly configured otherwise.

Updated `simulator\run_reconstruction_backtest.py`:

- Writes `candidate_diagnostics.csv`.
- Writes `stop_diagnostics.csv`.
- Adds candidate and stopped-trade counts to daily summaries.
- Exposes candidate-engine parameters through command-line flags.

Updated `simulator\long_vol_overlay.py`:

- Added `put_spread` structure.
- Added `trigger_mode` with `any`, `confluence`, and `crash_hedge`.
- Tightened `crash_hedge` so it requires downside trend, term dislocation, and skew dislocation.
- Changed defaults toward rare convexity instead of frequent ATM-straddle buying.

## March 2025 Spread Retest

Test window: 2025-03-04 through 2025-03-31

Configuration:

- Account equity: $28,000,000
- Baseline contracts: 66
- Daily gross credit cap: 1.5%
- Stop multiple: 2.5x short premium
- Target long-wing absolute delta: 0.08
- Candidate minimum score: 2.00
- Max candidate sides per tranche: 1

Output folder:

- `data\candidate_engine_march2025_default`

Results:

- Days: 20
- Total net P&L: -$50,845.46
- Total return: -0.18%
- Mean daily return: -0.009%
- Total credit sold: $467,760
- Trades: 34
- Stopped trades: 6
- Stop rate: 17.65%
- Positive days: 7
- No-trade days: 9
- Halted days: 0
- Worst day: -$196,478.12

This is a major improvement versus the prior strict-filter run:

- Prior strict-filter result: -$2,419,273.88
- New candidate-engine result: -$50,845.46
- Prior stop rate: 39.4%
- New stop rate: 17.65%
- Prior halted days: 4
- New halted days: 0

## Long-Vol Overlay Retest

Output folder:

- `data\long_vol_crash_put_spread_march2025_tight`

Configuration:

- Structure: put spread
- Trigger mode: crash hedge
- Daily budget: 0.05%
- Max trades: 2
- Minimum minutes between trades: 60
- Straddle threshold: 1.5
- Term threshold: 1.5
- Trend threshold: 2.0
- Skew threshold: 1.5

Results:

- Days: 20
- Trades: 2
- Trade days: 2
- Total net P&L: +$12,184.58
- Total return: +0.04%
- Positive days: 1
- Negative days: 1
- Zero days: 18

## Combined Result

Spread engine plus rare put-spread overlay:

- Total net P&L: -$38,660.88
- Total return: -0.14%
- Positive days: 7
- Worst day: -$196,478.12

## Interpretation

The new side-and-strike candidate engine fixed the biggest failure mode from the earlier strict-filter run. The model now correctly skips more low-quality days and no longer hits daily loss halts in this March 2025 test window.

This is still not a finished MBH clone. It is one improved window, and the candidate score threshold may be overfit to March 2025. The next validation step is to rerun the same candidate engine over more regimes and then perform walk-forward calibration rather than optimizing on this single failure window.

## Next Work

1. Add candidate-engine parameters to the walk-forward grid.
2. Run walk-forward tests by market regime, not just by contiguous date ranges.
3. Add event-day labels for CPI, FOMC, NFP, and major tariff/geopolitical days.
4. Add tick or 10-second stop-fill testing around stopped trades.
5. Compare candidate diagnostics to MBH screenshots once received.

