# Compounding Position Sizing — Results

Date: 2026-07-27

Production path (`p3_poststop_cooldown_120`) with equity-proportional contract sizing.
Selection <= `2023-12-29` | Holdout (rebased) >= `2024-01-02`

**Dashboard export: `p3_poststop_compounding_f1` (full compounding). Recommended sizing factor for discussion: `full`.**

## Full sample (sequential re-simulation)

| Variant | Calmar | CAGR | MaxDD | Worst | Sharpe | Peak k | Ending equity |
|---|---:|---:|---:|---:|---:|---:|---:|
| `cap_3x` | 2.808 | 32.35% | 11.52% | -6.61% | 1.88 | 3.00× | $70,036,458 |
| `fractional_f050` | 2.670 | 25.18% | 9.43% | -5.89% | 1.86 | 2.00× | $50,103,987 |
| `cap_2x` | 2.623 | 28.30% | 10.79% | -6.57% | 1.84 | 2.00× | $58,097,960 |
| `fractional_f075` | 2.550 | 29.55% | 11.59% | -6.13% | 1.86 | 3.34× | $61,582,146 |
| `cap_4x` | 2.501 | 34.94% | 13.97% | -6.57% | 1.90 | 4.00× | $78,683,169 |
| `fractional_f025` | 2.464 | 22.10% | 8.97% | -5.39% | 1.85 | 1.36× | $43,139,671 |
| `fixed` | 2.393 | 19.84% | 8.29% | -5.10% | 1.84 | 1.00× | $38,559,517 |
| `cap_6x` | 2.324 | 36.56% | 15.73% | -6.57% | 1.85 | 6.00× | $84,530,934 |
| `full` | 2.310 | 36.34% | 15.73% | -6.57% | 1.83 | 6.98× | $83,712,091 |
| `hwm` | 2.250 | 38.20% | 16.98% | -6.67% | 1.85 | 7.55× | $90,807,098 |
| `band_10` | 2.226 | 36.00% | 16.17% | -6.12% | 1.84 | 6.59× | $82,468,302 |
| `ratchet_63` | 2.198 | 35.23% | 16.03% | -6.47% | 1.84 | 6.29× | $79,707,428 |

## Selection (<= 2023-12-29)

| Variant | Calmar | CAGR | MaxDD | Worst | Peak k |
|---|---:|---:|---:|---:|---:|
| `cap_3x` | 3.045 | 35.08% | 11.52% | -6.61% | 2.94× |
| `fractional_f050` | 3.016 | 28.44% | 9.43% | -5.89% | 1.56× |
| `cap_2x` | 3.148 | 33.97% | 10.79% | -6.57% | 2.00× |
| `fractional_f075` | 3.118 | 31.58% | 10.13% | -6.13% | 2.09× |
| `cap_4x` | 3.091 | 35.58% | 11.51% | -6.57% | 2.98× |
| `fractional_f025` | 2.896 | 25.98% | 8.97% | -5.39% | 1.23× |
| `fixed` | 2.899 | 24.03% | 8.29% | -5.10% | 1.00× |
| `cap_6x` | 3.091 | 35.58% | 11.51% | -6.57% | 2.98× |
| `full` | 3.091 | 35.58% | 11.51% | -6.57% | 2.98× |
| `hwm` | 3.097 | 37.23% | 12.02% | -6.67% | 3.11× |
| `band_10` | 3.046 | 35.48% | 11.65% | -6.12% | 2.79× |
| `ratchet_63` | 2.951 | 34.94% | 11.84% | -6.47% | 2.78× |

## Holdout rebased (honest — restart at $13M on 2024-01-02)

