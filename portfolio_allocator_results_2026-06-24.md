# Portfolio Allocator Results - 2026-06-24

## Intent

The strategy remains a 0DTE SPX/SPXW execution strategy. The 1DTE option chain is not traded in these tests. It is retained only as a term-structure input through the existing 0DTE versus next-expiration straddle relationship, which feeds `term_ratio_z` and the candidate scoring/risk gates.

## Implementation

Added an optional portfolio allocator to `simulator/mbh_simulator.py`.

Also hardened the simulator so the legacy 1DTE sleeve path no longer opens trades. Next-expiration data remains available to the signal builder for term-structure scoring, but 1DTE candidates are not executable.

Allocator controls:

- `portfolio_margin_budget_pct`
- `core_margin_budget_pct`
- `exploratory_margin_budget_pct`
- `condor_margin_budget_pct`
- `one_dte_margin_budget_pct`

The allocator runs after signal selection and normal risk checks, but before order opening. It sizes each candidate down or blocks it if the sleeve or total portfolio margin budget would be exceeded.

The allocator is exposed through:

- `simulator/regime_validation.py`
- `simulator/deployment_sweep.py`

The validation output now includes per-sleeve approximate spread margin columns:

- `core_approx_spread_margin`
- `exploratory_approx_spread_margin`
- `condor_approx_spread_margin`
- `one_dte_approx_spread_margin`

## Test Setup

Common setup:

- Account equity: `$13,000,000`
- Test window: `2025-04-01` through `2025-09-30`
- Rolling train count: `40`
- Strategy: current 0DTE two-tier candidate engine with event controls and time-of-day controls
- Core score: `2.50`
- Exploratory score band: `2.40` to `2.49`
- 1DTE sleeve: disabled
- Allocator budgets:
  - Portfolio: `40%`
  - Core: `35%`
  - Exploratory: `2%`
  - Condor: `0%`
  - 1DTE: `0%`

## Results

### 40% target, relaxed 5% daily credit cap

Output:

- `data/deployment_sweep_allocator_guard_time_exploratory240_credit5pct.csv`
- `data/deployment_sweep_allocator_guard_time_exploratory240_credit5pct/target_margin_40p0_contracts_1140/`

Summary:

- Days: `86`
- Trades: `16`
- 1DTE trades: `0`
- Stopped trades: `4`
- Stop rate: `25.0%`
- Net P&L: `$711,993.31`
- Annualized return: `16.05%`
- Gross credit sold: `$2,010,770`
- Max margin: `$4,548,800`
- Max margin as equity: `34.99%`
- Average margin: `$251,037.56`
- Average margin as equity: `1.93%`
- Worst day: `-$442,060.60`
- Worst day as equity: `-3.40%`
- Halted days: `1`

Prior comparable non-allocator run:

- Net P&L: `$796,890.77`
- Annualized return: `17.96%`
- Max margin: `$5,198,400`
- Max margin as equity: `39.99%`
- Worst day: `-$392,103.00`

Interpretation:

The allocator successfully capped max spread margin below the 40% portfolio target, but it did not improve this run. It reduced deployment and net P&L while the worst day became larger.

### 40% target, standard 1.5% daily credit cap

Output:

- `data/deployment_sweep_allocator_guard_time_exploratory240_credit1p5pct.csv`
- `data/deployment_sweep_allocator_guard_time_exploratory240_credit1p5pct/target_margin_40p0_contracts_1140/`

Summary:

- Days: `86`
- Trades: `14`
- 1DTE trades: `0`
- Stopped trades: `4`
- Stop rate: `28.57%`
- Net P&L: `$581,831.55`
- Annualized return: `13.11%`
- Gross credit sold: `$1,572,730`
- Max margin: `$2,661,900`
- Max margin as equity: `20.48%`
- Average margin: `$194,125.23`
- Average margin as equity: `1.49%`
- Worst day: `-$391,777.50`
- Worst day as equity: `-3.01%`
- Halted days: `1`

Prior comparable non-allocator run:

- Net P&L: `$616,771.41`
- Annualized return: `13.90%`
- Max margin: `$2,661,900`
- Max margin as equity: `20.48%`
- Worst day: `-$391,777.50`

Interpretation:

With the standard credit cap, the allocator only blocked two candidate records. The daily credit cap remains the main deployment constraint, so portfolio allocation has limited impact unless additional sleeves are added.

## Conclusion

The portfolio allocator is implemented and tested, but should not be considered a performance improvement yet. It is a necessary control layer for adding sleeves and for testing fully deployed portfolios, but the current 0DTE vertical-spread engine still lacks enough independent profitable trade opportunities to use 40% buying power efficiently.

The next improvement should not be another sizing multiplier. The next useful research step is to add a new 0DTE-only sleeve with a different payoff shape, then let the allocator decide whether it deserves capital. Recommended next candidates:

1. 0DTE broken-wing butterfly or asymmetric butterfly sleeve on clustered/rich-premium days.
2. 0DTE long-put-spread hedge only on actual failure-mode signatures.
3. 0DTE debit-spread trend sleeve when short-premium gates block selling but trend continuation is strong.
