# MBH Steps 1-6 Implementation Results - 2026-06-24

## Source Data Added

The shared MBH Google Sheet was exported locally to:

- `data/mbh_returns/2025.csv`
- `data/mbh_returns/2024.csv`
- `data/mbh_returns/All_Time_Net_Returns.csv`
- `data/mbh_returns/All_Time_Returns.csv`
- `data/mbh_returns/Fund_1_0_DTE_Only_2026.csv`
- `data/mbh_returns/Fund_2_High_Vol_Multi_Strat_2026.csv`

Key MBH benchmark from the exported `All_Time_Net_Returns.csv`:

- 2025 audited net return: `32.44%`
- May-Sep 2025 compounded net return: `6.97%`
- 2025 monthly net returns:
  - Jan: `5.16%`
  - Feb: `0.27%`
  - Mar: `5.57%`
  - Apr: `8.73%`
  - May: `4.63%`
  - Jun: `-4.00%`
  - Jul: `2.64%`
  - Aug: `1.71%`
  - Sep: `2.01%`
  - Oct: `1.42%`
  - Nov: `0.43%`
  - Dec: `0.44%`

## Implemented

### 1. Structure diversity

Added two executable 0DTE-only two-leg structure sleeves:

- `trend_debit`: call debit spreads on strong uptrends and put debit spreads on strong downtrends.
- `long_put_hedge`: long put spreads on downside/failure-mode signals.

True butterflies/broken-wing butterflies still require a multi-leg trade object and settlement engine. The current simulator is a two-leg engine; forcing butterflies into it would produce bad accounting. The correct next engineering step is a multi-leg position model.

### 2. More trade opportunities

The new sleeves add opportunities alongside the current core/exploratory short-premium engine.

Broad first-pass run:

- Trades increased from `16` to `192`.
- 1DTE trades remained `0`.

This did increase frequency, but the added trades were not profitable.

### 3. Stop-loss/failure reduction

Added stopped-trade failure labels:

- `realized_vol_shock`
- `term_structure_shock`
- `downtrend_continuation`
- `uptrend_continuation`
- `skew_dislocation`
- `fast_adverse_move`
- `ordinary_stop`

In the tested 40% allocator runs, the four stopped trades labeled as:

- `ordinary_stop`: `2`
- `fast_adverse_move`: `2`

### 4. Failure-mode model

Failure labels are now emitted in `stop_diagnostics.csv`, making stopped-trade analysis reproducible by run.

The current Q2/Q3 2025 failed trades are not primarily labeled as term/skew/realized-vol shocks. They are mostly ordinary/fast adverse move failures, which points toward entry timing, strike selection, and intraday exits rather than a simple event-vol filter.

### 5. Allocator integration

The new sleeves are controlled by the portfolio allocator:

- Core budget
- Exploratory budget
- Trend debit budget
- Long put hedge budget
- Total portfolio margin budget

Debit-spread margin is now debit-aware. Debit trades consume allocator budget based on premium paid, not short-spread max loss.

### 6. MBH return calibration

Added:

- `simulator/mbh_return_calibration.py`

Calibration outputs:

- `mbh_calibration_allocator_credit5pct.md`
- `data/mbh_calibration_allocator_credit5pct.csv`
- `mbh_calibration_trend_debit_strict_credit5pct.md`
- `data/mbh_calibration_trend_debit_strict_credit5pct.csv`

## Results

### Current best allocator baseline, no new sleeves

Output:

- `data/deployment_sweep_allocator_guard_time_exploratory240_credit5pct.csv`
- `data/deployment_sweep_allocator_guard_time_exploratory240_credit5pct/target_margin_40p0_contracts_1140/`

Summary:

- Days: `86`
- Trades: `16`
- 1DTE trades: `0`
- Net P&L: `$711,993.31`
- Annualized return: `16.05%`
- Max margin: `$4,548,800`
- Max margin/equity: `34.99%`
- Worst day: `-$442,060.60`
- Worst day/equity: `-3.40%`

