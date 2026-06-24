# Continuous Q2/Q3 2025 Research Results

Date: 2026-06-22

## Data Stored

ThetaData Q2/Q3 2025 SPXW data has been downloaded and processed for the full market-date window from 2025-04-01 through 2025-09-30.

- Raw option data: `data/raw/thetadata/symbol=SPXW/date=YYYY-MM-DD/`
- Processed feature data: `data/processed/symbol=SPXW/date=YYYY-MM-DD/`
- Processed dates available in window: 126 of 126

The API key was used for the download session only and was not written into the repository.

## Validation Setup

Both validations used the scaled $13,000,000 account configuration.

- Baseline size: 31 contracts, scaled from the prior 66-contract / $28M framework.
- Training baseline: first 40 market dates.
- Test period: remaining 86 market dates.
- Conservative core: current 2.50 event-aware two-tier engine.
- Exploratory sleeve: lower-confidence entries permitted by the two-tier engine.
- Condor retest: quiet/unlabeled event buckets only, small 10% sleeve, lower-delta short strikes.

## Results

| Run | Test days | Trades | Stops | Stop rate | Net P&L | Gross credit sold | Max approx margin | Max margin / equity |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Event-aware two-tier | 86 | 34 | 11 | 32.4% | $5,959.81 | $84,830.00 | $141,360.00 | 1.09% |
| Quiet/rich condor retest | 86 | 61 | 16 | 26.2% | $20,632.79 | $57,690.00 | $86,080.00 | 0.66% |

## Sleeve Attribution

### Event-Aware Two-Tier

| Sleeve | Trades | Stops | Stop rate | Net P&L | Gross credit sold |
| --- | ---: | ---: | ---: | ---: | ---: |
| Core | 13 | 3 | 23.1% | $12,690.44 | $65,275.00 |
| Exploratory | 21 | 8 | 38.1% | -$6,730.63 | $19,555.00 |

### Quiet/Rich Condor Retest

| Sleeve | Trades | Stops | Stop rate | Net P&L | Gross credit sold |
| --- | ---: | ---: | ---: | ---: | ---: |
| Core | 8 | 1 | 12.5% | $25,694.32 | $32,725.00 |
| Exploratory | 19 | 6 | 31.6% | -$3,139.04 | $18,200.00 |
| Condor | 34 | 9 | 26.5% | -$1,922.49 | $6,765.00 |

## Microstructure Windows

Targeted stopped-trade windows have been generated so tick or 10-second data can be downloaded only where it is useful.

- Baseline stopped-trade windows: `data/validation_13m_continuous_q2_q3/microstructure_windows.csv`
- Baseline windows: 11
- Condor retest stopped-trade windows: `data/validation_13m_continuous_q2_q3_quiet_condor/microstructure_windows.csv`
- Condor retest windows: 16

## Interpretation

The continuous Q2/Q3 run confirms that the conservative event-aware core is still the best part of the strategy. The exploratory sleeve added frequency but lost money, mainly through a higher stop rate. The tightly filtered condor sleeve recovered trade frequency and reduced headline stop rate and margin usage, but its own attribution was still slightly negative.

The best current improvement is not to scale the condor sleeve yet. The next step should be to improve entry selection for the exploratory sleeve and rebuild the long-vol overlay around the actual stopped-trade windows now generated from the continuous Q2/Q3 run.

## Recommended Next Implementation

1. Tighten exploratory sleeve eligibility using the continuous Q2/Q3 sample:
   - Require stronger post-10:00 confirmation.
   - Reduce or block exploratory entries in event buckets with repeated stops.
   - Add a same-direction re-entry cooldown after a stopped trade.

2. Use the generated stopped-trade windows for targeted tick or 10-second downloads:
   - Download only around the 11 baseline stopped trades first.
   - Compare stop trigger path versus quote spread/mark behavior.
   - Determine whether stops are real directional failures, quote artifacts, or intraday whipsaws.

3. Rebuild the long-vol overlay from actual failure modes:
   - Hedge only on dates matching the stopped-trade setup.
   - Prefer small same-day debit structures around known failure windows.
   - Validate overlay cost against the $13M account and max margin constraints.

4. Keep the condor sleeve disabled for production sizing until it is positive on its own attribution.

