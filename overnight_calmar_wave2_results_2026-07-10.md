# Overnight Calmar Wave 2 Results (2026-07-10)

Eligible-calendar OOS on production substrate `p3_poststop_cooldown_120`
(skew 0.65, flatten −3.25%, 120min same-side cooldown, VIX skip >35 / elev 1.25×,
`linear_decay_downsize`). **68 variants** (1 ref + 55 singles p7–p12 + 12 Phase 13 combos).

**Scoring:** constrained Calmar with hard reject worst day < −7.5%, max DD > 11%, CAGR < 16%.

## Promotion decision

**Promoted: `put_wing_150`** → baked into `build_p3_poststop_cooldown_config()` as
`PRODUCTION_PUT_WING_WIDTH = 150` (was 200 via `wide_wings()`).

| Metric | Baseline | put_wing_150 | Delta |
|--------|---------:|-------------:|------:|
| CAGR | 17.71% | **19.92%** | +2.21pp |
| Calmar | 2.10 | **2.31** | +0.21 |
| Max DD | 8.42% | 8.62% | +0.20pp |
| Worst day | −6.82% | −6.82% | 0 |
| Sharpe | 1.62 | **1.85** | +0.23 |
| Constrained score | 2.265 | **2.496** | +0.23 |
| Trades | 21,010 | 22,064 | +5% |
| Stop rate | 21.7% | 21.2% | −0.5pp |

Hard floors pass. Max DD is +0.2pp vs baseline but still well under the 9.5% soft
and 11% hard caps; CAGR/Calmar/Sharpe gains dominate.

**Runner-up (not promoted):** `put_wing_175` — Calmar 2.31, DD **8.14%** (−0.28pp),
CAGR 18.82%, worst −6.83%. Prefer if prioritizing DD over CAGR.

**Best risk-shape single:** `trend_bc_085` — DD 8.10%, worst **−6.08%**, CAGR ~flat
at 17.69%. Strong secondary lever for a future combo with put_wing_150.

## Top 10 (constrained score, passing)

| Rank | Phase | Variant | CAGR | Calmar | DD | Worst | Score |
|-----:|-------|---------|-----:|-------:|---:|------:|------:|
| 1 | p9_struct | **put_wing_150** | 19.9% | 2.31 | 8.62% | −6.82% | 2.496 |
| 2 | p9_struct | put_wing_175 | 18.8% | 2.31 | 8.14% | −6.83% | 2.455 |
| 3 | p8_bc | trend_bc_085 | 17.7% | 2.18 | 8.10% | −6.08% | 2.352 |
| 4 | p11_dyn | vix_flatten_tight | 17.6% | 2.11 | 8.32% | −6.82% | 2.274 |
| 5 | p12_qual | min_score_10 | 17.7% | 2.11 | 8.41% | −6.82% | 2.270 |
| 6 | p11_dyn | intraday_cut_20 | 17.7% | 2.11 | 8.42% | −6.82% | 2.269 |
| 7 | p12_qual | sl_to_credit_40 | 17.7% | 2.10 | 8.42% | −6.82% | 2.267 |
| 8 | ref | baseline_vix125 | 17.7% | 2.10 | 8.42% | −6.82% | 2.265 |
| 9 | p12_qual | realized_z_100 | 17.7% | 2.10 | 8.42% | −6.82% | 2.265 |
| 10 | p12_qual | realized_z_125 | 17.7% | 2.10 | 8.42% | −6.82% | 2.265 |

## Phase 0 diagnostics (pruned hypotheses)

From `analyze_weak_periods*` / VIX attribution on prior dashboard runs:

- **Kept:** no-9am, Tuesday skip, halt-after-N-stops, tighter bear-call trend, put-wing structure
- **Pruned:** gap-Q4 skip (Q4 was *best* PnL bucket, not worst)

## Phase findings

### P7 Circuit breakers
Modest. `max_stops_3/4` and `late_reentry_1400` ≈ baseline. Day-wide halt-after-stop
did not beat baseline on constrained score alone (helps worst day mainly in combos).

### P8 Bear-call / trend
**`trend_bc_085`** is the clear winner: same CAGR, better DD (−0.32pp) and worst day
(+0.74pp). Tighter `trend_bc_075` / `050` and BC size scales cut CAGR below floor.
Chase-trend and hard-trend skips mostly rejected.

### P9 Structure
**Dominant phase.** Narrower put wing (150/175) lifts CAGR and Calmar. `delta_22`
rejected on worst day (−8.02%). `delta_15/18` and `call_wing_50` fail CAGR floor.
`call_wing_100` passes but trails put-wing winners.

### P10 Calendar / session
Aggressive filters hurt. `entry_1000/1030`, `no_9am_tod`, `interval_30`,
`skip_mon_tue` rejected (CAGR or DD). Only **`skip_tue`** passes (CAGR 16.8%,
score below baseline).

### P11 Dynamic risk
`vix_flatten_tight` and `intraday_cut_*` slight improvements. Regime-threshold /
scale and prior-day-loss variants mostly rejected (DD blowups or CAGR floor).

### P12 Quality gates
Near-null vs baseline (`min_score_10`, SL/credit, realized-z). `min_score_20` and
`tod_score_on` fail CAGR floor (echoes Test 3F). `two_sided` not in top ranks.

### P13 Combos
Hypothesis combos underperformed top singles. Best combo:
`combo_trend075_maxstops3` (score 2.230) — improves worst day but loses CAGR.
**None beat `put_wing_150`.** Future work: `put_wing_150` + `trend_bc_085`.

## Rejected (30 / 68)

Common failure modes: CAGR floor (calendar cuts, BC downsize, score gates),
max DD (regime downsize, delayed entry), worst day (`delta_22`).

## Artifacts

| Path | Role |
|------|------|
| `data/overnight_calmar_suite/summary.json` | Full metrics |
| `data/overnight_calmar_suite/report.json` | Ranked report |
| `data/overnight_calmar_suite/wave1_archive/` | Prior Wave 1 summary |
| `simulator/profiles.py` | Promoted put wing 150 |
| `scripts/overnight_calmar_variants.py` | Wave 2 registry |

## Next steps

1. Re-export dashboard: `python simulator/export_dashboard_run.py --preset p3_poststop_cooldown_120`
2. Optional Wave 2b: combo `put_wing_150` + `trend_bc_085` (+ optional `vix_flatten_tight`)
3. Live already uses `p3_poststop_cooldown_120` — picks up put wing 150 automatically
