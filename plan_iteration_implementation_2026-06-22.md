# Plan Iteration Implementation

Date: 2026-06-22

## Implemented

### Exploratory Bear-Call Guard

Added a configurable guard to the two-tier engine:

- `use_exploratory_bear_call_guard`
- `exploratory_bear_call_guard_end`
- `exploratory_bear_call_min_score`
- `exploratory_bear_call_min_distance_pct`

The default behavior now blocks morning exploratory bear-call candidates when they are low score or too close to spot. This targets the first two completed 10-second classifications, both of which were exploratory bear calls that failed directionally into upward spot movement.

CLI controls added to `simulator/regime_validation.py`:

- `--disable-exploratory-bear-call-guard`
- `--exploratory-bear-call-min-score`
- `--exploratory-bear-call-min-distance-pct`

### Strategy Run Comparison Report

Added `simulator/strategy_run_report.py`.

This creates a repeatable comparison report for validation runs, including:

- test days,
- trades,
- stopped trades,
- stop rate,
- net P&L,
- annualized return,
- average daily credit,
- max approximate margin,
- max margin as percent of equity,
- multiples to MBH-style 1.5% daily credit and 40% margin references.

Generated outputs:

- `data/strategy_run_comparison_q2_q3.csv`
- `strategy_run_comparison_q2_q3.md`

Current comparison:

| Run | Trades | Stops | Stop rate | Net P&L | Annualized return | Avg daily credit | Max margin / equity |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Original event-aware two-tier | 34 | 11 | 32.4% | $5,959.81 | 0.13% | $986.40 | 1.09% |
| Bear-call guard two-tier | 27 | 8 | 29.6% | $11,412.48 | 0.26% | $893.31 | 1.09% |
| Quiet/rich condor retest | 61 | 16 | 26.2% | $20,632.79 | 0.47% | $670.81 | 0.66% |

Sleeve impact from the bear-call guard:

| Sleeve | Original P&L | Guard P&L | Original stops | Guard stops |
| --- | ---: | ---: | ---: | ---: |
| Core | $12,690.44 | $14,197.80 | 3 | 3 |
| Exploratory | -$6,730.63 | -$2,785.32 | 8 | 5 |

The guard improved the result, but it also reduced trades from 34 to 27. The next frequency recovery should come from better structure/expression selection, not from loosening this specific rule.

### Targeted Microstructure Downloader

Added `simulator/microstructure_downloader.py`.

This reads `microstructure_windows.csv` and downloads only the stopped-trade windows rather than full-day tick/10-second data.

Default storage:

- `data/microstructure/thetadata/symbol=SPXW/date=YYYY-MM-DD/`

Completed downloads so far:

- `2025-06-03`, trade 1, CALL 5965
- `2025-06-05`, trade 1, CALL 5995

The next stopped-trade window, `2025-06-30`, exceeded the command runtime limit during repeated ThetaData requests. The downloader is resumable with `--skip` and `--limit`, so remaining windows can continue without re-downloading completed files.

### Stopped-Trade Microstructure Classifier

Added `simulator/stop_microstructure_classifier.py`.

This reads downloaded 10-second windows and classifies each stopped trade as one of:

- `real_directional_failure`
- `quote_spike_or_width_artifact`
- `fast_intraday_whipsaw`
- `late_day_reversal`
- `slippage_sensitive_stop`
- `ordinary_stop_path`
- `missing_microstructure`

Generated output:

- `data/validation_13m_continuous_q2_q3/stop_microstructure_classification.csv`

Current classification status:

| Classification | Count |
| --- | ---: |
| real_directional_failure | 2 |
| missing_microstructure | 9 |

The two completed 10-second windows both confirm real directional failures rather than quote-width artifacts.

## First Microstructure Findings

### 2025-06-03

- Sleeve: exploratory
- Side: bear call
- Short strike: 5965
- Entry: 09:32
- Stop: 11:59
- Entry credit: $2.85
- Stop trigger: $9.50
- Stop fill: $9.80
- 10-second rows: 943
- Max short ask: $10.40
- Max bid/ask width: $0.40
- Ask ticks above stop: 20
- Classification: `real_directional_failure`

Interpretation: this was not just a one-tick quote artifact. The underlying moved into the short-call area and the 10-second path confirmed the stop.

### 2025-06-05

- Sleeve: exploratory
- Side: bear call
- Short strike: 5995
- Entry: 09:47
- Stop: 11:31
- Entry credit: $3.15
- Stop trigger: $11.75
- Stop fill: $12.10
- 10-second rows: 685
- Max short ask: $12.30
- Max bid/ask width: $0.70
- Ask ticks above stop: 12
- Classification: `real_directional_failure`

Interpretation: also not a quote artifact. The exploratory bear-call entries were directionally wrong into a rally.

## Impact On The Improvement Plan

This supports the current plan:

1. Do not scale the exploratory sleeve yet.
2. Tighten exploratory bear-call permission, especially in morning rally conditions.
3. Continue microstructure downloads for the remaining stopped trades before changing stop logic.
4. Prioritize signal-side permission over stop-multiple tuning for these first two failures.

The first two stopped trades suggest the problem was entry selection, not overly tight stops.

The first guard implementation confirmed this direction: fewer stopped trades, higher P&L, and smaller exploratory-sleeve losses.

## Commands For Continuing

Continue remaining 10-second downloads one row at a time:

```powershell
$env:THETADATA_API_KEY="..."
python simulator\microstructure_downloader.py --windows data\validation_13m_continuous_q2_q3\microstructure_windows.csv --interval 10s --strike-range 30 --skip 2 --limit 1
```

Then rerun classification:

```powershell
python simulator\stop_microstructure_classifier.py --windows data\validation_13m_continuous_q2_q3\microstructure_windows.csv --output data\validation_13m_continuous_q2_q3\stop_microstructure_classification.csv
```

Regenerate the comparison report:

```powershell
python simulator\strategy_run_report.py --results-dirs data\validation_13m_continuous_q2_q3 data\validation_13m_continuous_q2_q3_quiet_condor --output-csv data\strategy_run_comparison_q2_q3.csv --output-md strategy_run_comparison_q2_q3.md
```

## Next Code Change

The exploratory bear-call guard is now implemented. The next implementation step should be:

1. Continue downloading the remaining stopped-trade microstructure windows.
2. Classify the remaining six stopped trades from the guarded run.
3. Add expression-level alternatives for frequency recovery:
   - 1DTE non-directional sleeve,
   - refined condor only if standalone attribution turns positive,
   - long-vol overlay tied to classified failure modes.
4. Keep core bear-call rules unchanged until more stopped-trade windows are classified.
