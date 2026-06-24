# P&L And Margin Iteration

Date: 2026-06-23

## What Was Done

1. Resumed targeted ThetaData 10-second downloads for stopped trades.
2. Switched remaining microstructure pulls to shorter around-stop windows to avoid multi-hour requests.
3. Classified the guarded run's stopped trades.
4. Tested time-of-day controls on top of the exploratory bear-call guard.
5. Tested a stricter exploratory score band.
6. Retested the small quiet-regime condor sleeve on top of the improved short-premium baseline.
7. Updated `simulator/continuous_research.py` so the current validation path uses the improved controls by default.
8. Added a capital deployment ladder to estimate whether scaling the improved baseline can close the MBH return gap.

## Download Status

Downloaded 10-second microstructure files are stored under:

`data/microstructure/thetadata/symbol=SPXW/date=YYYY-MM-DD/`

Completed useful downloads:

- Original removed exploratory bear-call stops:
  - 2025-06-03 CALL 5965, full entry-to-stop window, `sr30`
  - 2025-06-05 CALL 5995, full entry-to-stop window, `sr30`
- Guarded-run stopped trades:
  - 2025-06-30 CALL 6195, around-stop window, `sr10`
  - 2025-07-09 PUT 6240, around-stop window, `sr10`
  - 2025-07-09 CALL 6255, around-stop window, `sr10`
  - 2025-07-16 CALL 6260, around-stop window, `sr10`
  - 2025-07-30 PUT 6360, around-stop window, `sr10`
  - 2025-07-31 PUT 6340, around-stop window, `sr10`
  - 2025-08-07 PUT 6310, around-stop window, `sr10`
  - 2025-08-20 CALL 6375, around-stop window, `sr10`

The `sr10` downloads for 2025-07-31 and 2025-08-07 completed but did not include the stopped short strikes, so those two still need wider `sr30` or `sr50` retry if full classification is required.

## Microstructure Findings

Current guarded-run classification:

| Classification | Count |
| --- | ---: |
| real_directional_failure | 5 |
| fast_intraday_whipsaw | 1 |
| missing short strike in downloaded slice | 2 |

Interpretation:

- Most stopped trades were real directional failures, not quote artifacts.
- The June 30 stop was a fast late-day whipsaw and was blocked by time-of-day controls.
- The earlier removed June 3 and June 5 exploratory bear-call stops were also real directional failures, supporting the exploratory bear-call guard.

## Strategy Tests

All tests use the $13,000,000 account and the continuous Q2/Q3 2025 validation window.

| Run | Trades | Stops | Stop rate | Net P&L | Annualized return | Avg daily credit | Max margin | Max margin / equity |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Original event-aware two-tier | 34 | 11 | 32.4% | $5,959.81 | 0.13% | $986.40 | $141,360 | 1.09% |
| Bear-call guard | 27 | 8 | 29.6% | $11,412.48 | 0.26% | $893.31 | $141,360 | 1.09% |
| Bear-call guard + time controls | 24 | 7 | 29.2% | $19,538.87 | 0.44% | $741.40 | $141,360 | 1.09% |
| Guard + time controls + exploratory 2.40-2.49 | 16 | 4 | 25.0% | $22,525.75 | 0.51% | $668.37 | $141,360 | 1.09% |
| Same plus small quiet condor | 49 | 12 | 24.5% | $18,252.35 | 0.41% | $436.05 | $86,080 | 0.66% |

## Sleeve Attribution

### Current Best Absolute P&L Run

Run: `data/validation_13m_continuous_q2_q3_guard_time_exploratory240`

| Sleeve | Trades | Stops | Stop rate | Net P&L |
| --- | ---: | ---: | ---: | ---: |
| Core | 11 | 2 | 18.2% | $22,324.19 |
| Exploratory | 5 | 2 | 40.0% | $201.56 |

This is the best current short-premium baseline. It keeps exploratory from being a drag, but it does so by reducing frequency.

### Condor Retest

Run: `data/validation_13m_continuous_q2_q3_guard_time_exploratory240_condor`

| Sleeve | Trades | Stops | Stop rate | Net P&L |
| --- | ---: | ---: | ---: | ---: |
| Core | 6 | 1 | 16.7% | $19,332.24 |
| Exploratory | 5 | 2 | 40.0% | $201.56 |
| Condor | 38 | 9 | 23.7% | -$1,281.45 |

The condor sleeve improves observed margin efficiency but remains negative on standalone attribution, so it should not be scaled yet.

## Implementation Change

Updated `simulator/continuous_research.py` so `--validate` now uses:

- two-tier engine,
- event controls,
- time-of-day controls by default,
- exploratory minimum score 2.40,
- exploratory maximum score 2.49.

New override:

- `--disable-time-of-day-controls`

Defaults can still be changed with:

- `--exploratory-min-score`
- `--exploratory-max-score`

Added `simulator/capital_deployment_ladder.py`.

Generated outputs:

- `data/capital_deployment_ladder_guard_time_exploratory240.csv`
- `capital_deployment_ladder_guard_time_exploratory240.md`

## Capital Deployment Ladder

Using the current best absolute P&L run, first-order linear scaling gives:

| Target max margin / equity | Scale | Est. net P&L | Est. annual return | Est. avg daily credit | Est. worst day |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 2% | 1.84x | $41,431.06 | 0.93% | $1,229.32 | -$19,611.18 |
| 5% | 4.60x | $103,577.66 | 2.33% | $3,073.30 | -$49,027.96 |
| 10% | 9.20x | $207,155.31 | 4.67% | $6,146.60 | -$98,055.92 |
| 20% | 18.39x | $414,310.63 | 9.34% | $12,293.20 | -$196,111.84 |
| 30% | 27.59x | $621,465.94 | 14.01% | $18,439.81 | -$294,167.76 |
| 40% | 36.79x | $828,621.25 | 18.68% | $24,586.41 | -$392,223.68 |

This estimate is useful because it shows that scaling alone does not close the MBH gap. Even at 40% max margin, the current improved baseline only estimates about 18.7% annualized before more realistic slippage, halt effects, and capacity penalties.

## Current Recommendation

Promote `guard + time controls + exploratory 2.40-2.49` as the current short-premium baseline.

Do not scale the condor sleeve yet. It recovered frequency and reduced margin use, but its standalone P&L is still negative.

The next improvement must add new positive-expectancy expressions rather than just increasing size. Highest priority:

1. Add a 1DTE non-directional sleeve using the already downloaded next-expiration chains.
2. Rebuild long-vol hedges around the real directional-failure windows.
3. Continue core bear-call failure analysis, especially:

- 2025-07-16 CALL 6260
- 2025-08-20 CALL 6375

Both were real directional failures in bear-call positions. The current signal fields do not cleanly separate them from winning bear-call trades, so a hand-fit core bear-call block is not yet justified.
