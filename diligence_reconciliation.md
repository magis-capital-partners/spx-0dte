# Diligence Reconciliation

Date: 2026-06-17

## Agreed Facts Across Materials

- The main marketed strategy is same-day index option trading.
- The investor-call version is SPX-only 0DTE.
- The strategy is automated and quantitative.
- The strategy relies on short option premium with defined-risk hedges.
- The fund is intended to end each day in cash.
- The fund emphasizes low equity beta/correlation.
- Position sizing, defined-risk structures, and stop losses are the main risk controls.
- 2024 and 2025 returns are audited.

## Performance Reconciliation

Audited returns:

- Period from inception through 12/31/2024: 26.49% net after incentive allocation.
- FY 2025: 32.44% net after incentive allocation.
- Cumulative through 12/31/2025 per workbook reconciliation: 67.52%.

Marketing/deck targets:

- Target net CAGR: about 30%-32%.
- Target gross return: about 40%.
- FAQ confidence language: 95% confidence between 30%-50% and 99.7% confidence between 20%-60%.

Reconstruction implication:

- We should calibrate to 30%-40% annual gross/net range only after realistic costs.
- The workbook backtest showing much higher CAGR is not by itself enough; it must be reproduced from raw option-chain data.

## AUM Reconciliation

Observed figures:

- 2024 audit: roughly $4.4M members' capital.
- 2025 audit: roughly $26.3M members' capital.
- April 2026 materials/workbook: roughly $27M-$28M in Fund 1.
- Investor call: roughly $35M-$38M AUM.

Open question:

- Determine whether the call-level AUM includes Fund 2, pending subscriptions, managed accounts, or post-April growth.

## Liquidity Term Conflict

Marketing/FAQ:

- No lockup.
- No gate.
- No early withdrawal penalty.
- Withdrawal distributed 5-9 business days after month end.

2025 audit language:

- Withdrawals on the last day of the fiscal quarter with digital request 14 days prior.

Reconstruction implication:

- Not directly relevant to trading logic, but important for fund diligence and liquidity risk.

Question for MBH:

- Which liquidity language controls Fund 1 today?

## Minimum Investment Conflict

PPM:

- $25,000 minimum, manager discretion.

FAQ / marketing sheet:

- Fund 1 minimum appears to be 2.5% of fund assets, shown as $684,825 in the exported sheet.
- Other materials and call context imply low millions are typical.

Question for MBH:

- What is the current actual minimum and does it differ by investor class or side letter?

## Strategy Scope Conflict

Call:

- SPX exclusively.
- 0DTE only.
- No overnight risk.
- Risk-defined vertical spreads.

PPM:

- Broader mandate: SPX, QQQ, IWM, GLD, SPY, TLT, XSP.
- 0-14 DTE.
- Long options for hedging.
- Treasuries.
- Longer-duration strategies.
- Opportunistic stocks.

Fund summary:

- Mentions hedge toolkit with 0-14 DTE long straddles, opportunistic long wings, calendars, direct SPX puts, and VIX-linked call ladders.

Reconstruction decision:

- Build Fund 1 SPX 0DTE core first.
- Treat VIX calls, 0-14 DTE hedges, calendars, and non-SPX instruments as optional overlay modules only if execution records prove they are used in Fund 1.

## Capacity Conflict

Call:

- Strategy should scale cleanly to $100M.
- Scaling can happen by increasing contracts or shortening tranche interval from 15 minutes to 10-12 minutes.

Tear sheet:

- Capacity analysis states $500M total capacity.

Question for MBH:

- Is $500M a fund-family capacity estimate and $100M the SPX 0DTE core capacity estimate?
- Show expected slippage and fill quality at $100M and $500M.

## Execution Cost Reconciliation

Call:

- Retail Interactive Brokers example: $0.70 per contract.
- Fund rate: $0.17 per contract.
- Prime broker / faster refresh could save roughly $0.50 per contract in slippage.

Fund summary:

- $0.15 per contract commission plus Cboe fee/rebate details.
- Around $0.79 all-in cost per contract referenced.
- Non-volume practitioners around $1.22.

Reconstruction implementation:

- Simulator defaults to $0.79 per contract all-in cost.
- Sensitivity tests must include $0.17, $0.50, $0.79, $1.22, and slippage-by-regime.

## Critical Proof Requests

For strategy recreation:

- Full execution export for at least 20 representative days.
- Full execution export for the worst 10 days and best 10 days.
- Daily position screenshots at 10:00 AM, 1:00 PM, and 3:30 PM.
- Exact option-chain data source used in backtests.
- Definitions of the three disclosed signal families.
- Stop-loss rule and whether it is based on short leg mark, ask, bid, midpoint, spread mark, or broker stop order.
- Strike delta target, wing width, minimum credit, and side-selection logic.
- Daily loss stop behavior: stop new entries only or flatten all positions.

For investor diligence:

- Current governing liquidity terms.
- Current minimum investment.
- Current capacity analysis.
- Explanation of Fund 1 versus Fund 2 and whether performance records commingle strategy families.
