# Strategy Run Comparison

Account equity: $13,000,000

| Run | Days | Trades | Stops | Stop rate | Net P&L | Annualized return | Avg daily credit | Max margin | Max margin / equity |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| validation_13m_continuous_q2_q3 | 86 | 34 | 11 | 32.4% | $5,959.81 | 0.13% | $986.40 | $141,360.00 | 1.09% |
| validation_13m_continuous_q2_q3_bear_call_guard | 86 | 27 | 8 | 29.6% | $11,412.48 | 0.26% | $893.31 | $141,360.00 | 1.09% |
| validation_13m_continuous_q2_q3_bear_call_guard_time_controls | 86 | 24 | 7 | 29.2% | $19,538.87 | 0.44% | $741.40 | $141,360.00 | 1.09% |
| validation_13m_continuous_q2_q3_guard_time_exploratory240 | 86 | 16 | 4 | 25.0% | $22,525.75 | 0.51% | $668.37 | $141,360.00 | 1.09% |
| validation_13m_continuous_q2_q3_guard_time_exploratory240_condor | 86 | 49 | 12 | 24.5% | $18,252.35 | 0.41% | $436.05 | $86,080.00 | 0.66% |
| validation_13m_continuous_q2_q3_quiet_condor | 86 | 61 | 16 | 26.2% | $20,632.79 | 0.47% | $670.81 | $86,080.00 | 0.66% |

## MBH-Style Deployment Reference

- 30% annual return target on this equity: $3,900,000, or about $15,476 per trading day.
- 40% annual return target on this equity: $5,200,000, or about $20,635 per trading day.
- 1.5% daily gross premium reference: $195,000 per day.
- 40% average margin reference: $5,200,000.
