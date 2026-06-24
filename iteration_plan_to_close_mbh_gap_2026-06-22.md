# Iteration Plan To Close The MBH Gap

Date: 2026-06-22

## Objective

Improve the current SPX 0DTE reconstruction without pretending that a tuned backtest is the same as MBH's proprietary strategy.

The goal is to move from a conservative, low-deployment engine toward a credible MBH-style book:

- positive expectancy per trade after realistic fills,
- materially higher trade frequency,
- higher but controlled margin utilization,
- a book structure that resembles MBH snapshots,
- long-volatility and T-Bill attribution included separately,
- daily returns and risk shape that can be compared to MBH records once available.

## Current Gap

All current figures use the scaled $13,000,000 account.

| Item | Current event-aware two-tier | Current quiet/rich condor retest | MBH-style target / claim |
| --- | ---: | ---: | ---: |
| Test window | 86 days | 86 days | full year / rolling quarters |
| Net P&L | $5,959.81 | $20,632.79 | $3.9M-$5.2M per year for 30%-40% |
| Annualized return | 0.13% | 0.47% | 30%-40% |
| Avg net P&L needed | n/a | n/a | $15.5K-$20.6K per trading day |
| Avg daily credit sold | $986 | $671 | about $195K/day if 1.5% of equity |
| Max margin observed | $141K | $86K | about $5.2M at 40% average margin use |
| Max margin / equity | 1.09% | 0.66% | about 40% average, up to 70% max |
| Stop rate | 32.4% | 26.2% | not directly disclosed, but DDQ references about 65% win rate |

The main conclusion is that the current engine is not just missing return. It is not using anything close to the capital, premium, or multi-strategy structure that MBH describes.

## What Is Standing In The Way

### 1. We Are Underdeployed By Design

The current engine is intentionally conservative. That kept the worst failures under control, but it also reduced the book to a high-conviction subset.

At $13M, MBH-style 1.5% daily gross premium would imply roughly $195,000 of credit sold per day. Our continuous Q2/Q3 tests sold roughly $671-$986 per day on average. That is about 200x below the referenced daily credit target.

Scaling current trades up by 200x is not acceptable because the exploratory and condor sleeves are not positive enough on their own attribution.

### 2. The Exploratory Sleeve Is Not Yet An Edge

The core sleeve is the best-performing part of the book. The exploratory sleeve adds frequency but has produced a high stop rate and negative standalone P&L.

Until the exploratory sleeve has positive expectancy, we should not use it as the bridge to MBH-like frequency.

### 3. The Book Structure Is Too Simple

MBH materials describe multiple expressions:

- 0DTE non-directional volatility,
- 0DTE signal-based trend following,
- 1DTE non-directional volatility,
- tail-risk hedges,
- 12-20 live strategy expressions depending on the source.

Our current implementation is still mostly vertical spreads plus a small condor experiment. It does not yet fully model simultaneous delta-neutral, directional, 1DTE, and hedge books.

### 4. We Do Not Yet Know The Proprietary Permission Model

The unknown edge is probably not "sell a default spread every 15 minutes." It is the mapping from vol surface, skew, trend breakout, term structure, event context, and quote quality into:

- trade / no trade,
- put side / call side / both sides,
- structure type,
- short-leg delta,
- wing distance,
- tranche size,
- stop behavior,
- whether to add hedges.

Our current score is a useful placeholder, but it is still too blunt.

### 5. Stop Modeling May Be Wrong

We use 1-minute option quotes and simplified stop fills. Many stopped trades may depend on intra-minute quote path, bid/ask width, and whether the short-leg ask briefly spikes.

The generated stopped-trade windows are now the right next data set. We need tick or 10-second data around stops before trusting stop-rate conclusions or tuning stop multiples.

### 6. The Long-Volatility Overlay Is Not Integrated

MBH materials attribute roughly 10% of return to long-volatility hedges and describe tail hedging as part of the daily book.

Our current long-vol overlay is not yet built from actual failure modes. It should be driven by the stopped-trade paths, not by generic crash assumptions.

### 7. We Are Missing T-Bill Carry And Full Fund Economics

MBH materials attribute a portion of total returns to T-Bills or treasury yield. Our option simulation currently excludes that carry.

This will not close the whole gap, but it matters when comparing fund-level returns to strategy-level returns.

### 8. We Still Need MBH Daily Returns, Screenshots, Or Fills

