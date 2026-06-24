# Data Schema For Reconstruction

The simulator expects decision-time data only. No field may use information from later in the day when making an entry decision.

## Option Quote File

Required columns:

- `timestamp` - ISO timestamp in New York time or timezone-aware UTC.
- `expiry` - option expiry date, `YYYY-MM-DD`.
- `option_type` - `C` for call or `P` for put.
- `strike` - strike price.
- `bid` - bid price.
- `ask` - ask price.
- `delta` - option delta at the timestamp.
- `underlying_price` - SPX level at the timestamp.

Preferred columns:

- `iv` - implied volatility.
- `bid_size` - displayed bid size.
- `ask_size` - displayed ask size.
- `volume` - same-day traded volume.
- `open_interest` - open interest if available.
- `quote_condition` - quote status or exchange condition.
- `source` - vendor/source tag.

Notes:

- Quote data must include both 0DTE and 1DTE chains to compute term-ratio features.
- Deltas should be vendor-provided or computed from a documented model.
- Bid/ask width and quote staleness must be retained; they are part of the execution edge.

## Signal File

Required columns for the current simulator:

- `timestamp`
- `straddle_residual_z`
- `skew_z`
- `term_ratio_z`
- `trend_score`

Optional columns:

- `vix`
- `vix1d`
- `vix9d`
- `realized_vs_implied_z`
- `event_flag`
- `liquidity_score`
- `atm_surface_score`
- `skew_filter_score`
- `trend_breakout_score`
- `durational_influence_score`
- `model_expression`

Interpretation:

- `straddle_residual_z`: positive means 0DTE straddle is richer than expected decay baseline; negative means cheaper than expected.
- `skew_z`: positive means put/call skew is above normal; negative means below normal.
- `term_ratio_z`: positive or negative means the 0DTE/1DTE relationship is away from its normal band.
- `trend_score`: positive favors bull put spreads; negative favors bear call spreads.
- `model_expression`: optional explicit instruction such as `delta_neutral`, `directional_put`, `directional_call`, or `skip`.

## Position Snapshot File

For broker screenshots that only show strike-level positions, use:

- `timestamp`
- `expiry`
- `strike`
- `call_contracts`
- `put_contracts`
- `source`

Contract sign convention:

- Positive means long.
- Negative means short.
- Zero means no position.

Snapshot use:

- Reconcile active short legs against farther-OTM long wings.
- Identify unmatched long wings that likely remain after short-leg stops.
- Infer whether the book is balanced delta-neutral, directional, or dominated by stopped-short remnants.

## Execution Export Target

If MBH sends broker exports, preserve these fields:

- account
- order_id
- parent_order_id
- timestamp_submitted
- timestamp_filled
- symbol
- expiry
- option_type
- strike
- side
- quantity
- order_type
- limit_price
- stop_price
- fill_price
- commission
- exchange_fee
- route
- liquidity_flag
- bid_at_submit
- ask_at_submit
- midpoint_at_submit
- bid_at_fill
- ask_at_fill
- midpoint_at_fill

These exports let us estimate realized slippage, stop behavior, and whether the stated fills are reproducible.

## Daily Return Calibration File

Required columns:

- `date`
- `begin_equity`
- `end_equity`
- `gross_pnl`
- `net_pnl`
- `daily_return`
- `contracts_traded`
- `gross_credit_sold`
- `fees`
- `slippage_estimate`
- `max_intraday_drawdown`

Preferred columns:

- `bull_put_count`
- `bear_call_count`
- `stopped_short_count`
- `expired_short_count`
- `long_wing_pnl`
- `short_leg_pnl`
- `event_day_flag`
- `vix_open`
- `vix_close`

This file becomes the bridge between the recreated simulator and MBH's reported daily return sheet.
