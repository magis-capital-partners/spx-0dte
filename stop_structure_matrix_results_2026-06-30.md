# Stop + Structure Matrix — Test 2

Generated: 2026-06-30 15:12

Unconditional cadence (391 OOS days, gates off, 31 contracts/tranche).

## Results

| Variant | CAGR | Sharpe | Worst day | Spread win% | Spread stop% | Spread E[trade] | Overlay trades |
|---|---:|---:|---:|---:|---:|---:|---:|
| wide_wings_no_stop | 27.8% | 0.85 | -25.3% | 83.3% | 0.0% | $720 | 0 |
| mbh_like | 20.8% | 0.69 | -25.3% | 83.3% | 0.0% | $720 | 8,354 |
| stop_3.0 | 13.3% | 0.84 | -6.3% | 74.3% | 25.3% | $300 | 0 |
| stop_2.5 | 11.6% | 0.73 | -5.0% | 69.4% | 31.1% | $261 | 0 |
| wide_wings_stop_2.0 | 11.1% | 0.79 | -6.6% | 58.3% | 41.4% | $276 | 0 |
| no_stop | 10.7% | 0.49 | -26.5% | 85.3% | 0.0% | $240 | 0 |
| stop_2.0_baseline | 8.9% | 0.60 | -3.7% | 61.5% | 39.6% | $198 | 0 |
| net_long_no_stop | -0.8% | 0.16 | -26.7% | 85.3% | 0.0% | $240 | 9,254 |
| net_long_stop_2.0 | -2.7% | -0.06 | -4.5% | 61.5% | 39.6% | $198 | 9,254 |

## Variant descriptions

- **stop_2.0_baseline**: 2.0x short-leg stop, default wings (Test 1 repeat)
- **stop_2.5**: 2.5x short-leg stop, default wings
- **stop_3.0**: 3.0x short-leg stop, default wings
- **no_stop**: No per-trade stop — hold defined-risk to settlement
- **wide_wings_stop_2.0**: Put wing 200pt / call wing 75pt, 2.0x stop
- **wide_wings_no_stop**: Put wing 200pt / call wing 75pt, no stop
- **net_long_no_stop**: Net-long overlay (1.5x put / 1.8x call), no stop
- **net_long_stop_2.0**: Net-long overlay (1.5x put / 1.8x call), 2.0x stop
- **mbh_like**: Wide wings + net-long overlay + no stop (full MBH structure hypothesis)

## Interpretation

Best CAGR: **wide_wings_no_stop** at 27.8% (Sharpe 0.85, worst day -25.3%).

MBH target: ~30–40% CAGR, Sharpe ~2.5, worst day ~4–5%, win rate ~65%.
