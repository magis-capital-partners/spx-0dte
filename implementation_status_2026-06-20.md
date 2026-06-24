# Implementation Status - 2026-06-20

## Implemented

- ThetaData downloader for SPXW 0DTE and next-expiration first-order Greeks.
- Raw-to-processed feature builder.
- Historical no-lookahead baseline transformer for the disclosed signal families.
- Four-model ensemble policy in the simulator:
  - ATM Volatility Surface.
  - Skew.
  - Trend Breakout.
  - Durational Influence.
  - Model-confluence sleeves.
- Configurable backtest runner.
- Walk-forward grid runner.
- Long-volatility overlay runner with ATM straddle and direct-put modes.
- Strike-level holdings reconstruction at screenshot times.
- Snapshot scorer against DDQ/user position snapshots.
- Combined holdings tool for short-premium book plus long-vol overlay.

## Verified On Downloaded Pilot Data

Downloaded SPXW 0DTE and next-expiration data for:

- 2024-04-09
- 2024-07-12
- 2024-11-06
- 2025-04-24
- 2026-03-02

Verified workflows:

- Historical baseline walk-forward:
  - Training dates: 2024-04-09, 2024-07-12, 2024-11-06.
  - Test dates: 2025-04-24, 2026-03-02.
  - Fast verification grid completed successfully.
- Long-volatility overlay:
  - Six daily long-vol trades can be generated from signal triggers.
  - Direct-put overlay can materially increase net-long put exposure.
- Snapshot scoring:
  - Spread-only simple policy is too small and structurally incomplete.
  - Four-model ensemble increases book size and better reflects the DDQ's multi-strategy language.
  - Combined ensemble plus direct-put overlay gets much closer to DDQ total longs and long puts at 3 PM on 2026-03-02.

## Key Findings

The DDQ book shape cannot be matched by a single vertical-spread seller.

The strategy needs at least three layers:

1. Core 0DTE short-premium vertical spreads.
2. Multiple model sleeves that can fire independently at the same timestamp.
3. Long-volatility overlays, especially direct puts or put-heavy hedges, to explain the large net-long put exposure in the March 2 DDQ snapshot.

Current best qualitative read:

- The early 11 AM DDQ snapshot is balanced and lower exposure.
- The 3 PM DDQ snapshot is much larger and materially net-long, especially in puts.
- Retained wings alone are not enough to explain the full 3 PM put exposure.
- A direct-put or put-heavy long-volatility hedge layer is likely active in the DDQ snapshot.

## Current Calibration Gaps

- The ensemble model overtrades the 11 AM snapshot when sized to get closer to the 3 PM snapshot.
- Direct-put overlay improves long-put exposure but can overshoot net exposure if not paired with enough active short put/call risk.
- Exact strike matching remains weak because we still do not know:
  - actual wing selection rule,
  - long-vol hedge strike rule,
  - exact model sleeve activation thresholds,
  - whether screenshots include all accounts/classes or only Fund 1,
  - whether the snapshot expiration is definitely same-day only.

## Best Next Development Step

Download a larger sample before optimizing further:

- At least 60 trading days.
- Include 2025, 2026, low-VIX, high-VIX, CPI, FOMC, NFP, and known MBH screenshot days.

Then run:

1. Historical baseline generation.
2. Walk-forward grid search.
3. March 2 snapshot calibration.
4. Combined spread-plus-long-vol overlay scoring.
5. Parameter selection using both P&L and snapshot-shape fit.

Avoid optimizing solely to March 2. It is only one snapshot and will overfit.
