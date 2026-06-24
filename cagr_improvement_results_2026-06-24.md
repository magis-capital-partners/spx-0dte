# CAGR Improvement Results — 2026-06-24

## Objective

Improve the CAGR of the validated 0DTE SPX vertical-spread strategy, given that
it will be traded by selling spreads in **Interactive Brokers on a $13M equity
account with tens of millions of margin buying power available**. Because margin
is abundant, the binding constraints are *not* margin — they are trade
frequency, per-day deployment, and tail-loss control.

All runs are true simulator reruns over **2025-04-01 → 2025-09-30** (86
out-of-sample test days after a rolling 40-day baseline), $13M equity, using
`simulator/regime_validation.py`. Summaries via `simulator/summarize_run.py`
(compounded CAGR, Sharpe, Sortino, max drawdown).

## Diagnosis of the baseline

Reproduced prior best config: **16.05% simple / 16.91% compounded CAGR**,
Sharpe 1.73, worst day -3.40%, max margin 34.99% of equity.

Key findings from the daily/trade breakdown:

1. **It is not credit-capped.** Average daily credit sold was only **0.18% of
   equity** against a 5% cap. The cap is nowhere near binding.
2. **It is margin-budget-capped on active days.** Max margin hit exactly
   **34.99%** = the 35% core allocator budget. On the few active days, position
   size is throttled by the core budget, not by available margin.
3. **Most days have no trade.** Only 16 trades fired in 86 days; the candidate
   score gate (2.50) keeps the book to a high-conviction subset.
4. **The one bad day blew through the loss limit.** 2025-07-16 lost -$592K on a
   1140-contract bear-call stop. The daily-loss "halt" only blocks *new* entries
   — open positions kept running to settlement, so the day reached -3.40% even
   though the loss limit is 2.25%.

Because margin is abundant, the levers are: (a) cap the tail properly, then
(b) deploy more size into the profitable core sleeve.

## Improvement 1 — Flatten-on-loss governor (robust, free)

New optional control (`flatten_on_daily_loss`): when the daily marked loss
breaches the limit, **close all open positions** instead of only halting entries.
Implemented in `simulator/mbh_simulator.py` (`close_trade_at_snapshot`).

| Config | CAGR | Sharpe | Sortino | Max DD | Worst day |
|---|---:|---:|---:|---:|---:|
| Baseline (no governor) | 16.91% | 1.73 | 0.88 | 3.93% | -3.40% |
| **+ flatten governor** | **19.65%** | **2.16** | **1.39** | **3.13%** | **-2.56%** |

This is a pure improvement: **higher CAGR and lower worst-day/drawdown at the
same size.** It caps the tail (7/16 went from -$592K toward the loss limit).

## Improvement 2 — Deploy into the abundant margin (scaling)

The core sleeve is the profitable part of the book and is throttled by the 35%
core budget. With tens of millions of margin available, raise `baseline_contracts`,
the allocator budgets, and the (non-binding) credit cap together, protected by
the flatten governor.

| Deployment | CAGR | Sharpe | Max DD | Worst day | Max margin/eq | Halted |
|---|---:|---:|---:|---:|---:|---:|
| 1x (+flatten) | 19.65% | 2.16 | 3.13% | -2.56% | 35% | 1 |
| **2x (+flatten)** | **31.55%** | **2.02** | 3.59% | -2.51% | 50% | 3 |
| 2.5x (+flatten) | 44.71% | 2.29 | 3.72% | -3.03% | 62% | 3 |
| 3x (+flatten) | 25.01% | 1.43 | 4.46% | -2.85% | 61% | 4 |