| Variant | Calmar | CAGR | MaxDD | Worst | Peak k |
|---|---:|---:|---:|---:|---:|
| `cap_3x` | 2.389 | 37.53% | 15.71% | -5.88% | 2.44× |
| `fractional_f050` | 2.461 | 31.70% | 12.88% | -4.05% | 1.46× |
| `cap_2x` | 2.408 | 37.83% | 15.71% | -4.94% | 2.00× |
| `fractional_f075` | 2.424 | 34.35% | 14.17% | -4.81% | 1.85× |
| `cap_4x` | 2.389 | 37.53% | 15.71% | -5.88% | 2.44× |
| `fractional_f025` | 2.497 | 29.46% | 11.80% | -3.51% | 1.19× |
| `fixed` | 2.535 | 27.56% | 10.87% | -3.45% | 1.00× |
| `cap_6x` | 2.389 | 37.53% | 15.71% | -5.88% | 2.44× |
| `full` | 2.389 | 37.53% | 15.71% | -5.88% | 2.44× |
| `hwm` | 2.348 | 39.66% | 16.89% | -5.88% | 2.53× |
| `band_10` | 2.258 | 36.83% | 16.31% | -5.76% | 2.31× |
| `ratchet_63` | 2.296 | 36.60% | 15.94% | -5.11% | 2.30× |

## Holdout continuation (inherits selection equity — informational)

| Variant | Calmar | CAGR | MaxDD | Ending equity |
|---|---:|---:|---:|---:|
| `cap_3x` | 2.954 | 64.55% | 21.85% | $46,236,658 |
| `fractional_f050` | 2.733 | 42.75% | 15.64% | $32,194,115 |
| `cap_2x` | 2.983 | 48.06% | 16.11% | $35,332,974 |
| `fractional_f075` | 2.907 | 56.94% | 19.59% | $40,982,524 |
| `cap_4x` | 3.287 | 75.41% | 22.94% | $54,410,131 |
| `fractional_f025` | 2.611 | 33.68% | 12.90% | $27,235,083 |
| `fixed` | 2.535 | 27.56% | 10.87% | $24,168,052 |
| `cap_6x` | 3.251 | 82.58% | 25.40% | $60,257,896 |
| `full` | 3.213 | 81.60% | 25.40% | $59,439,053 |
| `hwm` | 3.194 | 88.02% | 27.56% | $64,942,130 |
| `band_10` | 3.054 | 80.22% | 26.27% | $58,295,441 |
| `ratchet_63` | 2.934 | 77.45% | 26.40% | $56,042,227 |

## Closed-form cross-check (homogeneity)

Should match sequential within rounding. Large gaps ⇒ homogeneity failure.

| Variant | Sim CAGR | Closed CAGR | Δ | Sim MaxDD | Closed MaxDD |
|---|---:|---:|---:|---:|---:|
| `cap_3x` | 32.35% | 32.60% | -0.25pp | 11.52% | 11.54% |
| `fractional_f050` | 25.18% | 25.24% | -0.06pp | 9.43% | 9.41% |
| `cap_2x` | 28.30% | 28.31% | -0.01pp | 10.79% | 10.80% |
| `fractional_f075` | 29.55% | 29.63% | -0.08pp | 11.59% | 11.56% |
| `cap_4x` | 34.94% | 35.04% | -0.10pp | 13.97% | 13.91% |
| `fractional_f025` | 22.10% | 22.15% | -0.05pp | 8.97% | 8.82% |
| `fixed` | 19.84% | 19.84% | +0.00pp | 8.29% | 8.29% |
| `cap_6x` | 36.56% | 36.68% | -0.12pp | 15.73% | 15.71% |
| `full` | 36.34% | 36.47% | -0.13pp | 15.73% | 15.71% |
| `hwm` | 38.20% | 38.38% | -0.18pp | 16.98% | 16.89% |
| `band_10` | 36.00% | 36.08% | -0.08pp | 16.17% | 16.15% |
| `ratchet_63` | 35.23% | 35.42% | -0.19pp | 16.03% | 15.95% |

## Interpretation

- Fixed-size production: **19.84% CAGR**, **8.29% max DD**, Calmar 2.39.
- Full compounding (f=1): **36.34% CAGR**, **15.73% max DD**, Calmar 2.31, peak size **6.98×**.
- Compounding is leverage-through-time, not alpha: Calmar stays roughly flat while CAGR and drawdown scale together.
- Production's 8.3% max DD is flattered by not resizing into a larger book; the stationary DD risk under f=1 is closer to 15.7%.
- Dashboard run id: `p3_poststop_compounding_f1` (primary remains `p3_poststop_cooldown_120`).
- Capacity caveat: peak tranche size ≈ 48 × peak_k contracts; simulator has no market-impact model.

