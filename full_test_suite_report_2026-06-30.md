# Full Test Suite Report — 2026-06-30

Comprehensive results from Tests 1, 2, and 3 (stop calibration 3A–3F), plus MBH snapshot analysis.
All backtests: **391 OOS days** (40-day warmup), $13M equity, 31 contracts/tranche, SPXW 0DTE.

## Executive summary

| Test | Status | Best variant | CAGR | Worst day | Win% | Key finding |
|---|---|---|---:|---:|---:|---|
| **Test 1** — Unconditional baseline | PASS | gates off, 2.0× stop | 8.9% | -3.7% | 61.5% | Base trade is +EV when deployed daily |
| **Test 2** — Stop + structure matrix | PASS | wide_wings_no_stop | 27.8% | -25.3% | 83.3% | Wide wings fix payoff; stops cap tail but cut CAGR |
| **Test 3A–3D** — Stop calibration | PASS | 3D_flatten_3.5 | **26.3%** | **-4.6%** | 73.2% | Best risk/return with stops enabled |
| **Test 3F** — Selective entry | **FAIL** | all variants | 0% | 0% | — | Score gates block 100% of entries |
| **MBH snapshots** | PASS | — | — | — | — | Wide wings, net-long book, loose stops |
| **Unit tests** | N/A | — | — | — | — | No pytest suite in repo |
| **Smoke tests** (5-day) | PASS | all scripts exit 0 | — | — | — | Scripts execute without errors |

**Closest to MBH target (30–40% CAGR, ~65% win, ~4–5% worst day):** `3D_flatten_3.5` at 26.3% / 73.2% / -4.6%.

---

## Test 1: Unconditional Baseline

**Script:** `simulator/unconditional_baseline.py`  
**Config:** ~20Δ verticals every 15 min, all gates off, 2.0× short-leg stop, default ~25pt wings.

| Metric | Result | MBH target |
|---|---:|---:|
| Active days | 391 (100%) | 99.2% |
| Trades | 9,256 | — |
| Win rate | **61.5%** | ~65% |
| Stop rate | 39.6% | — |
| Expectancy/trade | **+$198** | > $0 |
| CAGR | 8.88% | 30–40% |
| Sharpe | 0.60 | ~2.5 |
| Worst day | -3.7% | ~4–5% |

**Exit reason breakdown (the whole story):**

| Exit | Trades | Win% | E[trade] |
|---|---:|---:|---:|
| settled_at_close | 5,586 | 99.5% | +$6,033 |
| short_stopped_long_settled | 3,670 | 3.7% | **-$8,683** |

**Conclusion:** Deployment is not the problem — gating was. The 2.0× stop on tight wings destroys expectancy on ~40% of trades.

---

## Test 2: Stop + Structure Matrix

**Script:** `simulator/stop_structure_matrix.py`  
**Config:** Unconditional cadence, 9 structural variants.

| Variant | CAGR | Sharpe | Worst day | Win% | Stop% | E[trade] |
|---|---:|---:|---:|---:|---:|---:|
| **wide_wings_no_stop** | **27.8%** | 0.85 | -25.3% | 83.3% | 0% | $720 |
| mbh_like (wide + overlay + no stop) | 20.8% | 0.69 | -25.3% | 83.3% | 0% | $720 |
| stop_3.0 (tight wings) | 13.3% | 0.84 | -6.3% | 74.3% | 25.3% | $300 |
| stop_2.5 | 11.6% | 0.73 | -5.0% | 69.4% | 31.1% | $261 |
| wide_wings + stop 2.0 | 11.1% | 0.79 | -6.6% | 58.3% | 41.4% | $276 |
| stop_2.0 baseline (Test 1) | 8.9% | 0.60 | **-3.7%** | 61.5% | 39.6% | $198 |
| no_stop (tight wings) | 10.7% | 0.49 | -26.5% | 85.3% | 0% | $240 |
| net_long + no stop | -0.8% | 0.16 | -26.7% | 85.3% | 0% | $240 |
| net_long + stop 2.0 | -2.7% | -0.06 | -4.5% | 61.5% | 39.6% | $198 |

**Conclusion:** Wide asymmetric wings (200/75) are necessary. Net-long overlay hurts. Stops are required for tail control but need calibration — raw 2.0× on wide wings still only 11.1% CAGR.

---

## Test 3: Stop Calibration (3A–3F)

**Script:** `simulator/stop_calibration_runner.py`  
**Substrate:** Wide wings (200/75), stops required, gates off except 3F.

### Phase winners (sequential refinement)

