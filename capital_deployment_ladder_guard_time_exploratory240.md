# Capital Deployment Ladder

Source run: `data\validation_13m_continuous_q2_q3_guard_time_exploratory240\daily_regime_validation.csv`

This is a first-order linear scaling estimate. It does not assume fills, halt behavior, or slippage remain unchanged at larger size.

| Target max margin / equity | Scale | Est. net P&L | Est. annual return | Est. avg daily credit | Est. worst day |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 2.0% | 1.84x | $41,431.06 | 0.93% | $1,229.32 | $-19,611.18 |
| 5.0% | 4.60x | $103,577.66 | 2.33% | $3,073.30 | $-49,027.96 |
| 10.0% | 9.20x | $207,155.31 | 4.67% | $6,146.60 | $-98,055.92 |
| 20.0% | 18.39x | $414,310.63 | 9.34% | $12,293.20 | $-196,111.84 |
| 30.0% | 27.59x | $621,465.94 | 14.01% | $18,439.81 | $-294,167.76 |
| 40.0% | 36.79x | $828,621.25 | 18.68% | $24,586.41 | $-392,223.68 |

Use this as a sizing screen only. Any tier that looks attractive still needs a real rerun with slippage, credit caps, and daily loss halt behavior.
