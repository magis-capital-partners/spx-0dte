# Compounding Position Sizing Test Plan

Date: 2026-07-26  
**Executed:** 2026-07-27 — see `compounding_sizing_results_2026-07-27.md` and dashboard preset `p3_poststop_compounding_f1`.

Baseline: `p3_poststop_cooldown_120` (production), 1514 eligible OOS days,
2019-04-15 → 2026-07-24 (6.01 yr), `account_equity = $13,000,000`,
`baseline_contracts = 31`, `linear_decay_downsize` TOD, production VIX policy.

## 1. Objective

Today every OOS day is traded at the same size: `baseline_contracts = 31` against a
constant `$13M` notional. Profits are never reinvested — `portfolio_stats` compounds
equity for *reporting* only (`simulator/portfolio_metrics.py:51-69`), while
`simulate_day` sees a fixed `config.account_equity` every single day.

This plan measures what happens when contract count tracks the running equity base:

```
k_t = E_t / E_0                       (E_t = equity at the OPEN of day t)
baseline_contracts_t = round(31 * k_t)
account_equity_t     = E_0 * k_t
```

Both the size *and* every `pct × account_equity` risk governor scale together, so the
strategy's risk profile stays constant in percentage terms as the book grows.

## 2. Why this is mostly an arithmetic question, not a simulation question

`simulate_day` is **homogeneous of degree 1** in `(contracts, account_equity)`. Every
equity-dependent threshold in `simulator/mbh_simulator.py` is `pct × account_equity`:

| Line | Threshold |
|---|---|
| 2533 | `daily_credit_cap = account_equity * daily_credit_cap_pct` |
| 2534 | `daily_loss_limit = -account_equity * daily_loss_limit_pct` |
| 2536 | `flatten_loss_limit = -account_equity * flatten_loss_limit_pct` |
| 2588-2589 | VIX-tight halt / flatten limits |
| 2607, 2730 | `intraday_size_cut_pct` trigger |
| 2447-2448 | allocator sleeve / portfolio budgets (inactive: `use_portfolio_allocator=False`) |

Fees and slippage are per-contract, and there is **no market-impact or fill-size model**
— quantity never affects fill price. So scaling contracts and equity by the same `k`
scales every trade's P&L by exactly `k` and leaves every gate decision identical.

The consequence: under exact proportional compounding, the **daily return series is
unchanged** (`r_t = pnl_t / E_t` is invariant). Only the equity path changes, from
additive `E_0(1 + Σr_t)` to multiplicative `E_0 Π(1 + r_t)`. That means the answer is
computable in closed form from the existing `daily_summary.csv`, and re-simulation
serves only to validate the homogeneity assumption and capture integer rounding.

**Caveat that re-simulation exists to catch:** `round()` on contracts, `max(1, ...)`
floors in `_condor_contracts`, and the absolute `max_contracts_per_tranche = 48` cap,
which must be scaled by `k` or it will bind hard and silently cap the whole experiment.

## 3. Phase 0 result — already computed

Closed-form on the existing production daily P&L path:

| | Terminal | CAGR | Max DD | Calmar | Sharpe | Worst day |
|---|---|---|---|---|---|---|
| **Fixed 31 lots (production)** | 2.97× ($38.6M) | 19.84% | 8.29% | 2.39 | 1.84 | −6.62% |
| **Full compounding (f=1)** | **6.47× ($84.2M)** | **36.47%** | **15.71%** | 2.32 | 1.83 | −6.62% |

Compounding roughly **doubles CAGR and roughly doubles max drawdown**. Calmar is
essentially unchanged (2.39 → 2.32). That is the expected result for a scale-invariant
policy and it is the honest framing: compounding is not alpha, it is leverage applied
through time.

A second, less obvious read: the production 8.29% max DD is *flattered by not
compounding*. The largest drawdowns happen late in the sample when equity has already
tripled, so a fixed-dollar loss is a small percentage of the grown base. **15.71% is the
stationary drawdown risk of this strategy** — the number to expect at any point in time
once size tracks equity. The worst single day is −6.62% of equity either way.

## 4. Phase 3 variant grid — analytic preview

Same closed-form machinery, `k_t = (E_t/E_0)^f` and capped / ratcheted forms:

| Variant | Terminal | CAGR | Max DD | Calmar | Sharpe | Peak k |
|---|---|---|---|---|---|---|
| fixed (f=0) | 2.97× | 19.84% | 8.29% | 2.39 | 1.84 | 1.00 |
| fractional f=0.25 | 3.33× | 22.15% | 8.82% | 2.51 | 1.85 | 1.36 |
| fractional f=0.50 | 3.87× | 25.24% | 9.41% | **2.68** | 1.86 | 2.00 |
| fractional f=0.75 | 4.76× | 29.63% | 11.56% | 2.56 | 1.86 | 3.35 |
| **full f=1.00** | 6.47× | 36.47% | 15.71% | 2.32 | 1.83 | 7.02 |
| full, cap 2× | 4.47× | 28.31% | 10.80% | 2.62 | 1.84 | 2.00 |
| full, cap 3× | 5.45× | 32.60% | 11.54% | **2.82** | 1.89 | 3.00 |
| full, cap 4× | 6.08× | 35.04% | 13.91% | 2.52 | 1.90 | 4.00 |
| full, cap 6× | 6.54× | 36.68% | 15.71% | 2.33 | 1.86 | 6.00 |
| full, HWM-only sizing | 7.04× | 38.38% | 16.89% | 2.27 | 1.85 | 7.62 |
| full, 63-day ratchet | 6.18× | 35.42% | 15.95% | 2.22 | 1.85 | 6.34 |
| full, ±10% resize band | 6.37× | 36.08% | 16.15% | 2.23 | 1.84 | 6.62 |

**Read these with suspicion.** Only `f=1` is a genuinely scale-invariant policy. Every
cap and every `f<1` embeds a dollar level or a calendar position, so its ranking depends
on *where in the equity path this particular sample's drawdowns happened to land*. The
"cap 3× wins Calmar 2.82" line is almost certainly path luck, not a discovery — the cap
binds right before the drawdown that sets max DD. This is exactly the kind of result the
sealed-holdout protocol exists to kill, and I expect it to fail out of sample.

The two defensible conclusions from the grid: (a) `f` trades CAGR against DD along a
smooth, roughly Calmar-neutral frontier, and (b) the ratchet / band variants cost a
little CAGR and a little Calmar versus continuous resizing, which is the price of
operational simplicity.

## 5. Phases

| Phase | What | Cost |
|---|---|---|
| **P0** | Closed-form compounding on existing `daily_summary.csv` — **done**, §3 | seconds |
| **P1** | **Homogeneity validation.** Re-simulate ~60 days spanning eras/VIX regimes at `k ∈ {0.25, 0.5, 1, 2, 4, 7}` with config *and* contracts *and* tranche cap scaled. Assert `pnl(k) ≈ k · pnl(1)`. Report worst relative error and attribute residual to integer rounding. | ~5 min |
| **P2** | **Authoritative sequential re-simulation** of `f=1` compounding: real day loop, config rebuilt each day from running equity, no look-ahead. Compare to P0 closed form. | ~26 min |
| **P3** | Variant sweep (`f`, caps, ratchets) — analytic if P1 passes, re-simulated for the top 2 only. | mins / ~1 hr |
| **P4** | **Capacity & realism review.** Contract counts, margin utilization, and liquidity at peak `k`. | ~30 min |

**P1 is the gate.** If `pnl(k)/k·pnl(1)` holds within ~0.5% across the sample, P0/P3
numbers are the answer and P2 is confirmation. If it fails, the closed form is void and
every variant needs a full sequential run.

## 6. Design of the compounding runner

Equity is read at the **open of day t**, i.e. after day `t-1` has fully settled. No
intraday resizing, no look-ahead. Per day:

```python
k = k_of(equity)                                   # variant-specific
cfg = build_p3_poststop_cooldown_config(
    account_equity=PRODUCTION_ACCOUNT_EQUITY * k,
    baseline_contracts=max(1, round(PRODUCTION_BASELINE_CONTRACTS * k)),
)
policy = build_production_vix_policy(
    SCHEMES[PRODUCTION_SIZING_SCHEME],
    elevated_scale=VIX_ELEVATED_SCALE,
    max_contracts=max(1, round(PRODUCTION_MAX_CONTRACTS_PER_TRANCHE * k)),
)
result = simulate_day(quotes, signals, config=cfg, policy=policy)
equity += result.net_pnl
```

`condor_size_fraction` is a ratio (10/31), so the IC overlay scales automatically.

