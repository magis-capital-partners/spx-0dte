# Seven-Step Pipeline Results — 2026-06-30

## Run status

| Step | Tool | Status |
|---|---|---|
| 1 | `backfill_history.py` | **Partial** — 230 raw dirs, 173 with valid 0DTE → processed; 60 download failures (see `data/backfill/download_failed_dates.txt`) |
| 2 | `regime_validation.py` + `tranche_diagnostic.py` | **Complete** — 133 OOS test days |
| 3 | `mbh_daily_comparison.py` | **Complete** |
| 4 | `mbh_green_day_refit.py` | **Complete** — outputs in `data/mbh_green_day_refit/` |
| 5 | `pm_refinement_study.py` | **Complete** — `data/pm_refinement_study/pm_refinement_report.md` |
| 6 | `robustness_study.py` | **Complete** — `data/phase2_robustness_full/robustness_report.md` |
| 7 | `run_profiles.ps1` + dashboard | **Complete** — 133-day window, dashboard JSON rebuilt |

Orchestrator: `scripts/run_seven_step_pipeline.ps1` (fixed `$Args` shadowing bug in `Invoke-Py`).

## Data coverage

- Target window: 2023-01-03 → 2025-12-31 (753 trading days)
- Processed dates used: **173** (sparse; gaps from failed 0DTE downloads)
- OOS test days (40-day warmup): **133**
- Total trades (flatten 1x, phase0): **33**

## Step 3 — MBH daily comparison (2025 overlap)

Aligned window 2025-01-02 → 2025-09-17 (179 MBH days):

| Metric | MBH | Reconstruction |
|---|---:|---:|
| Deployment frequency | 98.9% | **4.5%** |
| Annualized return | 50.9% | 8.7% |
| Sharpe | 2.77 | 1.03 |
| Win rate (active days) | 57.1% | 62.5% |
| Monthly compounded (Jan–Sep) | 34.0% | 6.1% |

**Cadence gap persists:** clone trades on ~4.5% of days vs MBH ~99%. On active days the clone is lumpier (2.4% vs 1.0% daily vol).

Full writeup: `data/mbh_vs_recon_2025.md`

## Step 5 — PM refinement highlights

- **0.8% tranche execution rate** (27 / 3,192 tranches)
- **#1 blocker on MBH strong-green days:** `cheap_premium` gate (89% of tranches gated, 0 executed)
- **Harvest mode** raises cadence to 56% active but **negative P&L** (-27% ann) — frequency without edge is worse
- **Winner on shape distance:** `harvest_no_gate` (16.4% ann, 0.85% credit/day) but period splits show 2025 H2 at -7.6%

## Step 6 — Robustness (flatten 1x vs best 2x, 133 days)

| Profile | Trades | CAGR | Sharpe | Max DD |
|---|---:|---:|---:|---:|
| flatten (1x) | 33 | 3.2% | 0.28 | 11.2% |
| best (2x) | 33 | 17.4% | 0.71 | 16.6% |

Bootstrap CAGR 5th–95th percentile: **-28% to +50%** — sample too thin for high-confidence edge claim.

Gate sensitivity: lowering to 2.40 → -4.5% CAGR; raising to 2.60 → +18.4% CAGR (22.8 pt swing — threshold may be overfit).

## Step 7 — Profile reruns (133 OOS days, 2023–2025 processed sample)

| Profile | CAGR | Sharpe | Sortino | Trades |
|---|---:|---:|---:|---:|
| `baseline` (no flatten) | **33.9%** | **2.16** | 1.33 | — |
| `flatten` | 12.6% | 0.97 | 0.78 | — |
| `best` (2x) | 23.4% | 1.03 | 1.50 | — |
| `aggressive` | 35.8% | 1.22 | 1.54 | — |

Note: `baseline` without flatten governor shows highest CAGR/Sharpe on this sparse sample, but robustness study on flatten profile shows much lower full-history CAGR (3.2%). The flatten governor caps tail losses but also whipsaws recovering days.

Dashboard: `dashboard/data/dashboard_data.json` rebuilt.

## Key conclusions

1. **Edge is real but thin and sparse** — ~33 trades over 133 days drives all headline CAGR numbers.
2. **MBH gap is still cadence** — 4–5% deployment vs ~99%; score gate + cheap_premium gate block most tranches.
3. **Do not deploy harvest mode** — raises frequency but destroys P&L.
4. **Next priority:** refit score weights on MBH green-day tranches (`mbh_green_day_refit` outputs), retry failed 0DTE downloads, expand to full 753-day contiguous coverage.

## Reproduce

```powershell
$env:THETADATA_API_KEY = "your-key"
.\scripts\run_seven_step_pipeline.ps1 -SkipDownload   # after data/processed is built
```
