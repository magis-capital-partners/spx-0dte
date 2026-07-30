# Test 3G — Time-of-Day Contract Weighting (on 3D_flatten_3.5)

Generated: 2026-07-01 12:43

Substrate: frozen `3D_flatten_3.5` (wide wings put 200 / call 75, 3x short-leg stop w/ 2-bar confirm, halt -2.25%, flatten -3.5%). Only the per-tranche contract size is reshaped by time of day. Baseline 31 contracts, 391 OOS days, $13,000,000 equity.

Goal: sell more early / less late to cut peak concentration and tail (worst-day and max-drawdown) risk, and read the return give-up against the flat control.

## Results

| Scheme | Avg size | CAGR | Sharpe | Worst day | Max DD | Day win | Contracts vs control |
|---|---:|---:|---:|---:|---:|---:|---:|
| control_flat | 1.00x | 26.3% | 1.32 | -4.58% | 10.54% | 64.5% | 100.0% |
| linear_decay_neutral | 0.93x | 23.4% | 1.12 | -4.34% | 11.41% | 64.2% | 98.0% |
| linear_decay_downsize | 0.73x | 20.8% | 1.17 | -4.49% | 9.02% | 64.5% | 79.5% |
| step_3block_mild | 0.92x | 25.8% | 1.25 | -4.66% | 11.32% | 63.9% | 97.1% |
| step_3block_aggressive | 0.76x | 23.0% | 1.24 | -4.47% | 9.21% | 64.5% | 81.2% |
| front_load_morning | 0.75x | 22.8% | 1.21 | -4.60% | 9.89% | 65.2% | 82.4% |
| morning_heavy_afternoon_off | 0.58x | 15.1% | 0.96 | -4.30% | 10.69% | 65.0% | 66.0% |
| half_after_noon | 0.71x | 19.1% | 1.16 | -4.60% | 9.55% | 64.7% | 75.2% |
| taper_4step | 0.75x | 21.4% | 1.17 | -4.49% | 9.28% | 64.2% | 80.9% |

## Scheme definitions (multiplier of baseline by entry time)

- **control_flat**: 09:32-close: 1x
- **linear_decay_neutral**: 09:32-10:30: 1.5x, 10:30-11:30: 1.25x, 11:30-12:30: 1x, 12:30-13:30: 0.75x, 13:30-14:30: 0.6x, 14:30-close: 0.5x
- **linear_decay_downsize**: 09:32-10:30: 1.25x, 10:30-11:30: 1x, 11:30-12:30: 0.85x, 12:30-13:30: 0.6x, 13:30-14:30: 0.45x, 14:30-close: 0.25x
- **step_3block_mild**: 09:32-11:30: 1.25x, 11:30-13:30: 1x, 13:30-close: 0.5x
- **step_3block_aggressive**: 09:32-11:00: 1.5x, 11:00-13:00: 0.75x, 13:00-close: 0.33x
- **front_load_morning**: 09:32-12:00: 1.25x, 12:00-14:00: 0.5x, 14:00-close: 0.25x
- **morning_heavy_afternoon_off**: 09:32-12:00: 1x, 12:00-14:00: 0.5x, 14:00-close: 0x
- **half_after_noon**: 09:32-12:00: 1x, 12:00-close: 0.5x
- **taper_4step**: 09:32-10:30: 1.5x, 10:30-12:00: 1x, 12:00-13:30: 0.6x, 13:30-close: 0.3x

## How to read this

- `control_flat` reproduces production 3D (flat 31 contracts).
- Schemes with avg size ~1.0x are pure reshaping (same capital, timed differently).
- Schemes with avg size < 1.0x also net-downsize the book.
- Prefer schemes that materially improve worst-day / max-DD for a small CAGR give-up.