| Phase | Winner | CAGR | Sharpe | Worst day | Win% | Stop% |
|---|---|---:|---:|---:|---:|---:|
| 3A (stop multiple) | 3A_stop_3.0x | 15.8% | 0.80 | -10.6% | 72.1% | 26.2% |
| 3B (trigger/fill) | 3B_confirm_2bar | 18.2% | 0.88 | -9.6% | 73.8% | 23.8% |
| 3C (post-stop) | 3C_baseline | 18.2% | 0.88 | -9.6% | 73.8% | 23.8% |
| 3D (governor) | **3D_flatten_3.5** | **26.3%** | **1.32** | **-4.6%** | 73.2% | 22.2% |
| 3F (selective entry) | **ALL FAILED** | 0% | — | 0% | — | — |

### Notable 3B trade-offs

| Variant | CAGR | Worst day | Notes |
|---|---:|---:|---|
| 3B_spread_1.5x | 14.8% | **-5.7%** | Best tail in 3B (spread-value stop) |
| 3B_spread_2.0x | 17.6% | -8.0% | Higher return, worse tail |
| 3B_confirm_2bar | 18.2% | -9.6% | Best CAGR in 3B; carried forward |

### Production candidate config (`3D_flatten_3.5`)

```
Wide wings: put 200pt / call 75pt
Stop: 3.0× short-leg ask, 2-bar confirmation
Governor: flatten at -3.5% daily loss, halt entries at -2.25%
Credit cap: 1.5% of equity/day
Result: 26.3% CAGR, Sharpe 1.32, -4.6% worst day, 73.2% win, $698/trade
```

### Test 3F failure diagnosis

All four 3F variants produced **zero trades** over 391 days:

| Variant | Trades | Root cause |
|---|---:|---|
| 3F_gated_2.50 | 0 | Score ≥2.50 + premium richness gate |
| 3F_ablate_cheap_2.40 | 0 | Score ≥2.40 still blocks all candidates |
| 3F_harvest_2.50 | 0 | Harvest min score 2.25 too high for unconditional substrate |
| 3F_event_time_2.50 | 0 | Same score gates + event/time controls |

Verified on individual days: `candidate_min_score=1.0` produces ~24 trades/day; `≥2.40` produces 0. The unconditional signal features do not produce scores high enough for MBH-style selective entry without green-day refit weights.

**Next step for 3F:** Re-run with `signals_regime_validation.csv` + `data/models/mbh_green_day_weights.json`.

### Test 3E (microstructure)

**Skipped** — no `data/microstructure/` data downloaded.

---

## MBH Snapshot Analysis

**Script:** `simulator/analyze_mbh_snapshots.py` — runs cleanly.

Key findings from 3 position snapshots:

1. **Wide asymmetric wings:** put ~176–289pt, call ~49–122pt (vs our ~25pt default)
2. **Net-long book:** 1.5–1.8× long/short via retained wings after stops
3. **Loose effective stops:** 15:00 snapshot shows shorts at 0.15% OTM still open
4. **Heavy laddering:** 13 legs at 11:00 → 49 legs at 15:00 on DDQ day

---

## MBH target gap

| Metric | MBH target | Best with stops (3D_flatten_3.5) | Best no-stop (wide wings) |
|---|---:|---:|---:|
| CAGR | 30–40% | 26.3% | 27.8% |
| Win rate | ~65% | 73.2% | 83.3% |
| Worst day | ~4–5% | -4.6% | -25.3% |
| Sharpe | ~2.5 | 1.32 | 0.85 |

**Remaining gap:** ~4–14pp CAGR to reach 30–40%, primarily from (a) better entry selection once 3F is fixed, (b) possible stop loosening toward 3.5×, (c) laddering/book-building not yet modeled.

---

## Artifacts

| Output | Path |
|---|---|
| Test 1 results | `data/unconditional_baseline/`, `unconditional_baseline_results_2026-06-30.md` |
| Test 2 results | `data/stop_structure_matrix/`, `stop_structure_matrix_results_2026-06-30.md` |
| Test 3 summary | `data/stop_calibration/calibration_summary.csv`, `stop_calibration_results_2026-06-30.md` |
| MBH snapshots | `mbh_snapshot_reverse_engineering_2026-06-30.md` |
| Test plan | `stop_calibration_test_plan_2026-06-30.md` |

---

## Smoke test log (2026-06-30)

All primary scripts executed successfully on 5 OOS days:

- `unconditional_baseline.py --max-days 5` — exit 0
- `stop_structure_matrix.py --max-days 5` — exit 0 (9/9 variants)
- `stop_calibration_runner.py --max-days 5` — exit 0 (22/22 variants; 3F confirmed 0 trades)
- `analyze_mbh_snapshots.py` — exit 0

Note: Full 391-day CSV artifacts for Tests 1–3 were re-running at report time after accidental 5-day overwrite during smoke testing. Summary metrics above are from the completed full runs.
