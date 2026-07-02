# Live Execution (Interactive Brokers)

This package takes the validated 0DTE SPX vertical-spread strategy from the
`simulator/` backtest and runs it live against Interactive Brokers, using the
**same** `StrategyConfig`, candidate selection, short-leg stop (with N-bar
confirmation), and halt/flatten governor as the backtest. The goal is that a
given trading day's live decisions match what the backtest would have produced
from the same chain.

## Single source of truth

The winning parameters live in exactly one place — `simulator/profiles.py`:

- `build_3d_flatten_config()` → the validated **`3d_flatten_3_5`** production
  candidate (wide wings 200/75, 3.0× short-leg stop + 2-bar confirm, halt
  entries −2.25%, flatten −3.5%, 31 contracts flat, gates off).
- `SCHEMES` → optional Test-3G time-of-day contract weighting.

The backtest runners, the dashboard export, and this live executor **all** build
their config from that module, so a change moves every consumer together. That
is what makes iterating on the strategy safe.

## How to run

There are **no command-line flags**. All runtime settings live in
`live/live_config.py` (`ACTIVE`). Edit that object, then:

```
python live/ib_executor.py
```

Key `LiveConfig` fields:

| Field | Meaning |
|---|---|
| `profile` | Strategy profile name from `simulator/profiles.py` (`3d_flatten_3_5`) |
| `sizing_scheme` | `""`/`control_flat` = flat book; e.g. `linear_decay_downsize` for Test-3G weighting |
| `account_equity` / `contract_scale` | Deployment size; `contract_scale` scales the validated size for pilots |
| `max_contracts_per_tranche` | Hard safety cap applied after all sizing |
| `mode` | `dry` (log only) / `paper` (7497) / `live` (7496, requires `allow_live=True`) |
| `dry_with_ib` | In dry mode, still connect to read the live chain (places nothing) |
| `poll_seconds`, `host`, `port`, `client_id`, `baselines_path` | IB connection / loop |

## Architecture

```
                ┌─────────────────────────────────────────────┐
                │  ib_executor.py  (run during RTH)            │
                │                                              │
  IB Gateway ──▶│  1. MarketData: pull SPXW 0DTE chain         │
   / TWS        │  2. feature snapshot → SignalSnapshot        │
                │  3. select_candidate_entries(...)            │
                │  4. per-tranche sizing (once per tranche)    │
                │  5. place BAG combo (sell short / buy wing)  │
                │  6. synthetic short-leg stop (N-bar confirm),│
                │     keep long wing; native STP as backstop   │
                │  7. mark book; HALT entries at loss limit,   │
                │     FLATTEN open positions at deeper limit   │
                │  8. 0DTE cash-settles at close (no EOD MKT)  │
                │  9. config.json snapshot + fills.jsonl log   │
                └─────────────────────────────────────────────┘
```

## Prerequisites

- IB Gateway or TWS running with API enabled (paper port 7497, live 7496).
- Market data: OPRA (US options) + index (SPX) subscription for live Greeks
  (delta drives strike selection — without it there are no candidates).
- `pip install ib_insync` (in `requirements.txt`).

## Safe rollout

1. **Dry, no IB** (`mode="dry"`): logs intended trades with neutral signals;
   exercises the full loop with no market data.
2. **Dry, live chain** (`mode="dry"`, `dry_with_ib=True`): reads the real chain,
   logs intended strikes/credits, places nothing.
3. **Paper** (`mode="paper"`): full order flow on the IB paper account.
4. **Live pilot** (`mode="live"`, `allow_live=True`): start with a small
   `contract_scale`, confirm fills/stops behave, then ramp.

## Iteration loop

Each strategy change flows through one loop:

1. Add/edit a profile (or `SCHEMES` entry) in `simulator/profiles.py`.
2. Backtest it — the runners import the same registry (`stop_calibration_runner`,
   `time_of_day_sizing_runner`, `robustness_study`).
3. Dry-run it live (`mode="dry"`, `dry_with_ib=True`); the `config.json`
   snapshot should match the backtest config.
4. Paper it, then **reconcile**:

```
python simulator/reconcile_live.py --date <session-date>
```

`reconcile_live.py` replays the session date through the backtest with the exact
saved config and diffs entries / contracts / stops / P&L. Differences beyond
fills/slippage indicate a logic gap.

## Known gap — signal parity

`IBSignalProvider._build_signal` currently returns a **neutral** snapshot (the
live z-score assembly is a marked seam). With the `3d_flatten_3_5` gates off,
trades still fire, but candidate *scoring/side selection* can differ from the
backtest (which uses real `signals_unconditional.csv`). Day-1 paper validates
**execution plumbing**; wiring the live features against `historical_baselines`
is the next iteration and is what `reconcile_live.py` measures.

> Live trading involves real financial risk. The backtest uses 1-minute quotes
> and modeled fills; live slippage on 0DTE stops can be worse. Always start in
> paper and a small live pilot.
