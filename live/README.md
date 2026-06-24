# Live Execution (Interactive Brokers)

This package takes the validated 0DTE SPX vertical-spread strategy from the
`simulator/` backtest and runs it live against Interactive Brokers, using the
**same** `StrategyConfig`, candidate selection, short-leg stop, and daily-loss
flatten governor as the backtest. The goal is that a given trading day's live
decisions match what the backtest would have produced from the same chain.

## Architecture

```
                ┌─────────────────────────────────────────────┐
                │  ib_executor.py  (run during RTH)            │
                │                                              │
  IB Gateway ──▶│  1. MarketData: pull SPXW 0DTE + 1DTE chain  │
   / TWS        │  2. feature snapshot → SignalSnapshot        │
                │  3. simulator.select_candidate_entries(...)  │
                │  4. risk gates + allocator (shared config)   │
                │  5. place BAG combo (sell short / buy wing)  │
                │  6. attach STP on short leg (stop_multiple)  │
                │  7. mark book each tick; flatten on daily    │
                │     loss limit (same governor as backtest)   │
                │  8. log fills → data/live/<date>/fills.jsonl │
                └─────────────────────────────────────────────┘
```

## Prerequisites

- IB Gateway or TWS running with API enabled (default paper port 7497, live 7496).
- Market data: OPRA (US options) + index (SPX) subscription for live Greeks.
- `pip install ib_insync` (added to `requirements.txt`).

## Safe rollout

1. **Dry run** (`--mode dry`): compute and log intended trades, place nothing.
2. **Paper** (`--mode paper`, port 7497): full order flow on IB paper account.
3. **Live pilot** (`--mode live`, port 7496): start with `--contract-scale 0.05`
   to trade a small fraction of the validated size, confirm fills/stops behave,
   then ramp.

## Best validated config (from backtest)

The backtest's best risk-adjusted configuration on $13M equity with abundant
margin is the **2x-deployment + flatten-on-loss** profile (see
`../cagr_improvement_results_2026-06-24.md`). Pass `--profile best` to load it.

> Live trading involves real financial risk. The backtest uses 1-minute quotes
> and modeled fills; live slippage on 0DTE stops can be worse. Always start in
> paper and a small live pilot.
