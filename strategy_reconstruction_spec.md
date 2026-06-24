# Strategy Reconstruction Spec

Version: 0.1

Date: 2026-06-17

## Reconstruction Target

The target is the SPX-only 0DTE strategy described in the call, not the full broad mandate permitted by the PPM.

We are reconstructing the tradable mechanics and testing plausible versions of the undisclosed model. The unknown proprietary element is the exact mapping from option-chain signals to side, strike, and contract count. That piece must be inferred and tested from data.

## Instruments

- Underlying: SPX index options, preferably SPXW PM-settled same-day expirations.
- Expiry: 0DTE only for the main strategy.
- Reference expiry: 1DTE chain required for the 0DTE/1DTE ratio feature.
- Avoid SPY for the core recreation because the call explicitly rejected SPY due to assignment and scale issues.
- Avoid NDX for the core recreation because the call explicitly cited inferior exit liquidity.

## Session Rules

- Trade only during regular U.S. market hours.
- Start each day with no option positions.
- End each day in cash.
- No overnight option positions.
- Entry cadence: every 15 minutes in the disclosed baseline.
- Later capacity test: shorten cadence to 10-12 minutes or increase contracts per tranche.

DDQ-updated implementation assumptions:

- First eligible tranche: 9:32 AM New York time.
- Last eligible tranche: typically between 3:00 PM and 3:30 PM New York time.
- Forced settlement/flattening: 4:00 PM New York time.

These times remain configurable because the call mentioned occasional very late entries, while the DDQ states the typical last entry is between 3:00 PM and 3:30 PM.

## Trade Structure

Supported structures:

- Bull put spread: sell put short leg, buy lower-strike protective put.
- Bear call spread: sell call short leg, buy higher-strike protective call.

Primary short-leg target:

- Short leg delta: 15-25 delta.
- Starting target: 20 delta.

Protective wing target:

- The snapshots do not support a simple fixed 25-point wing.
- Actual wing distance appears wide and variable by side, time, and volatility regime.
- Current best reconstruction is to choose farther-OTM wings by target low delta or target low premium, with fixed-width fallback.
- Initial target long-wing delta: 5 delta until trade logs reveal the true rule.

Entry fill:

- Conservative default: sell short leg at bid and buy long wing at ask.
- Alternative modes to test later: midpoint, midpoint minus slippage, and limit-order fill probability.

## Position Sizing

Disclosed base tranche:

- 16 contracts per entry in normal conditions.
- Throttle to 8, 4, or 0 contracts when models disagree or expected value deteriorates.

Portfolio constraints from materials and DDQ:

- Average buying power use: about 40%.
- Maximum intraday margin utilization: about 70%.
- Average credit sold per day: about 1.5% of account equity, depending on regime.
- Call-level premium range: 1.5%-2.0% of account equity per day.
- Conservative daily loss control should be modeled around 2.25% of NAV for stop/pause behavior, with stress tests allowing 4%-5% bad-day losses.
- DDQ 99% 1-day VaR: -1.36%.
- DDQ CVaR / expected shortfall: -2.04%.

First implementation:

- Daily gross credit cap defaults to 1.5% of equity.
- Baseline tranche size defaults to 16.
- Signal policy may reduce size to 8, 4, or 0.
- New entries stop after the daily loss limit is breached.

## Stop And Wing Handling

Disclosed behavior:

- Stop loss is placed on the short leg only.
- Long wing is not sold when the short leg is stopped.
- Long wing remains as protection and can occasionally become profitable on large moves.

Initial stop rule:

- Stop is triggered when the short-leg ask reaches a configurable multiple of the short-leg entry premium.
- Default multiple: 2.0x.
- Sensitivity tests: 2.0x, 2.5x, 3.0x.

Example from call:

- If short-leg credit is around $4, stop may be around $8 or $12.

End-of-day P&L:

- If the short leg was not stopped: spread settles at intrinsic value.
- If the short leg was stopped: short-leg P&L is locked in at the stop fill and the long wing settles at intrinsic value.

## Disclosed Signal Families And DDQ Model Mapping

DDQ model 1: ATM Volatility Surface.

