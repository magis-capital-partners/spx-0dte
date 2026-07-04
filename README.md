# MBH SPX 0DTE Strategy Recreation

This folder turns the diligence plan into a working reconstruction project.

The current version focuses on the narrower strategy described in the call and Fund 1 materials:

- SPX/SPXW only.
- Same-day expiration.
- Risk-defined vertical spreads.
- Empty book at the start of the regular session.
- 15-minute tranche cadence.
- 15-25 delta short legs.
- Protective long wings.
- Stop the short leg only.
- Keep the long wing until end-of-day settlement.
- No overnight option positions.
- Position size throttled from 16 contracts to 8, 4, or 0 based on option-chain signals.

The simulator does not claim to know MBH's proprietary model. It exposes the signal fields MBH described so we can test candidate reconstructions against real SPX option-chain data.

## Files

- `strategy_reconstruction_spec.md` - formal version of the recreated strategy rules.
- `diligence_reconciliation.md` - what the MBH materials agree on, where they conflict, and what must be resolved.
- `data_schema.md` - data fields needed to run a credible backtest.
- `position_snapshot_analysis.md` - DDQ and position-snapshot analysis used to improve the reconstruction.
- `position_snapshots.csv` - structured version of the user snapshot and DDQ snapshots.
- `simulator\mbh_simulator.py` - first-pass SPX 0DTE vertical-spread simulator.
- `simulator\snapshot_tools.py` - helper for summarizing strike-level position snapshots.
- `simulator\thetadata_downloader.py` - downloads SPXW 0DTE and next-expiration first-order Greeks from ThetaData.
- `simulator\feature_builder.py` - converts ThetaData files into simulator quotes and signal features.
- `simulator\run_reconstruction_backtest.py` - runs the reconstruction simulator on processed data.
- `simulator\calibration_report.py` - summarizes current backtest results and trade shape.
- `simulator\holdings_from_trades.py` - reconstructs strike-level simulated holdings at screenshot times.
- `simulator\calibration_grid.py` - scores parameter grids against the March 2 DDQ snapshot.
- `simulator\historical_baselines.py` - builds no-lookahead historical signal baselines.
- `simulator\walk_forward_grid.py` - runs walk-forward parameter grids.
- `simulator\long_vol_overlay.py` - models long-volatility hedge overlays.
- `simulator\combine_holdings.py` - combines spread and long-vol holdings.
- `simulator\snapshot_scorer.py` - scores simulated holdings against MBH/DDQ snapshots.
- `implementation_status_2026-06-20.md` - current implementation status and calibration findings.
- `run_full_research.ps1` - orchestrates a larger ThetaData download, feature build, walk-forward grid, and long-vol overlay run.
- `simulator\sample_quotes.csv` - small synthetic quote sample.
- `simulator\sample_signals.csv` - synthetic signal sample.
- `simulator\run_sample.py` - sample run using the synthetic files.

## ThetaData Usage

**Local cache:** SPXW history is stored under `data/raw/` and `data/processed/`.
See **`data/README.md`** and **`data/inventory/manifest.json`**. Backtests re-use this
cache — do not re-download unless filling explicit gaps.

The downloader reads the API key from `THETADATA_API_KEY`. Do not hard-code the key into scripts.

Example:

```powershell
$env:THETADATA_API_KEY="..."
python .\simulator\thetadata_downloader.py --symbol SPXW --dates 2026-03-02 --interval 1m --strike-range 80
python .\simulator\feature_builder.py --symbol SPXW --dates 2026-03-02
python .\simulator\run_reconstruction_backtest.py --symbol SPXW --dates 2026-03-02 --account-equity 28000000
python .\simulator\calibration_report.py
```

For a larger research run:

```powershell
$env:THETADATA_API_KEY="..."
.\run_full_research.ps1 -StartDate "2025-01-02" -EndDate "2025-03-31" -TrainCount 40 -TestCount 20
```

## Current Status

ThetaData is wired in and the first real-data pilot is running through the full path: raw 0DTE/next-expiration chains, processed signal features, simulator trades, daily results, and calibration report.

The remaining work is not plumbing. It is model research: larger data download, parameter search, walk-forward validation, intraday snapshot matching, long-volatility hedge attribution, and execution/slippage calibration.
