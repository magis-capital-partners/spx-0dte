# $13M Account Scaling And Condor Sleeve Run

Date recorded: 2026-06-22

## Scaling Change

Validation defaults were changed from the prior $28M reference account to a $13M account.

Proportional tranche scaling:

- Prior account equity: $28,000,000
- Prior full-size tranche: 66 spreads
- New account equity: $13,000,000
- Scaled tranche: 66 * 13 / 28 = 30.64, rounded to 31 spreads

Updated defaults in `simulator/regime_validation.py`:

- `--account-equity`: $13,000,000
- `--baseline-contracts`: 31

The percentage controls remain unchanged:

- Daily credit cap: 1.5% of equity
- Daily loss limit: 2.25% of equity
- Exploratory sleeve size: 15% of base contracts

## Added Reporting

`daily_regime_validation.csv` now includes:

- `gross_credit_pct_equity`
- `approx_spread_margin`
- `approx_spread_margin_pct_equity`
- `condor_trades`
- `condor_stopped_trades`
- `condor_credit_sold`
- `condor_net_pnl`

Approximate spread margin is computed as:

`(spread width - entry credit) * contracts * 100`

This is a risk-defined vertical-spread margin approximation.

## Implemented Structure Sleeve

Added optional neutral-condor sleeve:

- Enabled with `--condor-sleeve`
- Builds a paired bull-put and bear-call spread from lower-delta shorts
- Uses its own neutral/rich-premium gates instead of the side-specific directional candidate score
- Defaults:
  - target short delta: 0.12
  - delta range: 0.08 to 0.16
  - size: 15% of base contracts per leg
  - blocks tariff shock, tariff reversal, and FOMC buckets

The sleeve is implemented but disabled by default.

## $13M Validation Results

Sample: 60 processed validation days with rolling 40-day baselines.

| Run | Trades | Stops | Stop Rate | Net P&L | Return On $13M | Max Day Credit | Max Approx Margin |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Core 2.50 baseline | 9 | 1 | 11.1% | $68,544.03 | 0.5273% | $17,050 | $137,950 |
| Event-aware two-tier | 20 | 5 | 25.0% | $71,269.56 | 0.5482% | $17,050 | $137,950 |
| Event-aware two-tier + condor | 70 | 25 | 35.7% | -$4,852.45 | -0.0373% | $17,370 | $138,365 |
| Strict low-delta condor variant | 39 | 12 | 30.8% | $47,584.29 | 0.3660% | $17,050 | $137,950 |

Best current $13M working baseline:

- `data/validation_13m_event_two_tier`

This keeps the event-aware two-tier engine and leaves condors disabled.

## Readout

The $13M scaling worked as expected. Returns and risk are proportionally similar to the prior $28M run, with full-size tranches reduced from 66 to 31 spreads.

The current strategy remains far below MBH-like capital deployment:

- Max observed daily credit sold: $17,050, or 0.1312% of equity
- Max approximate spread margin: $137,950, or 1.0612% of equity

This is still far below the MBH-referenced average buying power use around 40%.

The condor sleeve is not ready to enable:

- Relaxed condor generated too many marginal trades and lost money.
- Strict condor reduced the damage but still underperformed the no-condor two-tier baseline.
- The event-heavy sparse sample is not a good calibration set for condors; continuous Q2/Q3 data should come before more tuning.

## Current Recommendation

Use `validation_13m_event_two_tier` as the current working baseline.

Keep the condor code available but disabled by default.

Next best improvement:

- Download continuous Q2/Q3 2025 and stabilize normal-day baselines.
- Then retest condors/butterflies on normal rich-premium days, not only the current event-heavy validation basket.
- Add a portfolio allocator that targets margin bands only after sleeve-level stop behavior is acceptable.