Without MBH daily returns and more position screenshots, we can only test plausible reconstructions. We cannot confirm that our path, exposure, or trade selection matches MBH.

The most valuable outside data would be:

- daily return sheet,
- timestamped position screenshots,
- actual fills or broker exports,
- margin/buying-power snapshots,
- examples of no-trade or reduced-size days.

## Iteration Plan

## Phase 1: Lock The Continuous Baseline

Purpose: make every future change measurable.

Work:

- Freeze the current Q2/Q3 2025 baseline outputs.
- Add a single comparison report that always shows P&L, credit, margin, stops, no-trade days, and sleeve attribution.
- Report annualized return, average daily credit as percent of equity, and max margin as percent of equity.

Acceptance gate:

- Every new strategy run must beat the current event-aware core on risk-adjusted metrics, not only trade count.

## Phase 2: Diagnose Stopped Trades With Microstructure Data

Purpose: determine whether the main losses are real signal failures or stop-fill artifacts.

Work:

- Download tick or 10-second data only for the 11 baseline stopped-trade windows first.
- Reconstruct short-leg bid/ask path, spread value path, and spot path.
- Classify each stop as:
  - real directional failure,
  - quote-width / mark artifact,
  - same-minute spike,
  - late-day reversal,
  - event shock,
  - avoidable re-entry.
- Test stop alternatives:
  - 2.0x, 2.5x, 3.0x short-leg stop,
  - spread-value stop,
  - two-sample confirmation stop,
  - no same-side re-entry after a stop.

Acceptance gate:

- Improve stop loss per stopped trade without hiding tail risk.
- Keep any stop-rule change valid using only information available at the time.

## Phase 3: Rebuild The Exploratory Sleeve

Purpose: recover frequency through positive-expectancy trades, not looser filters.

Work:

- Split exploratory candidates by event bucket, time of day, side, delta, score band, trend score, skew z-score, term-ratio z-score, and straddle residual z-score.
- Find contexts where exploratory trades are positive before scaling.
- Add stricter rules:
  - no exploratory entries after 14:30,
  - no same-side exploratory re-entry after a stop,
  - require stronger confirmation after 10:00,
  - block repeated same-side strike clusters,
  - block event buckets with repeated exploratory stops.
- Consider making the 2.35-2.50 band half-size core rather than exploratory.

Acceptance gate:

- Exploratory sleeve must be positive standalone over Q2/Q3.
- Stop rate should fall below 20%-25% before any size increase.

## Phase 4: Build True Multi-Expression Strategy Outputs

Purpose: move closer to MBH's described structure.

Work:

- Separate the engine into independent strategy expressions:
  - rich-vol delta-neutral short premium,
  - skew-filtered bull-put,
  - skew-filtered bear-call,
  - trend-breakout bull-put,
  - trend-breakout bear-call,
  - term-structure/durational block or trade,
  - quiet-regime condor,
  - event-regime reduced-size trade.
- Allow multiple expressions at the same timestamp only when they are independently approved.
- Track each expression's P&L and stop behavior separately.

Acceptance gate:

- At least two non-core expressions must show positive standalone expectancy before combined scaling.
- The combined book must increase credit sold without increasing worst-day risk materially.

## Phase 5: Rebuild Structure Selection

Purpose: stop forcing every opportunity into the same vertical-spread mold.

Work:

- Retest structure types by regime:
  - vertical spreads,
  - iron condors,
  - broken-wing butterflies,
  - wide-wing spreads,
  - 1DTE non-directional structures,
  - retained-wing behavior after stopped shorts.
- Select wings by delta, premium, and margin efficiency rather than fixed width.
- Compare candidate structures using bid/ask fills, not midpoint fantasy fills.

Acceptance gate:

- Structure selection must improve expectancy or reduce stop severity in at least one identifiable regime.
- Condors remain disabled for production sizing until positive standalone attribution.

## Phase 6: Add The 1DTE Non-Directional Sleeve

Purpose: test whether MBH-like return stability comes partly from a 1DTE book, not only 0DTE.

Work:

- Use the already downloaded next-expiration chains.
- Build a small 1DTE non-directional module.
- Track it separately from the 0DTE book.
- Test whether 1DTE helps on days where 0DTE premium is unattractive or too event-driven.

Acceptance gate:

- 1DTE sleeve must add return or reduce volatility after realistic fills.
- It cannot mask losses in the 0DTE engine.

