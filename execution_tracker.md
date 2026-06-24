# Execution Tracker

Date: 2026-06-17

## Phase 1: Diligence Reconciliation

Status: First pass complete.

Completed:

- Extracted and reviewed MBH PDFs, transcript, and exported Google Sheets.
- Reconciled audited 2024 and 2025 returns.
- Identified conflicts in liquidity, minimum investment, strategy scope, execution cost, AUM, and capacity.
- Wrote `diligence_reconciliation.md`.

Open:

- Need MBH/Mark to clarify governing liquidity terms, actual minimum, current AUM, and whether Fund 1 uses any non-SPX or non-0DTE overlays.

## Phase 2: Public Baseline Replication

Status: Research package collected; not yet run against fresh data.

Completed:

- Downloaded and unpacked the Vilkov 0DTE replication package.
- Reviewed its summary findings: naive/unconditional 0DTE carry is not enough after realistic friction.

Open:

- Need compatible historical data to rerun baseline tests.
- Need to map public strategy families into our simulator so naive vertical spreads can be compared directly against MBH-style filtered entries.

## Phase 3: MBH-Style Simulator

Status: Real-data pipeline complete and runnable.

Completed:

- Created `simulator\mbh_simulator.py`.
- Supports bull put and bear call vertical spreads.
- Supports 15-minute tranches.
- Supports 15-25 delta short-leg selection.
- Supports protective long wings selected by target delta or fixed-width fallback.
- Supports short-leg-only stop losses.
- Keeps the long wing after the short leg stops.
- Settles positions at end of day.
- Applies per-contract fees.
- Applies daily gross credit cap.
- Applies daily loss halt for new entries.
- Separates 0DTE tradable expiry from 1DTE chain data.
- Supports multiple trade instructions at the same timestamp, allowing delta-neutral and directional expressions.
- Updated default trading window to 9:32 AM through 3:30 PM based on the DDQ.
- Updated default daily credit cap to 1.5% of equity based on the DDQ.
- Added ThetaData downloader for SPXW 0DTE and next-expiration first-order Greeks.
- Added feature builder for ATM straddle residual, skew, 0DTE/next-expiration ratio, and trend score.
- Added real-data backtest runner with configurable account equity, baseline contracts, stop multiple, wing delta, and credit cap.
- Downloaded five pilot dates: 2024-04-09, 2024-07-12, 2024-11-06, 2025-04-24, and 2026-03-02.
- Ran pilot backtests and generated daily/trade outputs.

Verified:

- Code compiles.
- Synthetic sample day runs successfully.
- ThetaData authentication works through `THETADATA_API_KEY`.
- Real SPXW pilot data downloads and processes successfully.

Open:

- Need larger historical date set for walk-forward testing.
- Need real stop-fill modeling from MBH execution data.
- Need real fee/rebate schedule and route assumptions.

## Phase 4: Signal Reconstruction

Status: Candidate engine implemented; first failure-window retest materially improved.

Completed:

- Signal file schema defined.
- Placeholder signal policy maps disclosed signal slots to position size, including 16/8/4/0-style model throttling and DDQ-style VIX tiering.
- Signal slots include straddle residual, skew z-score, 0DTE/1DTE ratio z-score, trend score, realized/implied score, and VIX.
- DDQ model mapping added: ATM Volatility Surface, Skew, Trend Breakout, and Durational Influence.
- Position snapshot analysis added in `position_snapshot_analysis.md`.
- Added strike-level holdings reconstruction from simulated trades at screenshot times.
- Added calibration grid against the March 2, 2026 DDQ 3 PM snapshot.
- Added four-model ensemble policy that allows ATM Surface, Skew, Trend Breakout, Durational Influence, and confluence sleeves to fire independently.
- Added long-volatility overlay module with ATM straddle and direct-put modes.
- Added combined holdings and snapshot scoring tools.
- Added historical no-lookahead signal baseline transformer.
- Added walk-forward grid runner.
- Downloaded and processed 60 SPXW trading days from 2025-01-02 through 2025-03-31.
- Ran a 40-day train / 20-day test walk-forward grid.
- Best tested spread configuration lost $2,224,975.21 on $28,000,000 equity over the March 2025 test window, a -7.95% return.
- Best tested spread configuration had a 31.6% stop rate and halted on 13 of 20 test days.
- Tested the first ATM long-vol overlay; it lost $121,335.84 across the same 20 test days.
- Wrote `research_run_2025_q1_summary.md`.
- Implemented stricter entry-permission filters for premium richness, term-ratio dislocation, realized-volatility shock, and extreme trend score.
- Retested one strict-policy configuration. It reduced trades from 1,358 to 576 and halted days from 13 to 4, but still lost $2,419,273.88 and had a 39.4% stop rate.
- Implemented side-and-strike candidate scoring for bull-put and bear-call spreads.
- Added candidate diagnostics for accepted, selected, rejected, and gated spread candidates.
- Added stop-path diagnostics with minutes-to-stop and adverse spot movement.
- Changed default candidate hurdle to `candidate_min_score = 2.00`.
- Rebuilt long-volatility overlay as a rare crash-hedge put-spread sleeve.
- Retested March 2025 with the candidate engine. Result improved to -$50,845.46, a -0.18% return, with 34 trades, 6 stopped trades, a 17.65% stop rate, 9 no-trade days, and 0 halted days.
- Retested rare put-spread overlay. Result was +$12,184.58 across 2 trades.
- Combined spread plus overlay result was -$38,660.88, a -0.14% return.
- Wrote `phase_1_6_implementation_2026-06-21.md`.
- Added candidate-engine parameters to `walk_forward_grid.py`.
- Added `regime_validation.py` for rolling no-lookahead validation by coarse signal regime.
- Ran a Q1 2025 candidate-score sweep. Score 2.25 produced +$74,206.71 with 14 trades, 1 stopped trade, and 0 halted days.
- Ran rolling validation across 22 currently processed test days from 2025-03-04 through 2026-03-02. Score 2.25 produced +$52,792.08 with 15 trades, 1 stopped trade, and 0 halted days.
- Promoted default `candidate_min_score` from 2.00 to 2.25.
- Wrote `candidate_regime_validation_2026-06-21.md`.
- Added 35 additional 2025 regime dates in `regime_expansion_dates_2025.csv`.
- Downloaded and processed same-day and next-expiration SPXW chains for the 35-date regime basket.
- Expanded processed date directories to 100.
- Expanded validation showed score 2.25 did not generalize: -$527,169.92, 51 trades, 13 stops.
- Added position concentration limits, stop cooldowns, negative-term intraday memory gate, and trend-chase gate.
- Focused sweep found the best current expanded-sample configuration at score 2.50 with refined memory: +$118,359.04, 7 trades, 0 stops.
- Promoted default `candidate_min_score` from 2.25 to 2.50.
- Added event-calendar output to `regime_validation.py`, including `event_summary.csv`.
- Current default expanded validation is in `data\regime_validation_expanded_current_default`.
- Wrote `expanded_regime_iteration_2026-06-21.md`.

Open:

- Improve trade frequency without reintroducing clustered stopouts.
- Add event-calendar fields to validation output.
- Download continuous Q2/Q3 2025 data to improve baselines beyond event/regime samples.
- Add regime/event labels for CPI, FOMC, NFP, and major macro shock days.
- Extend the position-snapshot matching engine to score exact strike-level similarity.
- Calibrate long-volatility overlay module; DDQ says 10% of returns and 4-8 daily strategies come from these hedges.
- Download enough additional regimes to avoid overfitting March 2025.

## Phase 5: Stress And Execution Testing

Status: Not started.

Open:

- Need historical quote data with stressful dates.
- Need CPI/FOMC/NFP/event calendar.
- Need slippage and fill model.
- Need capacity model by AUM and tranche size.

## Immediate Next Work

1. Add candidate-engine parameters to the walk-forward grid.
2. Validate the candidate engine across additional 2025 and 2026 regimes.
3. Add event calendar labels and stress-day buckets.
4. Add T-Bill carry attribution.
5. Compare simulated daily returns to MBH daily returns and screenshots once received.
6. Use tick or 10-second data around stop events after the 1-minute strategy shape is validated.
