# Two-Tier Engine Validation Summary

Date recorded: 2026-06-22

## Implementation

- Added optional two-tier candidate selection to `simulator/mbh_simulator.py`.
- Kept the conservative core at score >= 2.50.
- Added an exploratory sleeve with score range 2.00 to 2.40 and default size of 15% of the base contract count.
- Added exploratory-specific blocks for:
  - intraday memory and stop cooldowns through the existing risk gate
  - late entries after 14:30 when score is below 2.50
  - existing same-side strike clusters within 25 points
  - candidates where skew and trend both conflict with the side
- Added `--two-tier-engine` and exploratory parameters to `simulator/regime_validation.py`.
- Added daily output fields for core/exploratory trade counts, stops, credit sold, and P&L.

Primary output:

- `data/two_tier_validation`

Comparison baseline output:

- `data/two_tier_validation_core_baseline`

## Same-Sample Validation Results

Sample: 60 processed validation days, rolling 40-day baselines.

| Engine | Trades | Stops | Stop Rate | Halted Days | Gross Credit Sold | Net P&L | Worst Day |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Core 2.50 baseline | 9 | 1 | 11.1% | 0 | $167,850 | $145,686.34 | $0.00 |
| Two-tier | 43 | 15 | 34.9% | 0 | $216,375 | $57,110.71 | -$37,947.40 |

Two-tier sleeve split:

| Sleeve | Trades | Stops | Gross Credit Sold | Net P&L |
| --- | ---: | ---: | ---: | ---: |
| Core | 6 | 1 | $101,520 | $79,721.32 |
| Exploratory | 37 | 14 | $114,855 | -$22,610.61 |

## Readout

The two-tier implementation recovered frequency, moving from 9 trades to 43 trades on the same 60-day sample. It did not meet the risk criteria: stop rate rose to 34.9%, above the 15% target, and net P&L fell below the core-only baseline.

The exploratory sleeve is the problem. It generated most of the added frequency, but also 14 of 15 stops and negative standalone P&L. The existing guards are directionally useful, but the 2.00 to 2.40 score band is too permissive on this sparse event-heavy sample.

Useful next tests:

- Raise exploratory minimum score to 2.25.
- Narrow exploratory max score to 2.35 or merge 2.40-2.50 into half-size core behavior.
- Disable exploratory entries on rich-premium plus term-dislocated days until event labeling is improved.
- Add the planned time-of-day controls before further score tuning.
