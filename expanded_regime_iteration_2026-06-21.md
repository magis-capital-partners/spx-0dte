# Expanded Regime Iteration

Date recorded: 2026-06-21

## Data Added

Added a focused 2025 regime basket:

- File: `regime_expansion_dates_2025.csv`
- New regime dates: 35
- Downloaded same-day and next-expiration SPXW chains for each date.
- Processed date directories after this run: 100

Regime buckets covered:

- Tariff shock / Liberation Day sequence
- CPI days
- FOMC days
- NFP days
- Quiet grind / normalization days
- Late-month non-event samples

## First Expanded Validation

Run:

- `data\regime_validation_expanded_score225`

Configuration:

- Candidate minimum score: 2.25
- Max candidate sides: 1
- No refined concentration/memory gates

Result:

- Days: 57
- Total net P&L: -$527,169.92
- Trades: 51
- Stopped trades: 13
- Stop rate: 25.49%
- Halted days: 0

Interpretation:

The March-only improvement did not generalize. Losses clustered on event/stress days, especially repeated same-side entries around the same strikes before the first stop fired.

## Risk-Layer Improvements

Implemented in `simulator\mbh_simulator.py`:

1. Position concentration limits
   - Max open trades per side
   - Max open trades on the same side and short strike

2. Stop cooldowns
   - Global cooldown after a stop
   - Same-side cooldown after a stop
   - Per-side stop limit

3. Intraday memory gate
   - Tracks negative 0DTE/next-expiration term-ratio dislocation through the day.
   - Blocks later entries after a severe negative term dislocation.

4. Trend-chase gate
   - Blocks bull-put entries when the uptrend signal is already too extended.
   - Blocks bear-call entries when the downtrend signal is already too extended.

## Focused Sweep

Expanded validation variants:

| Run | P&L | Trades | Stops | Stop Rate | No-Trade Days |
| --- | ---: | ---: | ---: | ---: | ---: |
| Score 2.25, no concentration | -$527,169.92 | 51 | 13 | 25.49% | 35 |
| Score 2.25, concentration | -$220,199.45 | 34 | 7 | 20.59% | 35 |
| Score 2.25, refined memory | -$65,286.00 | 25 | 4 | 16.00% | 41 |
| Score 2.50, no refined memory | -$39,016.64 | 12 | 3 | 25.00% | 47 |
| Score 2.75, no refined memory | +$77,462.88 | 4 | 0 | 0.00% | 54 |
| Score 2.50, refined memory | +$118,359.04 | 7 | 0 | 0.00% | 51 |

## Current Default

Promoted the conservative refined configuration:

- `candidate_min_score = 2.50`
- `candidate_half_score = 2.25`
- `candidate_full_score = 2.50`
- `max_open_trades_per_side = 2`
- `max_open_trades_same_side_strike = 1`
- `stop_cooldown_minutes = 30`
- `same_side_stop_cooldown_minutes = 120`
- `max_stops_per_side = 2`
- `memory_term_ratio_skip_threshold = 1.50`
- `memory_skew_skip_threshold = 99.0`
- `memory_trend_skip_threshold = 99.0`
- `candidate_max_chase_trend = 1.50`

Validation output:

- `data\regime_validation_expanded_s250_refined_memory`
- `data\regime_validation_expanded_current_default`

Result:

- Days: 57
- Total net P&L: +$118,359.04
- Total return on $28,000,000: +0.42%
- Total credit sold: $119,010
- Trades: 7
- Stopped trades: 0
- Stop rate: 0.00%
- Positive days: 6
- No-trade days: 51
- Halted days: 0
- Worst day: $0.00

Event summary from `data\regime_validation_expanded_current_default\event_summary.csv`:

| Event Bucket | Days | P&L | Trades | Stops | No-Trade Days |
| --- | ---: | ---: | ---: | ---: | ---: |
| CPI event | 8 | +$67,892.88 | 4 | 0 | 5 |
| FOMC event | 6 | $0.00 | 0 | 0 | 6 |
| NFP event | 8 | +$28,935.72 | 1 | 0 | 7 |
| Quiet grind | 8 | +$17,715.72 | 1 | 0 | 7 |
| Tariff reversal | 1 | $0.00 | 0 | 0 | 1 |
| Tariff shock | 4 | $0.00 | 0 | 0 | 4 |
| Unlabeled | 22 | +$3,814.72 | 1 | 0 | 21 |

## Interpretation

The new risk layers solved the observed event-day blowups in the expanded sample. However, the resulting strategy is too selective to be a full MBH recreation. It is now behaving like a high-conviction subset of the strategy rather than the broader 15-minute tranche engine described in the DDQ.

This is progress: the priority moved from "avoid obvious blowups" to "recover trade frequency without reintroducing clustered stopouts."

## Next Iteration

1. Improve baseline quality with more continuous data, not just event/regime samples.
2. Test a two-tier engine:
   - Core engine: current conservative 2.50 refined gate.
   - Small-size exploratory engine: lower score threshold, but only when no negative-term memory flag and no concentration risk.
3. Add time-of-day controls:
   - avoid first 10 minutes
   - reduce late-day entries after 14:30 unless score is exceptional
4. Rerun after adding continuous Q2/Q3 2025 data.