MBH calibration over overlapping months May-Sep 2025:

- Strategy compounded return: `5.46%`
- MBH compounded net return: `6.97%`
- Gap: `-1.50%`

Monthly gaps:

| Month | Strategy | MBH Net | Gap |
|---|---:|---:|---:|
| May | `0.00%` | `4.63%` | `-4.63%` |
| June | `1.38%` | `-4.00%` | `5.38%` |
| July | `-1.41%` | `2.64%` | `-4.05%` |
| August | `3.75%` | `1.71%` | `2.04%` |
| September | `1.70%` | `2.01%` | `-0.31%` |

### Broad new-sleeve run

Output:

- `data/deployment_sweep_allocator_new_sleeves_credit5pct.csv`
- `data/deployment_sweep_allocator_new_sleeves_credit5pct/target_margin_40p0_contracts_1140/`

Summary:

- Days: `86`
- Trades: `192`
- 1DTE trades: `0`
- Net P&L: `-$1,280,489.71`
- Annualized return: `-28.86%`
- Max margin/equity: `24.97%`
- Worst day: `-$689,455.70`

Sleeve attribution:

- Core: `$607,565.85`
- Exploratory: `-$49,013.74`
- Trend debit: `-$1,146,200.02`
- Long put hedge: `-$692,841.80`

Conclusion: not viable.

### Strict trend-debit run

Output:

- `data/deployment_sweep_allocator_trend_debit_strict_credit5pct.csv`
- `data/deployment_sweep_allocator_trend_debit_strict_credit5pct/target_margin_40p0_contracts_1140/`

Summary:

- Days: `86`
- Trades: `38`
- 1DTE trades: `0`
- Net P&L: `$647,693.31`
- Annualized return: `14.60%`
- Max margin/equity: `35.38%`
- Worst day: `-$493,164.38`

Sleeve attribution:

- Core: `$761,007.05`
- Exploratory: `-$49,013.74`
- Trend debit: `-$64,300.00`

MBH calibration over overlapping months May-Sep 2025:

- Strategy compounded return: `4.90%`
- MBH compounded net return: `6.97%`
- Gap: `-2.07%`

Conclusion: stricter trend debit is less bad, but still not better than the current baseline.

## Current Answer

The implemented sleeves do not move the strategy from roughly mid-teens annualized toward 30%+. They increase trade frequency, but the first-pass debit/hedge sleeves destroy too much premium through paid optionality.

The current best validated configuration remains the 0DTE core/exploratory short-premium engine with allocator controls and no executable 1DTE trading.

## What Still Stands Between This And MBH

The MBH return profile is not just "more vertical spread size." The exported sheet shows a strong 2025 return path with large positive months in Jan, Mar, Apr, and May, and a controlled negative June. Our available continuous validation only begins producing out-of-sample trades on May 29 because of the 40-day rolling baseline requirement, so we are not yet testing the strongest MBH months.

The biggest remaining blockers are:

1. Need earlier 2025 continuous data and/or shorter warmup baselines so Jan-May can be tested.
2. Need a true multi-leg engine for butterflies, broken-wing butterflies, and real condor accounting.
3. Need intraday exit rules for debit structures. Holding 0DTE debit spreads to close is not viable in the first-pass tests.
4. Need calibration to MBH daily returns if available, not only monthly returns.
5. Need a fill/slippage model. Current tests still use quoted bid/ask assumptions.

## Recommended Next Engineering Step

Build a multi-leg position engine before adding more structure sleeves.

Minimum requirements:

- Position with 2-4 option legs.
- Per-leg quantity and buy/sell direction.
- Mark-to-market by leg.
- Stop/target rules by total position value, not short-leg ask only.
- Expiration settlement by leg.
- Margin/debit calculation by structure.

That unlocks real testing of:

- Broken-wing butterflies.
- Asymmetric butterflies.
- Iron condors as one structure instead of two independent credit spreads.
- Ratio/hedged structures.

Without that, the system cannot faithfully model the structures most likely to explain the gap to MBH.