## Phase 7: Build The Long-Vol Overlay From Actual Failure Modes

Purpose: create hedges that respond to the short-premium engine's real loss paths.

Work:

- Use stopped-trade classifications from Phase 2.
- Test small debit structures only when the setup resembles prior failures:
  - put debit spreads,
  - call debit spreads,
  - broken-wing long convexity,
  - retained long-wing inventory after stopped shorts.
- Size daily hedge spend around 0.025%-0.10% of equity until proven.

Acceptance gate:

- Overlay improves worst-day and drawdown behavior without consuming normal-day expectancy.
- Overlay attribution is reported separately.

## Phase 8: Add Capital Deployment Ladder

Purpose: scale only after expectancy is proven.

Work:

Test deployment tiers:

- Tier 0: current conservative sizing, about 1% max margin observed.
- Tier 1: 2%-3% max margin.
- Tier 2: 5%-7% max margin.
- Tier 3: 10%-15% max margin.
- Tier 4: 20%-30% max margin.
- Tier 5: MBH-like 40% average margin only after robust evidence.

At each tier, track:

- net return,
- gross credit sold,
- stop losses,
- worst day,
- average and max margin,
- daily loss halt frequency,
- capacity and slippage penalty.

Acceptance gate:

- Do not advance to the next tier unless incremental size preserves positive expectancy and acceptable worst-day behavior.

## Phase 9: Expand Continuous Data

Purpose: prevent overfitting Q2/Q3 2025.

Work:

- Download and process Q4 2025.
- Download and process Q1 2026 through the DDQ screenshot dates.
- Backfill enough Q1/Q2 2025 to create more stable rolling baselines.
- Label CPI, FOMC, NFP, OpEx, tariff/geopolitical shocks, post-shock normalization, quiet grind, and low-VIX compression days.

Acceptance gate:

- Strategy remains profitable across at least 150-250 out-of-sample trading days.
- Results are not dominated by one month or one regime.

## Phase 10: Calibrate Against MBH Evidence

Purpose: compare behavior, not just returns.

Work:

- Use MBH screenshots to compare:
  - long/short contract counts,
  - call/put exposure,
  - strike distances,
  - retained long wings,
  - active strategy count,
  - intraday book evolution.
- Use daily returns once available to compare:
  - direction of daily P&L,
  - volatility,
  - drawdowns,
  - correlation to SPX,
  - no-trade or low-size days.

Acceptance gate:

- The simulated book should look structurally similar at 10:00, 13:00, and 15:30.
- Daily return correlation should improve without hand-fitting individual dates.

## Phase 11: Add Realistic Execution And Capacity

Purpose: ensure any apparent edge survives real trading assumptions.

Work:

- Model contract-count slippage.
- Add fees and exchange costs.
- Penalize wide markets and low displayed size.
- Test 15-minute, 12-minute, and 10-minute entry cadence.
- Stress from $13M to larger AUM levels.

Acceptance gate:

- Strategy remains viable under conservative bid/ask and slippage assumptions.
- Capacity does not depend on impossible fills.

## Immediate Next Work

1. Download tick or 10-second data for the 11 baseline stopped-trade windows.
2. Build the stopped-trade classifier.
3. Rework exploratory sleeve rules using the Q2/Q3 continuous sample.
4. Add a comparison report that makes return, credit, and margin gaps visible on every run.
5. Only after the exploratory sleeve is positive, begin the capital deployment ladder.

## What Would Make This A Credible MBH-Like Recreation

The strategy becomes credible only when all of these are true:

- positive expectancy per sleeve,
- materially higher credit sold without simply magnifying losses,
- average margin use moves toward MBH-like levels in controlled steps,
- stop behavior survives microstructure data,
- long-volatility hedge improves actual observed failures,
- 1DTE and T-Bill attribution are modeled separately,
- simulated positions resemble MBH screenshots,
- daily return behavior matches MBH records out of sample.

## Source Notes

Public MBH pages currently describe a systematic 0DTE index options hedge fund, a 40% targeted annual return / 10% quarterly target, 70% liquidity language, multiple portfolios including 0DTE non-directional, 0DTE trend-following, 1DTE non-directional, and tail-risk hedging, plus a multi-strategy ecosystem. Treat these as marketing/source claims until reconciled against audited records and actual daily return sheets.

- https://www.mbhcapitalmanagement.com/
- https://www.mbhcapitalmanagement.com/accredited-investors

