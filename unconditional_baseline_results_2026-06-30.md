# Unconditional Baseline — Test 1

Generated: 2026-06-30 21:34

## Configuration

- Account equity: $13,000,000
- Baseline contracts: 31
- Stop multiple: 2.0x (short leg)
- Target short delta: 0.2 (0.15–0.25)
- All signal/score gates: **disabled**
- Risk cooldowns / concentration limits: **disabled**
- OOS days (40-day warmup): 391

## Portfolio (compounded)

| Metric | Value |
|---|---:|
| OOS days | 391 |
| Active days | 391 (100.0%) |
| Total trades | 9256 |
| Tranches / executed | 9384 / 9256 (98.6% fill) |
| Trades per active day | 23.67 |
| Net P&L | $1,834,401 |
| CAGR | 8.88% |
| Sharpe | 0.60 |
| Max drawdown | 15.97% |
| Worst day | $-483,578 |
| Day win rate | 45.3% |

## Per-Trade Stats (overall)

| Metric | Value | MBH target |
|---|---:|---:|
| Trades | 9256 | — |
| **Win rate** | **61.5%** | ~65% |
| Stop rate | 39.6% | — |
| **Expectancy / trade** | **$198** | > $0 |
| Median P&L | $3,981 | — |
| Avg win | $7,231 | — |
| Avg loss | $-11,044 | — |
| Win/loss ratio | 0.65x | — |

## By Side

| Side | Trades | Win rate | Expectancy | Stop rate |
|---|---:|---:|---:|---:|
| bear_call | 5349 | 60.3% | $-160 | 40.2% |
| bull_put | 3907 | 63.2% | $689 | 38.8% |

## By Exit Reason

| Exit reason | Trades | Win rate | Expectancy |
|---|---:|---:|---:|
| settled_at_close | 5586 | 99.5% | $6,033 |
| short_stopped_long_settled | 3670 | 3.7% | $-8,683 |

## Why Tranches Did Not Execute (top gate/reject reasons)

| Reason | Count |
|---|---:|
| rejected:insufficient_credit | 10 |

## No-Trade Tranche Skip Reasons

| Skip reason | Tranches |
|---|---:|
| no_trade | 128 |

## Interpretation Guide

- Win rate ≈ 65% but negative expectancy → **stop/exit problem** (losses too large per stop)
- Win rate < 55% → **structure/strike problem** (delta selection or side choice)
- Win rate ≈ 65% and positive expectancy → **deployment/sizing problem** (scale up)