- Compute ATM 0DTE straddle value at each snapshot.
- Compare to a linear decay baseline from entry time to close.
- Estimate residual richness/cheapness by time of day and volatility regime.
- Hypothesis: rich residual can increase premium-selling compensation; cheap residual can reduce expected value.
- DDQ says this model identifies attractive environments for high expected-value risk-premium sales through delta-neutral trades.

DDQ model 2: Skew.

- Compute delta-matched skew, such as 15-delta and 25-delta put IV minus call IV.
- Compare to historical time-of-day norms.
- Hypothesis: abnormal skew can indicate either opportunity or danger depending on regime and side.
- DDQ frames this primarily as a filter for unattractive risk-premium environments.

DDQ model 3: Trend Breakout.

- Identify rapid changes in options pricing.
- Use as a directional expression layer.
- Directional outputs can favor bull-put spreads, bear-call spreads, or no trade.

DDQ model 4: Durational Influence.

- Compare 0DTE ATM straddle value, implied variance, or implied volatility against 1DTE.
- Compare to stable historical ratio bands by time of day and event type.
- Hypothesis: ratio dislocation marks unusual event risk or overpaid same-day premium.
- DDQ says this model identifies when 0DTE movement is driven by events or flows outside the 0DTE space, allowing low expected-value trades to be eliminated.

Portfolio expression:

- The four models can express either delta-neutral or directional trades.
- MBH says this creates eight base strategy expressions.
- Full model confluence adds two ensemble expressions.
- Four to eight long-volatility strategies are layered daily as dynamic hedges.

Additional reconstruction candidates:

- Realized volatility versus implied volatility.
- Intraday trend and range expansion.
- VIX/VIX1D/VIX9D regime.
- Event calendar filters.
- Quote width and displayed size.
- Dealer gamma and strike concentration proxies.

## First-Pass Signal Policy

The initial code includes a placeholder policy so the simulator can run before the real model is inferred.

Rules are intentionally simple:

- Trade larger when 0DTE premium appears rich and other dislocation measures are not severe.
- Trade smaller or skip when premium is cheap, skew is extreme, 0DTE/1DTE ratio is extreme, or VIX is in a low expected-value zone.
- Choose bull put spreads in positive trend conditions.
- Choose bear call spreads in negative trend conditions.
- Use skew as a fallback side-selection proxy when trend is neutral.
- Allow both bull-put and bear-call spreads in the same timestamp when the model expression is delta-neutral.

This policy is not the final edge. It is a harness for testing candidate rules.

## Calibration Targets

Targets from MBH materials:

- 2024 audited net return: 26.49%.
- 2025 audited net return: 32.44%.
- 2026 YTD interim returns from the Google workbook.
- Claimed long-run net target: roughly 30%-32%.
- Claimed gross target: roughly 40%.
- Claimed win rate: about 65%.
- Claimed worst bad day: roughly 4%-5%.
- Claimed daily premium sold: 1.5%-2.0% of equity.
- Claimed average buying power use: 40%.
- Claimed beta/correlation near zero.

Calibration must not overfit to these targets. The first goal is to match mechanics and risk shape, then test out-of-sample.

## Minimum Acceptable Backtest Standard

A candidate recreation must include:

- 0DTE and 1DTE chain data.
- Realistic bid/ask fills.
- Stop slippage.
- Fees and exchange costs.
- No hindsight strike selection.
- No using end-of-day results in entry decisions.
- Walk-forward signal calibration.
- Separate test periods by VIX regime and event calendar.
- Performance reported net of realistic execution assumptions.

## What Would Count As A Plausible Recreation

A candidate strategy becomes plausible only if it produces:

- Positive net expectancy per tranche after costs.
- Sharpe above 1.5 in out-of-sample testing.
- Max drawdown and worst-day behavior consistent with the claimed strategy.
- Low correlation to SPX over daily and monthly horizons.
- Sensitivity to low-VIX environments consistent with MBH's own FAQ.
- No dependence on unrealistic midpoint fills or impossible stop execution.

## What Would Disprove The Recreation

The reconstruction is not credible if:

- Returns disappear under bid/ask fills.
- Stop slippage turns the payoff materially worse than advertised.
- Profits come only from a few overfit filters.
- The strategy requires data fields unavailable at decision time.
- Low-VIX periods produce persistent negative expectancy.
- Capacity tests show material deterioration at current or target AUM.
