# Seven-Step Pipeline Results — 2026-06-29

## Status

| Step | Tool | Status |
|---|---|---|
| 1 | `backfill_history.py` (2023-01-03 → 2025-12-31) | **Blocked** — no local processed data; `THETADATA_API_KEY` not set in shell |
| 2 | `tranche_diagnostic.py` | **Blocked** — needs Step 1 validation output |
| 3 | `mbh_daily_comparison.py` | **Partial** — ran on bundled `_tmp_robustness_summary` daily CSV (prior machine run, flatten-scale) |
| 4 | `mbh_green_day_refit.py` | **Blocked** — needs Step 1 tranche export |
| 5 | `pm_refinement_study.py` | **Blocked** — needs Step 1 processed data + tranches |
| 6 | `robustness_study.py` | **Blocked** — needs Step 1 processed data |
| 7 | `run_profiles.ps1` + dashboard | **Blocked** — needs Step 1 processed data |

**Backfill inventory:** 753 trading days missing raw + processed data for 2023–2025.

Orchestration script added: `scripts/run_seven_step_pipeline.ps1`

```powershell
$env:THETADATA_API_KEY = "your-key"
.\scripts\run_seven_step_pipeline.ps1
```

## Step 3 — MBH daily comparison (interim, bundled CSV)

Source: `_tmp_robustness_summary/daily_regime_validation.csv` (flatten-scale run, sparse processed dates).

Aligned window 2025-01-02 → 2025-12-31 (252 MBH trading days):

| Metric | MBH | Reconstruction (interim) |
|---|---:|---:|
| Deployment frequency | 99.2% | **13.5%** |
| Annualized return | 40.9% | 3.6% |
| Sharpe | 2.57 | 0.42 |
| Win rate (active days) | 58.0% | 55.9% |
| Daily vol (active days) | 0.87% | 1.61% |

**Cadence gap:** MBH is active ~every day; the clone trades on only ~14% of days (~7× less frequent). On days it fires, per-day swing is lumpier (1.6× MBH active-day vol) because the book sits flat most days.

Monthly compounded gap: MBH **+40.9%** vs recon **+3.6%** for 2025. Largest negative gaps: Apr (-13.3%), Jan (-9.7%), Mar (-8.0%).

Full writeup (local, gitignored): `data/mbh_vs_recon_2025.md`

## Next action

Set `THETADATA_API_KEY` and rerun the full pipeline. Step 1 alone downloads ~753 trading days of SPXW 1-minute chains — expect several hours depending on ThetaData rate limits.

After a full run, refresh this document with Steps 2, 4–7 metrics from:

- `data/signal_diagnostics_full/tranche_diagnostic_report.md`
- `data/mbh_green_day_refit/`
- `data/pm_refinement_study/pm_refinement_report.md`
- `data/phase2_robustness_full/robustness_report.md`
- `data/profile_best/` summaries via `summarize_run.py`
