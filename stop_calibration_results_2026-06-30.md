# Stop Calibration Results — Test 3A–3F

> ⚠️ **VOID — DO NOT USE (marked 2026-08-06, plan E2).** This report was generated
> from 3-day smoke-test artifacts after the accidental 5-day overwrite noted in
> `full_test_suite_report_2026-06-30.md` — its header claims 391 OOS days but
> `data/stop_calibration/phase_winners.json` shows `"days": 3`. The 576–689% CAGRs,
> the 3A/3C/3D phase winners, and the `3F_gated_2.50` "final recommendation"
> (a zero-trade config selected by a ranking bug) are all artifacts. The
> authoritative results are `data/stop_calibration/calibration_summary.csv` and
> `full_test_suite_report_2026-06-30.md`. The Wave 4 suite (`scripts/
> merge_calmar_wave4_shards.py`) now asserts day counts and minimum trade counts
> so this bug class cannot recur.

Generated: 2026-06-30 21:52

Substrate: wide wings (put 200pt / call 75pt), 391 OOS days, gates off except 3F.

## Phase winners

- **3A** → `3A_stop_2.0x`: CAGR 576.2%, Sharpe 17.26, worst -0.2%, win 75.4%, stop 23.2%
- **3B** → `3B_ask_baseline`: CAGR 576.2%, Sharpe 17.26, worst -0.2%, win 75.4%, stop 23.2%
- **3C** → `3C_no_same_strike`: CAGR 688.9%, Sharpe 20.67, worst -0.1%, win 82.3%, stop 17.7%
- **3D** → `3D_halt_2.25`: CAGR 688.9%, Sharpe 20.67, worst -0.1%, win 82.3%, stop 17.7%
- **3F** → `3F_gated_2.50`: CAGR 0.0%, Sharpe 0.00, worst 0.0%, win 0.0%, stop 0.0%

## All variants

| Phase | Variant | CAGR | Sharpe | Worst% | Win% | Stop% | E[trade] | Avg stop P&L |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| 3A | 3A_stop_2.0x | 576.2% | 17.26 | -0.2% | 75.4% | 23.2% | $4,336 | $-7,000 |
| 3A | 3A_stop_3.5x | 572.5% | 13.15 | -0.5% | 79.7% | 8.7% | $4,323 | $-15,883 |
| 3A | 3A_stop_3.0x | 537.1% | 13.77 | -0.5% | 78.3% | 13.0% | $4,199 | $-13,162 |
| 3A | 3A_stop_2.5x | 426.0% | 12.04 | -0.6% | 76.8% | 20.3% | $3,760 | $-11,078 |
| 3B | 3B_ask_baseline | 576.2% | 17.26 | -0.2% | 75.4% | 23.2% | $4,336 | $-7,000 |
| 3B | 3B_ask_slip_0.05 | 565.7% | 16.99 | -0.2% | 75.4% | 23.2% | $4,300 | $-7,155 |
| 3B | 3B_short_mid | 537.6% | 16.14 | -0.3% | 75.4% | 23.2% | $4,201 | $-7,581 |
| 3B | 3B_confirm_2bar | 537.6% | 15.99 | -0.3% | 75.4% | 23.2% | $4,201 | $-7,581 |
| 3B | 3B_spread_2.0x | 511.0% | 13.08 | -0.5% | 78.3% | 14.5% | $4,104 | $-12,861 |
| 3B | 3B_spread_1.5x | 434.8% | 12.22 | -0.6% | 76.8% | 20.3% | $3,799 | $-10,890 |
| 3C | 3C_no_same_strike | 688.9% | 20.67 | -0.1% | 82.3% | 17.7% | $5,220 | $-7,711 |
| 3C | 3C_cooldown_120 | 583.0% | 19.08 | -0.1% | 81.5% | 18.5% | $5,570 | $-7,994 |
| 3C | 3C_cooldown_nostrike | 583.0% | 19.08 | -0.1% | 81.5% | 18.5% | $5,570 | $-7,994 |
| 3C | 3C_max2_stops_side | 580.5% | 19.95 | -0.1% | 78.3% | 20.0% | $5,003 | $-7,552 |
| 3C | 3C_baseline | 576.2% | 17.26 | -0.2% | 75.4% | 23.2% | $4,336 | $-7,000 |
| 3D | 3D_halt_2.25 | 688.9% | 20.67 | -0.1% | 82.3% | 17.7% | $5,220 | $-7,711 |
| 3D | 3D_flatten_2.25 | 688.9% | 20.67 | -0.1% | 82.3% | 17.7% | $5,220 | $-7,711 |
| 3D | 3D_flatten_3.5 | 688.9% | 20.67 | -0.1% | 82.3% | 17.7% | $5,220 | $-7,711 |
| 3F | 3F_gated_2.50 | 0.0% | 0.00 | 0.0% | 0.0% | 0.0% | $0 | $0 |
| 3F | 3F_ablate_cheap_2.40 | 0.0% | 0.00 | 0.0% | 0.0% | 0.0% | $0 | $0 |
| 3F | 3F_harvest_2.50 | 0.0% | 0.00 | 0.0% | 0.0% | 0.0% | $0 | $0 |
| 3F | 3F_event_time_2.50 | 0.0% | 0.00 | 0.0% | 0.0% | 0.0% | $0 | $0 |

## Final recommendation

Best overall calibrated config: **`3F_gated_2.50`** (0.0% CAGR, 0.0% worst day).

MBH targets: ~30–40% CAGR, ~65% win, ~4–5% worst day, ~2.25% all-stop portfolio cap.