**Sharding must change.** Existing suites shard by *date range*
(`scripts/run_production_ic_ab.py:89-93`), which is only valid because fixed-size days
are independent. A compounding path is sequential, so date-sharding is impossible.
Shard **by variant** instead: each shard runs the full 1514-day path for a subset of
variants. Measured cost is 0.83 s/day I/O + 0.21 s/day simulation, so one variant is
~26 min and 8 parallel variant-shards is ~30-45 min wall (disk-bound).

New files:

- `simulator/compounding_sizing.py` — `k_of` policies (`full`, `fractional(f)`, `capped(n)`, `ratchet(days)`, `band(pct)`) plus the per-day config/policy builder above.
- `simulator/test_compounding_sizing.py` — P1 homogeneity unit test.
- `scripts/analyze_compounding_closed_form.py` — promote the P0/P3 analytic script.
- `scripts/run_compounding_sizing_suite.py` — sequential runner, `--shard/--shards` over **variants**, checkpoint/resume matching house convention.
- `scripts/merge_compounding_sizing_shards.py`, `scripts/summarize_compounding_sizing.py`.
- `scripts/run_compounding_sizing_parallel.ps1`.

Outputs: `data/compounding_sizing/` (`checkpoint.json` per shard, `daily_<variant>.csv`
with an `equity_open` / `k` / `contracts_sold` column, `report.json`, `SUMMARY.md`).

## 7. Anti-overfit protocol

House split (per `why_not_look_at_test_plan.md`):

| Split | Role | Dates |
|---|---|---|
| Rolling feature train | 40 eligible days before each OOS day | walk-forward |
| **Selection** | Rank variants here only | OOS ≤ `2023-12-29` |
| **Holdout (sealed)** | Promotion validation, never retune | OOS ≥ `2024-01-02` |

One adjustment specific to compounding: a compounded holdout inherits whatever equity
the selection period produced, which makes cross-variant holdout metrics
incomparable and lets selection-period luck leak forward. **Re-base equity to 1.0× at
`2024-01-02`** and report holdout CAGR / DD / Calmar on that fresh path.

Promotion gate vs the fixed-size baseline on holdout:

- Calmar ≥ baseline − 0.05
- Max DD ≤ 20% (hard ceiling — this is the real constraint here, not CAGR)
- Worst day not worse by > 0.5pp
- Peak `k` reachable under the P4 capacity review

## 8. Capacity and realism (P4) — the actual risk to this result

The simulator has no market-impact model, so it will happily tell you that 7× size fills
at the same price as 1× size. At peak `k ≈ 7`:

| | 1× | 7× |
|---|---|---|
| Baseline tranche | 31 | ~218 |
| Peak morning tranche (VIX elevated) | 48 | ~336 |
| IC overlay | 10 | ~70 |

SPXW 0DTE is deep, but 336 contracts in a single tranche is a real order. P4 must pull
`approx_spread_margin` from the trade log to confirm margin utilization (which is
scale-invariant by construction, so this should pass) and sanity-check tranche sizes
against observed quote depth. **The credible ceiling on compounding is liquidity, and
the backtest cannot see it.** A cap on `k` is likely to be justified on capacity grounds
even though §4 shows it is not justified on statistical grounds.

Two further real-world gaps to note, not to model: the live pilot is 2 contracts at
$500k (`live/live_config.py`), where integer rounding dominates and `round(2 × k)` is
extremely lumpy; and real accounts have deposits/withdrawals, so the runner should
accept an external equity-base override rather than assuming a closed system.

## 9. Commands

```powershell
# P0 / P3 analytic (seconds)
python scripts/analyze_compounding_closed_form.py

# P1 homogeneity gate
python -m pytest simulator/test_compounding_sizing.py -v

# P2/P3 full sequential runs, sharded by variant
.\scripts\run_compounding_sizing_parallel.ps1 -Shards 8
# or manually:
python scripts/run_compounding_sizing_suite.py --shard 0 --shards 8 --resume --checkpoint-every 10
python scripts/merge_compounding_sizing_shards.py --shards 8
python scripts/summarize_compounding_sizing.py
```

## 10. Expected deliverable

`compounding_sizing_results_2026-07-26.md`: fixed vs compounding headline table,
homogeneity validation residuals, the `f`-frontier with selection/holdout columns,
drawdown attribution (when and where the 15.7% DD occurs on the compounded path), the
capacity table at peak `k`, and a recommended `f` / cap with the reasoning tied to a
stated max-DD tolerance rather than to maximum CAGR.
