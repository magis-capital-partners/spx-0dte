# Seven-Step Pipeline Results — 2026-06-30 (expanded rerun)

## Data coverage

| Metric | Prior (6/29) | After retry (6/30) |
|---|---:|---:|
| Raw date dirs | 392 | 474+ |
| Valid 0DTE | 288 | **370** |
| Processed + enriched | 288 | **431** |
| OOS test days (40-day warmup) | 133 | **391** |
| Still missing 0DTE | 104 | **22** (after 82/104 retry recovery) |

Helpers added: `simulator/retry_missing_0dte.py`, `simulator/build_missing_processed.py`

## Profile reruns (391 OOS days, 2023–2025)

| Profile | CAGR | Sharpe | Sortino |
|---|---:|---:|---:|
| `baseline` | 5.6% | 0.42 | — |
| `flatten` | 5.4% | 0.49 | — |
| `best` (2x) | **10.5%** | **0.59** | — |
| `aggressive` | 12.2% | 0.55 | — |

Headline CAGRs dropped vs the 133-day sample (33% baseline) — the expanded window is a more honest read.

## MBH comparison (2025 full year)

| Metric | MBH | Recon |
|---|---:|---:|
| Deployment frequency | 99.2% | **6.8%** |
| Annualized return | 41.2% | 5.5% |
| Sharpe | 2.57 | 0.71 |
| Compounded Jan–Dec | 40.8% | 5.4% |

Cadence gap narrowed slightly (4.5% → 6.8%) but remains ~15× below MBH.

## Green-day score refit (overlap window, expanded tranches)

| Model | Trades | Active% | CAGR | Shape dist |
|---|---:|---:|---:|---:|
| Hand-tuned | 12 | 12.9% | -8.3% | 1.143 |
| **Green refit** | 11 | 7.1% | **+13.9%** | **1.084** |
| Strong-green refit | 59 | 52.9% | -73.0% | 1.274 |

**Green refit** improves P&L and shape distance on the overlap window but still trades far too rarely. **Strong-green refit** opens cadence (53% active) but destroys P&L — do not deploy.

Weights: `data/models/mbh_green_day_weights.json` (green), `data/models/mbh_green_day_weights_strong.json` (strong_green).

## Next steps

1. Retry final **22** stubborn 0DTE dates (ThetaData session errors)
2. Download remaining raw gaps in 2023–2025 target range (~280 calendar days still missing raw)
3. Ablation study: remove `cheap_premium` gate before next refit
4. Do not promote refit weights to live until cadence exceeds ~30% active days with positive full-sample CAGR

## Reproduce

```powershell
$env:THETADATA_API_KEY = "your-key"
python simulator/retry_missing_0dte.py
python simulator/build_missing_processed.py
.\scripts\run_seven_step_pipeline.ps1 -SkipDownload
```
