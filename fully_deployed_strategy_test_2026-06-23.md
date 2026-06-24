# Fully Deployed Strategy Test

Date: 2026-06-23

## Purpose

Test whether the current best short-premium engine can approach MBH-like returns by increasing deployment, instead of only estimating deployment linearly.

This matters because a linear estimate does not let the simulator apply:

- daily credit caps,
- daily loss halts,
- stop behavior,
- contract rounding,
- skipped trades after halts,
- actual max margin reached.

## Important Account Scale

Current research account equity is $13,000,000.

At this equity:

- 1.5% daily credit cap = $195,000.
- 40% buying power / margin reference = $5,200,000.

The earlier $420,000 credit-cap number corresponds to a $28,000,000 account, not the current $13,000,000 scale.

## Implemented

Added:

- `simulator/deployment_sweep.py`

Generated:

- `data/deployment_sweep_guard_time_exploratory240.csv`
- `deployment_sweep_guard_time_exploratory240.md`
- `data/deployment_sweep_guard_time_exploratory240_credit5pct.csv`
- `deployment_sweep_guard_time_exploratory240_credit5pct.md`

The sweep reruns the actual simulator at higher baseline contract counts. It is not just a linear spreadsheet estimate.

## Current Strategy Under Test

Current best short-premium baseline:

- two-tier engine,
- event controls,
- time-of-day controls,
- exploratory bear-call guard,
- exploratory score band 2.40-2.49,
- $13,000,000 account equity.

Base run:

- Trades: 16
- Stops: 4
- Stop rate: 25.0%
- Net P&L: $22,525.75
- Max margin: $141,360
- Max margin / equity: 1.09%

## True Deployment Sweep With 1.5% Daily Credit Cap

| Target max margin | Contracts | Trades | Stops | Stop rate | Net P&L | Annualized return | Avg credit/day | Max margin | Avg margin | Worst day | Halted days |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 5% | 143 | 16 | 4 | 25.0% | $99,747.03 | 2.25% | $3,062.03 | $652,080 | $32,972.85 | -$49,184.85 | 0 |
| 10% | 285 | 16 | 4 | 25.0% | $199,675.92 | 4.50% | $6,086.45 | $1,299,600 | $65,541.45 | -$98,025.75 | 0 |
| 20% | 570 | 16 | 4 | 25.0% | $343,600.58 | 7.74% | $11,539.53 | $1,880,000 | $122,884.88 | -$251,361.70 | 0 |
| 40% | 1140 | 14 | 4 | 28.6% | $616,771.41 | 13.90% | $18,696.28 | $2,661,900 | $198,024.65 | -$391,777.50 | 2 |

The 40% target did not actually reach 40% max margin under the standard 1.5% daily credit cap. It reached 20.48% max margin because credit caps and daily loss halts started binding.

## Fully Deployed Stress With Relaxed 5% Credit Cap

This is not a production recommendation. It is a research stress test to see what happens if the current engine is allowed to use the intended 40% max margin.

| Target max margin | Contracts | Trades | Stops | Stop rate | Net P&L | Annualized return | Avg credit/day | Max margin | Avg margin | Worst day | Halted days |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 40% | 1140 | 16 | 4 | 25.0% | $796,890.77 | 17.96% | $24,375.81 | $5,198,400 | $262,490.47 | -$392,103.00 | 1 |

This reached 39.99% max margin, but still only annualized at about 18.0% over the Q2/Q3 test window. Worst day was about -3.0% of equity.

## Conclusion

The current vertical-spread engine does improve when sized up, but sizing alone does not bridge the MBH gap.

At full 40% max-margin deployment, the current strategy still falls short of MBH's stated 30%-40% annualized return range and carries a meaningful worst-day loss.

The blocker is not just under-deployment. The blocker is missing positive-expectancy trade expressions.

## What This Means For Next Improvements

The next work should not be "multiply contracts more." It should add new sleeves that can use capital differently.

Highest priority:

1. Add a 1DTE non-directional sleeve.
   - Uses already downloaded next-expiration chains.
   - Should trade when 0DTE is unattractive or too path-dependent.
   - Track separately from 0DTE verticals.

2. Add a portfolio allocator.
   - Allocate target margin by sleeve.
   - Track core vertical, exploratory vertical, 1DTE, condor, and long-vol hedge sleeves separately.
   - Enforce portfolio-level credit and margin caps.

3. Rebuild long-vol overlay around actual failures.
   - Current stopped trades are mostly real directional failures.
   - A useful hedge must reduce July 16 / August 20 style call-side failures and July 30 / August 7 put-side failures without dragging too much on normal days.

4. Keep condor disabled for production sizing.
   - It improves trade count and margin efficiency, but standalone attribution is still negative.

5. Improve margin reporting from "max margin by day" to true intraday margin path.
   - Current reporting is still approximate.
   - MBH comparison needs average intraday buying-power use, max intraday buying-power use, and sleeve-level buying-power use.

