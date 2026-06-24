# Strategy Improvement Run

Date recorded: 2026-06-22

## Implemented

- Tightened the exploratory sleeve default from score 2.00 to 2.25.
- Added optional time-of-day controls:
  - early entries require higher score
  - late core entries require higher score
  - exploratory entries are blocked after the exploratory cutoff
  - final-hour entries require minimum distance from spot
  - same-side late re-entry is blocked after an earlier same-side stop
- Added optional event-aware controls:
  - event bucket is passed from `regime_expansion_dates_2025.csv` into each daily simulation
  - exploratory sleeve is blocked on shock buckets by default
  - scheduled macro days can require stronger exploratory scores
  - exploratory entries can be blocked on rich-premium plus term-dislocated days
- Added validation switches in `simulator/regime_validation.py`:
  - `--time-of-day-controls`
  - `--event-controls`
  - `--exploratory-min-score`
  - `--event-shock-buckets`
  - `--scheduled-macro-buckets`
  - `--scheduled-macro-exploratory-min-score`

## Validation Runs

Sample: 60 processed validation days with rolling 40-day baselines.

| Run | Trades | Stops | Stop Rate | Halted Days | Gross Credit Sold | Net P&L | Worst Day |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Core 2.50 baseline | 9 | 1 | 11.1% | 0 | $167,850 | $145,686.34 | $0.00 |
| Original two-tier | 43 | 15 | 34.9% | 0 | $216,375 | $57,110.71 | -$37,947.40 |
| Event-aware two-tier, min 2.25 | 20 | 5 | 25.0% | 0 | $193,150 | $151,888.87 | -$11,008.98 |
| Event-aware two-tier, min 2.30 | 17 | 4 | 23.5% | 0 | $184,435 | $169,921.27 | $0.00 |
| Time + event controls, min 2.25 | 16 | 4 | 25.0% | 0 | $142,460 | $122,752.65 | -$14,823.70 |

## Readout

The best frequency-preserving improvement is event-aware two-tier with exploratory minimum score 2.25:

- It reaches the 20-trade lower bound.
- It improves net P&L versus the conservative core by $6,202.53.
- It keeps halted days at zero.
- It cuts worst day from the original two-tier's -$37,947.40 to -$11,008.98.
- It keeps the exploratory sleeve positive: 11 trades, +$6,202.53.

The best raw P&L variant is event-aware two-tier with exploratory minimum score 2.30:

- Net P&L improves to $169,921.27.
- Worst day is $0.00 on this sparse sample.
- It falls below the frequency target with 17 trades.

The 3.0x stop variants did not help. They reduced frequency and lowered P&L without solving the stop-rate issue.

## Current Recommendation

Use event-aware two-tier with exploratory minimum score 2.25 as the next working baseline because it is the only tested variant that both improves P&L and reaches the target trade-frequency floor.

Remaining gap:

- Stop rate is still 25.0%, above the 15% target.

Next improvement should not be more score tuning on this sparse sample. The next meaningful step is to add the MBH-like structure layer: iron condors, butterflies/broken-wing butterflies, and integrated long-vol hedges. That gives the strategy more ways to express premium richness without forcing marginal vertical-spread entries.
