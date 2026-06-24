# Strategy Run Comparison

Account equity: $13,000,000

| Run | Days | Trades | Stops | Stop rate | Net P&L | Annualized return | Avg daily credit | Max margin | Max margin / equity |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| validation_13m_continuous_q2_q3_rebuilt_guard_time_exploratory240 | 86 | 16 | 4 | 25.0% | $22,525.75 | 0.51% | $668.37 | $141,360.00 | 1.09% |
| validation_13m_continuous_q2_q3_guard_time_exploratory240_one_dte | 86 | 94 | 19 | 20.2% | $-21,945.42 | -0.49% | $891.10 | $84,725.00 | 0.65% |
| validation_13m_continuous_q2_q3_guard_time_exploratory240_one_dte_late | 86 | 35 | 7 | 20.0% | $19,323.58 | 0.44% | $789.07 | $141,360.00 | 1.09% |

## MBH-Style Deployment Reference

- 30% annual return target on this equity: $3,900,000, or about $15,476 per trading day.
- 40% annual return target on this equity: $5,200,000, or about $20,635 per trading day.
- 1.5% daily gross premium reference: $195,000 per day.
- 40% average margin reference: $5,200,000.