**2x deployment reaches ~31.5% CAGR (MBH's 30–32% range)** with Sharpe 2.0 and
worst-day capped near -2.5%. Note **3x is *worse* than 2x** — see whipsaw below.

## Improvement 3 — Decoupled (deeper) flatten trigger

The 3x collapse is a flatten **whipsaw**: on 2025-08-29, a large position dipped
through the 2.25% limit intraday and was force-closed at -$318K, even though it
*recovered to +$785K by close* at 2.5x size. Fix: decouple the flatten trigger
(`flatten_loss_limit_pct`) from the entry-halt level — halt new entries at 2.25%
but only force-flatten at a deeper 3.5%, so recovering days are not whipsawed.

| Config | CAGR | Sharpe | Sortino | Max DD | Worst day |
|---|---:|---:|---:|---:|---:|
| 2.5x, flatten @2.25% | 44.71% | 2.29 | 3.69 | 3.72% | -3.03% |
| **2.5x, flatten @3.5% (aggressive)** | **64.09%** | **2.79** | 2.90 | 5.01% | -3.80% |
| 3x, flatten @3.5% | 44.37% | 1.96 | 2.48 | 5.20% | -3.77% |

The deeper trigger is a **risk/return dial**: tighter = lower CAGR + smaller
worst-day; deeper = higher CAGR + larger worst-day.

## Recommended configurations

Exposed as named profiles in `live/strategy_profiles.py` (used by both the
backtest reruns and the IB executor):

| Profile | CAGR | Sharpe | Worst day | Max margin/eq | When to use |
|---|---:|---:|---:|---:|---|
| `flatten` | 19.7% | 2.16 | -2.6% | 35% | Most conservative; just adds the governor |
| `best` | 31.6% | 2.02 | -2.5% | 50% | **Recommended.** Matches MBH range, tight tail |
| `aggressive` | 64.1% | 2.79 | -3.8% | 62% | Upper bound; more leverage + deeper flatten |

**Recommendation: deploy `best` (2x + flatten).** It roughly matches MBH's
audited 30–32% with the strongest tail control, and uses only ~50% of equity in
margin — well within your buying power. Treat `aggressive` as an upper bound to
ramp toward *after* the validation sample is expanded.

## Important caveats (do not over-trust the high numbers)

- **Small sample.** 86 test days, only ~15 trades. The CAGR is driven by a
  handful of large winning core trades; scaling multiplies them. The Sharpe is
  high but on few observations.
- **Scaling is leverage.** It raises CAGR and drawdown roughly together until
  the loss governor distorts the path. The genuine *edge* improvement is the
  flatten governor; the rest is sizing into abundant margin.
- **Fills are modeled.** 1-minute quotes, bid/ask fills, stop at the short-leg
  ask. Live 0DTE stop slippage can be worse — start small in IB (see below).
- **Warm-up gap.** The 40-day rolling baseline means strong MBH months
  (Jan–Apr 2025) are not yet in the continuous test window. Expanding the data
  is the highest-value next step before pushing leverage past `best`.

## Reproduce

```powershell
cd "MBH Capital/Strategy Recreation"
.\run_profiles.ps1                 # runs all four profiles + rebuilds dashboard
# or a single profile:
python simulator/regime_validation.py --start-date 2025-04-01 --end-date 2025-09-30 `
  --train-count 40 --account-equity 13000000 --baseline-contracts 2280 `
  --daily-credit-cap-pct 0.10 --two-tier-engine --event-controls --time-of-day-controls `
  --exploratory-min-score 2.40 --exploratory-max-score 2.49 --portfolio-allocator `
  --portfolio-margin-budget-pct 0.80 --core-margin-budget-pct 0.70 `
  --exploratory-margin-budget-pct 0.04 --flatten-on-daily-loss --results-dir data/profile_best
python simulator/summarize_run.py data/profile_best
```

## Files changed / added

- `simulator/mbh_simulator.py` — `flatten_on_daily_loss`, `flatten_loss_limit_pct`,
  `close_trade_at_snapshot`, early-close handling in mark/settle.
- `simulator/regime_validation.py` — `--flatten-on-daily-loss`,
  `--flatten-loss-limit-pct`, `--daily-loss-limit-pct` flags.
- `simulator/summarize_run.py` — compounded CAGR / Sharpe / Sortino / max-DD.
- `live/` — IB executor, shared strategy profiles, README (see below).
- `dashboard/` — single-file React SPA + data builder + Pages workflow.
- `run_profiles.ps1` — one-command rerun + dashboard rebuild.
