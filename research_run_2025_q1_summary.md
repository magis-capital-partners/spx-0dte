# 2025 Q1 ThetaData Research Run

Date recorded: 2026-06-20

## Scope

- Symbol: SPXW
- Raw date range: 2025-01-02 through 2025-03-31
- Raw dates downloaded: 60
- Processed dates built: 60
- Training window: first 40 processed dates, 2025-01-02 through 2025-03-03
- Test window: next 20 processed dates, 2025-03-04 through 2025-03-31

## Files Produced

- Walk-forward grid: `data\walk_forward_full_fast\walk_forward_grid.csv`
- Long-vol overlay summary: `data\long_vol_full_fast\long_vol_daily_summary.csv`
- Long-vol overlay trades: `data\long_vol_full_fast\long_vol_trades.csv`

## Walk-Forward Result

Best tested spread configuration by mean daily return:

- Baseline contracts: 66
- Daily gross credit cap: 1.5% of equity
- Stop multiple: 2.5x short premium
- Target long-wing absolute delta: 0.08
- Skew extreme threshold: 1.0
- 0DTE/1DTE term-ratio extreme threshold: 1.0
- Total net P&L: -$2,224,975.21
- Total return on $28,000,000 equity: -7.95%
- Mean daily return: -0.397%
- Total credit sold: $6,911,770
- Trades: 1,358
- Stopped trades: 429
- Stop rate: 31.6%
- Halted days: 13 of 20

The other tested settings were also negative. Increasing gross credit from 1.5% to 2.0% worsened results in this window, and larger baseline contract counts worsened drawdown.

## Long-Vol Overlay Result

Overlay structure tested: ATM straddle trigger overlay.

- Test days: 20
- Total net P&L: -$121,335.84
- Mean daily P&L: -$6,066.79
- Positive days: 4 of 20
- Total return on $28,000,000 equity: -0.43%

This overlay behaved as hedge drag in this configuration. It should not be treated as an edge until the trigger logic is rebuilt.

## Interpretation

The current model is not close enough to MBH's live edge. The infrastructure works, and the result is useful because it shows that the present reconstruction still overtrades. The model is taking too many entries in difficult intraday regimes, creating a stop rate around 32% and frequent daily halts.

This is inconsistent with the described MBH edge, where the "secret sauce" is filtering out low-probability entries. The next version should shift from a broad four-model entry engine to a stricter entry-permission engine.

## Strict-Filter Follow-Up

Implemented a stricter entry-permission layer:

- Requires positive ATM premium richness by default.
- Skips entries when the 0DTE/1DTE ratio is too dislocated.
- Skips entries during realized-volatility shock conditions.
- Skips entries during extreme trend-score conditions.

Single strict-policy retest, using the same March 2025 test dates:

- Baseline contracts: 66
- Daily gross credit cap: 1.5% of equity
- Stop multiple: 2.5x short premium
- Target long-wing absolute delta: 0.08
- Total net P&L: -$2,419,273.88
- Total return on $28,000,000 equity: -8.64%
- Mean daily return: -0.432%
- Total credit sold: $3,107,110
- Trades: 576
- Stopped trades: 227
- Stop rate: 39.4%
- Positive days: 4 of 20
- No-trade days: 5 of 20
- Halted days: 4 of 20

This reduced trade count and daily halts, but it did not improve returns. The remaining book is still stopping out too often. The next bottleneck is therefore not only "trade less"; it is strike/direction selection and stop path modeling on the trades that survive the filters.

## Candidate-Engine Follow-Up

Implemented the side-and-strike candidate engine described in `phase_1_6_implementation_2026-06-21.md`.

Best current March 2025 retest:

- Candidate minimum score: 2.00
- Max selected sides per tranche: 1
- Total net P&L: -$50,845.46
- Total return on $28,000,000 equity: -0.18%
- Total credit sold: $467,760
- Trades: 34
- Stopped trades: 6
- Stop rate: 17.65%
- Positive days: 7 of 20
- No-trade days: 9 of 20
- Halted days: 0 of 20
- Worst day: -$196,478.12

Rare crash-hedge put-spread overlay:

- Trades: 2
- Total net P&L: +$12,184.58

Combined spread plus overlay result:

- Total net P&L: -$38,660.88
- Total return: -0.14%

## Next Model Changes

1. Add candidate-engine parameters to the walk-forward grid.
2. Validate the score-2 hurdle outside March 2025.
3. Add regime and event labels before expanding optimization.
4. Use tick or 10-second data around stop events after the 1-minute strategy shape is validated.
5. Keep snapshot matching separate from P&L optimization. The March 2 DDQ snapshot requires more long-put exposure than the P&L model naturally holds, but forcing that exposure hurt the first walk-forward test.
