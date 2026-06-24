# Recommended Steps Implementation Run

Date recorded: 2026-06-22

## Implemented

### Continuous Q2/Q3 Tooling

Added `simulator/continuous_research.py`.

Purpose:

- Generate expected market dates for a continuous research window.
- Download ThetaData chains when `THETADATA_API_KEY` is available.
- Build processed features from raw files.
- Run the current $13M event-aware two-tier validation on the continuous window.

Default target window:

- 2025-04-01 through 2025-09-30

Current local status:

- Expected market dates: 126
- Already processed dates in Q2/Q3: 26
- Download could not be run because `THETADATA_API_KEY` was not set in this shell.

### Condor Quiet/Rich Retest

Added `--condor-allowed-event-buckets` to `simulator/regime_validation.py`.

This lets the condor sleeve be tested only on specific event buckets, such as:

- `quiet_grind`
- `unlabeled`

Run tested:

- `data/validation_13m_quiet_rich_condor`
- Event-aware two-tier engine
- Condor sleeve enabled only for quiet/unlabeled buckets
- Lower-delta, stricter rich-premium condor configuration

### Stopped-Trade Microstructure Windows

Added `simulator/stop_microstructure_windows.py`.

Purpose:

- Read `stop_diagnostics.csv`
- Create targeted 10-second/tick download windows only around stopped trades
- Avoid downloading high-frequency data for every day

Generated:

- `data/validation_13m_event_two_tier_current/microstructure_windows.csv`
- `data/validation_13m_quiet_rich_condor/microstructure_windows.csv`

## Validation Results

Sample: current 60-day validation set from the available 100 processed dates.

| Run | Trades | Stops | Stop Rate | Net P&L | Return On $13M | Max Day Credit | Max Approx Margin |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Current $13M event-aware two-tier | 20 | 5 | 25.0% | $71,269.56 | 0.5482% | $17,050 | $137,950 |
| Quiet/rich condor retest | 39 | 12 | 30.8% | $47,584.29 | 0.3660% | $17,050 | $137,950 |

Sleeve attribution:

| Run | Core P&L | Exploratory P&L | Condor P&L |
| --- | ---: | ---: | ---: |
| Current $13M event-aware two-tier | $68,544.03 | $2,725.53 | $0.00 |
| Quiet/rich condor retest | $48,909.63 | $2,725.53 | -$4,050.87 |

## Readout

The current $13M event-aware two-tier engine remains the best working baseline.

The quiet/rich condor retest did not improve the strategy:

- Trade count rose from 20 to 39.
- Stop rate rose from 25.0% to 30.8%.
- Net P&L fell from $71,269.56 to $47,584.29.
- Condor standalone P&L was -$4,050.87.

The condor sleeve should remain disabled until continuous data improves normal-day baselines and gives us enough quiet/rich observations to calibrate against.

## Current Blocker

Continuous Q2/Q3 2025 download was not completed because `THETADATA_API_KEY` is missing from the current shell.

When the key is available, run:

```powershell
python simulator\continuous_research.py --download --build --validate --start-date 2025-04-01 --end-date 2025-09-30
```

## Current Recommendation

Use:

- `data/validation_13m_event_two_tier_current`

Keep disabled:

- Condor sleeve

Next executable step after setting `THETADATA_API_KEY`:

1. Download/build continuous Q2/Q3 2025.
2. Rerun current $13M event-aware two-tier validation on continuous data.
3. Use `microstructure_windows.csv` to download 10-second/tick data only for stopped trades.
4. Re-evaluate stop model before adding margin utilization.
