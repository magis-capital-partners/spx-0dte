# Position Snapshot Analysis

Date: 2026-06-18

Sources:

- User-provided position snapshot.
- `MBH DDQ.pdf`, extracted to `C:\Users\werdn\Documents\Investing\SPX Options\MBH Capital\Source Materials\Extracted Text\MBH_DDQ.txt`.

## DDQ Model Updates

The DDQ clarifies that the strategy has four core models:

1. ATM Volatility Surface - identifies environments to underwrite high expected-value risk premium through delta-neutral trades.
2. Skew - filters out unattractive risk-premium selling environments based on volatility skew shape and steepness.
3. Trend Breakout - identifies rapid changes in options pricing to initiate directional trades.
4. Durational Influence - identifies when 0DTE movement is being driven by events or flows outside the 0DTE market, allowing low expected-value trades to be removed.

The DDQ also explains the "12-18 live strategies" language:

- Four models can express delta-neutral or directional trades, creating eight base strategy expressions.
- When all four models agree, MBH treats that confluence as two additional ensemble strategies.
- Four to eight long-volatility strategies are layered daily as dynamic hedges.

Model implication:

- The simulator must allow multiple trade instructions at the same timestamp, not just one bull-put or bear-call decision.
- The core 0DTE engine should model delta-neutral and directional expressions separately.
- Long-volatility overlays should be modeled as a later module, not ignored permanently.

## DDQ Risk And Trading Parameters

DDQ parameters to use in the reconstruction:

- Average premium sold per day: approximately 1.5% of account equity.
- 99% 1-day VaR: -1.36%.
- CVaR / expected shortfall: -2.04%.
- Average intraday margin utilization: approximately 40%.
- Maximum margin utilization: approximately 70%.
- Hard-stop framework: portfolio-level worst case is a function of total premium collected.
- Maximum single-day loss if all positions stop out with slippage: approximately -2.25% of capital.
- Worst-case scenario frequency: roughly once per quarter.
- Individual trade win rate: approximately 65%.
- Typical first trade: 9:32 AM ET.
- Typical last entry: between 3:00 PM and 3:30 PM ET, depending on volatility.

VIX sizing:

- VIX below 12: significantly reduce premium sold and may not trade.
- VIX 12-17: tiered size reduction.
- VIX below 16: approximately 90% of base size.
- VIX below 15: approximately 80% of base size, with continued incremental reductions lower.
- VIX 17-25: full base size; described as optimal.
- VIX 25-35: full base size, but structures may adjust for wider bid/ask spreads.
- VIX above 35: evaluate carefully and possibly reduce size or step aside.

Return attribution from DDQ:

- Approximately 75% of total returns from short options / premium selling.
- Approximately 15% from T-Bills.
- Approximately 10% from long-volatility hedges.

Model implication:

- A pure short-spread simulator should explain most but not all reported returns.
- T-Bill carry and long-vol hedges need separate attribution modules before matching fund-level performance.

## Snapshot Contract Totals

### User Snapshot

- Rows: 33.
- Long calls: 513.
- Short calls: 338.
- Net calls: +175.
- Long puts: 520.
- Short puts: 308.
- Net puts: +212.
- Total long contracts: 1,033.
- Total short contracts: 646.
- Net contracts: +387.
- Put strike range: 7140 to 7415.
- Call strike range: 7445 to 7605.

### DDQ March 2, 2026 Around 11:00 AM

- Rows: 13.
- Long calls: 66.
- Short calls: 66.
- Net calls: 0.
- Long puts: 264.
- Short puts: 264.
- Net puts: 0.
- Total long contracts: 330.
- Total short contracts: 330.
- Net contracts: 0.
- Put strike range: 6380 to 6760.
- Call strike range: 6905 to 6965.

### DDQ March 2, 2026 Around 3:00 PM

- Rows: 49.
- Long calls: 379.
- Short calls: 206.
- Net calls: +173.
- Long puts: 1,820.
- Short puts: 1,204.
- Net puts: +616.
- Total long contracts: 2,199.
- Total short contracts: 1,410.
- Net contracts: +789.
- Put strike range: 6380 to 6885.
- Call strike range: 6905 to 6965.

## Inferences From The Snapshots

The snapshots strongly support the retained-wing behavior:

- The 11:00 AM DDQ snapshot is balanced by option side: long contracts equal short contracts for both calls and puts.
- The 3:00 PM DDQ snapshot is materially net long, especially in puts.
- The user snapshot is also materially net long across calls and puts.
- This is consistent with short legs being stopped while long wings remain open.

The snapshots also imply that a fixed 25-point wing is not a good reconstruction assumption:

- User snapshot put shorts are around 7335-7415 while long puts are around 7140-7300.
- User snapshot call shorts are around 7445-7470 while long calls are around 7540-7605.
- DDQ 11:00 AM put shorts are around 6730-6760 while long puts are around 6380-6520.
- DDQ 11:00 AM call shorts are around 6905-6915 while long calls are around 6960-6965.

Better wing hypotheses:

- Wings are selected by far-OTM delta.
- Wings are selected by low absolute premium.
- Wings are selected by a target max-loss / margin-efficiency rule.
- Wings vary by time of day, VIX, and side.

The current simulator has been updated to support target-delta wing selection.

## Reverse-Engineering Approach From Position Snapshots

When more snapshots arrive, use this matching process:

1. Separate calls and puts.
2. Split each side into long and short strikes.
3. For active spreads, match each short to farther-OTM longs of the same option type and expiry.
4. Minimize mismatch by contract count, distance, and likely entry time.
5. Treat unmatched longs as retained wings from stopped shorts or standalone long-volatility hedges.
6. Treat unmatched shorts as active risk that must still have farther-OTM long protection somewhere in the book.
7. Compare net long count over time to stopped-short count.
8. Infer whether the day was dominated by delta-neutral entries, trend-breakout entries, or long-volatility overlays.

The key variable to estimate is not just strike distance. It is how many short contracts were opened, how many stopped, and which long wings remained after the stops.

## Simulator Changes Made From This Analysis

Implemented:

- Default entry start changed to 9:32 AM ET.
- Default last entry changed to 3:30 PM ET.
- Daily credit cap changed to 1.5% of account equity.
- VIX sizing now follows the DDQ tiers more closely.
- Policy can emit multiple trade instructions at the same timestamp.
- Delta-neutral policy can open both bull-put and bear-call spreads.
- Wing selection now supports target-delta wings with fixed-width fallback.
- 0DTE tradable expiry remains separated from 1DTE reference data.

Still to implement after data arrives:

- Feature generator for ATM Volatility Surface, Skew, Trend Breakout, and Durational Influence.
- Position-snapshot matching engine.
- Long-volatility hedge module.
- T-Bill carry attribution.
- Execution/slippage model from actual fills.
