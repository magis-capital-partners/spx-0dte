# Next Improvement Plan

Date recorded: 2026-06-21

## Current State

The current default engine is conservative and safe on the expanded validation sample:

- Candidate minimum score: 2.50
- Negative-term intraday memory gate enabled
- Trend-chase gate enabled
- Same-side and same-strike concentration controls enabled
- Expanded validation result: +$118,359.04 over 57 test days
- Trades: 7
- Stops: 0
- Halted days: 0

This is a safer high-conviction subset, but it is too selective to resemble the MBH strategy. The next work should recover frequency while preserving the risk control improvements.

## Phase 1: Build A Two-Tier Engine

Goal: keep the current conservative engine as the core book, then add a smaller exploratory sleeve that can trade more often without recreating clustered stopouts.

Core sleeve:

- Candidate minimum score: 2.50
- Full-size allocation
- Current memory, concentration, and trend-chase gates

Exploratory sleeve:

- Candidate minimum score range: 2.00 to 2.40
- Size: 10-25% of core sleeve
- Disabled when any of these are true:
  - negative-term intraday memory flag is active
  - same-side stop cooldown is active
  - candidate is near an existing same-side strike cluster
  - entry is after 14:30 and score is below 2.50
  - candidate side conflicts with skew and trend together

Success criteria:

- At least 20-40 trades over the 57-day expanded validation sample
- Stop rate below 15%
- No halted days
- Worst day better than -0.50% of equity
- Net P&L better than current +$118K or materially higher credit sold with similar drawdown

Primary output:

- `data\two_tier_validation`

## Phase 2: Add Time-Of-Day Controls

Goal: reduce bad late-day and post-shock entries while allowing good midday decay trades.

Test controls:

- No new entries before 09:45 unless score >= 2.75
- No exploratory entries after 14:30
- Core entries after 14:30 require score >= 2.75
- After 15:00, require wider distance from spot or smaller size
- If a side stopped earlier in the day, no same-side re-entry after 14:00

Reason:

Several historical stopouts were fast, late, and same-side. MBH explicitly references late reversal days as the worst-case path. Time-of-day gating is a direct response to that.

Success criteria:

- Reduce late-day stopped trades without eliminating profitable late-day winners
- Improve worst-day and stop-rate metrics before optimizing P&L

## Phase 3: Improve Event Labels And Regime Buckets

Goal: make validation explainable by event type, not just signal state.

Add fields:

- `event_bucket`
- `event_importance`
- `scheduled_release_time`
- `fomc_statement_time`
- `post_event_window`
- `overnight_gap_bucket`
- `intraday_range_bucket`

Buckets:

- CPI
- FOMC
- NFP
- tariff/geopolitical shock
- OpEx / monthly expiration
- quiet grind
- post-shock normalization

Success criteria:

- Event summary can show where trades are enabled, blocked, profitable, and stopped
- Strategy can have separate rules for scheduled macro days versus ordinary dislocation days

Primary output:

- richer `event_summary.csv`
- `event_calendar_2025_2026.csv`

## Phase 4: Download Continuous Data For Better Baselines

Goal: stop relying on a sparse event basket for historical baselines.

Download priority:

1. Continuous Q2 2025
2. Continuous Q3 2025
3. Continuous Q4 2025
4. Continuous Q1 2026 through the MBH DDQ date

Reason:

The current historical z-scores are useful but still fragile because the training set is narrow. Continuous data should make skew, term-ratio, and straddle-richness baselines more stable.

Success criteria:

- At least 150-250 processed trading days
- Rolling validation results are not dominated by one month or one event cluster
- Signal regime classifier no longer overflags most days as trend/skew extreme

## Phase 5: Add Stop-Path Microstructure

Goal: determine whether stopped trades are genuinely bad ideas or artifacts of 1-minute quote granularity and stop-fill assumptions.

For stopped trades only:

- Download tick or 10-second data around entry-to-stop windows
- Reconstruct bid/ask path on the short leg
- Measure slippage versus theoretical stop
- Test stop alternatives:
  - 2.0x, 2.5x, 3.0x short premium
  - stop on spread value
  - time-delayed stop confirmation
  - no same-minute re-entry after a stop

Success criteria:

- Stop model explains observed drawdown mechanics
- Any improved stop rule must reduce realized loss without materially increasing tail loss

## Phase 6: Rebuild Long-Vol Overlay Around Actual Failures

Goal: make the long-vol sleeve respond to the short-premium engine's true failure modes.

Current result:

- Rare put-spread overlay is no longer daily drag, but it is not yet integrated with the short-premium book.

Next version:

- Trigger only when short-premium engine is blocked by negative-term memory, extreme trend, or event shock
- Use put spreads or broken-wing put structures
- Size from 0.025% to 0.10% of equity per day
- Avoid buying convexity when implied premium is already too expensive unless crash conditions are present

Success criteria:

- Improves worst-day result
- Does not reduce average return materially
- Helps on known stress days such as tariff shock and large reversal days

## Phase 7: MBH Snapshot And Daily Return Calibration

Goal: compare our engine to MBH behavior rather than only optimizing P&L.

Use:

- DDQ screenshots
- User-provided position snapshot
- Any daily position screenshots from MBH
- Any daily return sheet from MBH

Compare:

- long vs short contracts
- call vs put exposure
- strike clustering
- time-of-day book evolution
- number of active strategies/sleeves
- days with no-trade or reduced-size behavior

Success criteria:

- Simulated book looks structurally similar at 10:00, 13:00, and 15:30
- Daily return correlation improves without overfitting individual screenshots

## Phase 8: Capacity And Execution Model

Goal: make the backtest closer to real fund economics.

Add:

- fee schedule by venue/route assumption
- slippage by contract count
- refresh-speed penalty
- contract-count scaling from $28M to $100M
- tranche interval test: 15 minutes vs 12 minutes vs 10 minutes

Success criteria:

- Capacity estimate does not rely on mid-price fantasy fills
- Strategy remains viable after realistic friction

## Immediate Next Implementation Order

1. Implement the two-tier engine.
2. Add time-of-day controls.
3. Run expanded validation against the current 100 processed dates.
4. If results improve, download continuous Q2/Q3 2025.
5. Rerun rolling validation with improved baselines.
6. Only then tune score thresholds further.

## Do Not Do Yet

- Do not optimize dozens of parameters on the current 57-day expanded test window.
- Do not claim MBH replication until frequency, snapshot structure, and daily return behavior match better.
- Do not scale to tick data for all days; use tick/10-second data only around stopped trades first.

