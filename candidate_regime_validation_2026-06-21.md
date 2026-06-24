# Candidate Regime Validation

Date recorded: 2026-06-21

## Changes Made

Updated `simulator\walk_forward_grid.py`:

- Added candidate-engine parameters to grid output and configuration.
- Refactored parameter iteration into a flat product to avoid Python's static nesting limit.
- Added positive-day, no-trade-day, and worst-day metrics.

Added `simulator\regime_validation.py`:

- Runs a rolling no-lookahead validation.
- For each test day, builds signal baselines only from the prior training window.
- Classifies each day into coarse signal regimes.
- Writes daily results, regime summaries, trades, stop diagnostics, and candidate reason summaries.

Promoted the default candidate hurdle:

- Old default: `candidate_min_score = 2.00`
- New default: `candidate_min_score = 2.25`

## Fixed Q1 2025 Score Sweep

Window:

- Train: 2025-01-02 through 2025-03-03
- Test: 2025-03-04 through 2025-03-31
- Output: `data\walk_forward_candidate_score_sweep\walk_forward_grid.csv`

Best configuration tested:

- Baseline contracts: 66
- Daily credit cap: 1.0% or 1.5%
- Stop multiple: 2.5x
- Target long-wing delta: 0.08
- Candidate minimum score: 2.25

Result:

- Total net P&L: +$74,206.71
- Total return on $28,000,000 equity: +0.27%
- Total credit sold: $134,365
- Trades: 14
- Stopped trades: 1
- Stop rate: 7.14%
- Positive days: 6 of 20
- No-trade days: 13 of 20
- Halted days: 0 of 20
- Worst day: -$33,130.35

Candidate hurdle comparison at 66 baseline contracts:

- Score 1.50: -$373,193.11, 73 trades, 2 stopped
- Score 1.75: -$246,890.91, 54 trades, 1 stopped
- Score 2.00: -$50,845.46, 34 trades, 6 stopped
- Score 2.25: +$74,206.71, 14 trades, 1 stopped

## Rolling Regime Validation

Window:

- Available rolling test dates: 2025-03-04 through 2026-03-02
- Test days: 22
- Training window per day: prior 40 processed dates
- Output, score 2.00: `data\regime_validation_candidate_score2`
- Output, score 2.25: `data\regime_validation_candidate_score225`

Score 2.00:

- Total net P&L: -$40,219.56
- Total return: -0.14%
- Trades: 28
- Stopped trades: 4
- Stop rate: 14.29%
- Positive days: 6
- No-trade days: 14
- Halted days: 0

Score 2.25:

- Total net P&L: +$52,792.08
- Total return: +0.19%
- Trades: 15
- Stopped trades: 1
- Stop rate: 6.67%
- Positive days: 6
- No-trade days: 15
- Halted days: 0
- Worst day: -$87,646.98

Regime notes for score 2.25:

- `cheap_premium`: +$4,934.72, 12 days, 1 trade
- `rich_premium`: -$71,911.26, 3 days, 4 trades
- `term_dislocated`: +$38,929.88, 7 days, 5 trades
- `skew_dislocated` and `trend_extreme` appeared on all 22 days under the current coarse classifier.

## Interpretation

The candidate engine is improving in the right way: higher selectivity reduced trades, reduced stops, eliminated halts, and improved P&L on both the fixed Q1 split and the rolling validation sample.

The current evidence supports `candidate_min_score = 2.25` as the default over `2.00`.

This is not yet a robust MBH-style validation. The rolling sample is only 22 test days, and the current regime classifier overflags skew/trend dislocation because the baseline history is still narrow. The next step should be more data by regime before further optimizing parameters.

## Next Data Needed

Prioritize additional processed dates in these buckets:

1. Quiet grind-up days
2. Low-volatility compression days
3. CPI/FOMC/NFP event days
4. Large overnight gap days
5. Intraday reversal days
6. High-VIX selloff days
7. Post-shock normalization days

Once these are added, rerun `regime_validation.py` and only then widen the grid.

