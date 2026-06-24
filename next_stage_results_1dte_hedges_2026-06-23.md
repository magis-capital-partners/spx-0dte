# Next Stage Results: 1DTE Sleeve And Debit Hedges

Date: 2026-06-23

## Implemented

### Data Rebuild

Updated `simulator/feature_builder.py` so `normalized_option_quotes.csv` now includes:

- 0DTE quote rows,
- next-expiration quote rows.

This was required because the prior processed files used next-expiration data for term-ratio features but did not retain those quotes for trading.

Rebuilt continuous Q2/Q3 2025 processed data:

- 2025-04-01 through 2025-09-30
- 126 market dates

### 1DTE Non-Directional Sleeve

Updated `simulator/mbh_simulator.py` and `simulator/regime_validation.py` to support a next-expiration sleeve:

- uses the next expiry in the quote snapshot,
- opens paired bull-put and bear-call credit spreads,
- marks 1DTE positions at close using bid/ask quotes instead of intrinsic settlement,
- reports `one_dte_trades`, `one_dte_credit_sold`, `one_dte_net_pnl`, and stopped trades separately.

### Attached Debit Hedge Tester

Added `simulator/attached_debit_hedge.py`.

This tests a trade-attached long-vol hedge:

- bear-call source trade gets a call debit spread,
- bull-put source trade gets a put debit spread,
- hedge size is based on a fraction of the source trade credit,
- output is reported separately from the core strategy.

## Validation Results

All results use the $13,000,000 account and continuous Q2/Q3 2025 validation.

| Run | Trades | Stops | Stop rate | Net P&L | Gross credit sold | Max margin |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Current best rebuilt baseline | 16 | 4 | 25.0% | $22,525.75 | $57,480 | $141,360 |
| Baseline + all-day 1DTE sleeve | 94 | 19 | 20.2% | -$21,945.42 | $76,635 | $84,725 |
| Baseline + late-only 1DTE sleeve | 35 | 7 | 20.0% | $19,323.58 | $67,860 | $141,360 |

## Sleeve Attribution

### All-Day 1DTE

| Sleeve | Trades | Stops | Stop rate | Net P&L | Credit sold |
| --- | ---: | ---: | ---: | ---: | ---: |
| Core | 5 | 2 | 40.0% | -$9,008.27 | $22,140 |
| Exploratory | 5 | 2 | 40.0% | $201.56 | $3,750 |
| 1DTE | 84 | 15 | 17.9% | -$13,138.71 | $50,745 |

The all-day 1DTE sleeve is not viable. It adds many trades but loses standalone money and interferes with the core sleeve through portfolio concentration and credit usage.

### Late-Only 1DTE

| Sleeve | Trades | Stops | Stop rate | Net P&L | Credit sold |
| --- | ---: | ---: | ---: | ---: | ---: |
| Core | 11 | 2 | 18.2% | $22,324.19 | $53,730 |
| Exploratory | 5 | 2 | 40.0% | $201.56 | $3,750 |
| 1DTE | 19 | 3 | 15.8% | -$3,202.17 | $10,380 |

Late-only 1DTE is less bad, but still negative standalone. It should not be scaled.

## Debit Hedge Results

Current best baseline:

- Net P&L: $22,525.75
- Worst day: -$10,662.45

Attached hedge tests:

| Hedge test | Hedge P&L | Hedge budget | Combined P&L |
| --- | ---: | ---: | ---: |
| Hedge all short-premium trades | -$10,177.68 | $14,082.50 | $12,348.07 |
| Hedge only core bear-call trades | -$7,442.40 | $11,057.50 | $15,083.35 |

The attached debit hedges are also not viable in this form. They helped one observed call-side failure but dragged on too many winning bear-call trades and did not improve the largest July 16 loss enough to justify the cost.

## Conclusion

The next-stage additions were implemented, but neither should be promoted:

- Do not scale the all-day 1DTE sleeve.
- Do not scale the late-only 1DTE sleeve yet.
- Do not add the attached debit hedge as currently designed.

The current best production candidate remains:

- event controls,
- time-of-day controls,
- exploratory bear-call guard,
- exploratory score band 2.40-2.49,
- no condor,
- no 1DTE sleeve,
- no attached debit hedge.

## Next Improvement Target

The useful next step is not more broad sleeves. It is a portfolio allocator plus stricter sleeve eligibility:

1. Add per-sleeve margin budgets so new sleeves cannot displace the profitable core.
2. Rework 1DTE selection so it trades only when standalone 1DTE expectancy is positive by event/time bucket.
3. Add a true intraday margin path report.
4. Test a broken-wing butterfly structure for late-day rich-premium conditions, because condors and 1DTE spreads are not yet positive standalone.

