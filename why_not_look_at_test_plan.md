# Why-Not-Look-At Test Plan (anti-overfit)

Date: 2026-07-15

## Anti-overfit protocol

| Split | Role | Dates |
|---|---|---|
| Rolling feature train | 40 eligible days of baselines before each OOS day | walk-forward |
| **Selection** | Rank variants / pick phase winners **only here** | OOS ≤ `2023-12-29` |
| **Holdout (sealed)** | Promotion validation only — **never retune** | OOS ≥ `2024-01-02` |

Promotion gate vs baseline holdout:

- Calmar ≥ baseline − 0.05
- CAGR not worse by > 1pp
- Worst day not worse by > 0.5pp
- No hard reject on selection (CAGR / DD / worst-day floors)

## Waves / runners

| Wave | Runner coverage |
|---|---|
| W0 instrumentation | FOMC calendar + VIX-5 helper + diagnostics in summarize |
| W1 VIX-5 | `W1_1`…`W1_4` backtests; `W1_5` attribution |
| W2 structure | delta / credit / adjust variants |
| W3 sides | policy variants; D1/D2 via attribution hooks |
| W4 straddles | short ATM straddle day sim + overlay |
| W5 early exit | profit-take / time-exit knobs |
| W6 Fed | FOMC skip / half / cutoff / puts-only; D attribution |
| W7 liquidity | prefer/require 25s |

## Commands

```powershell
.\scripts\run_why_not_look_at_parallel.ps1
# or manually:
python scripts/run_why_not_look_at_suite.py --shard 0 --shards 8 --resume --checkpoint-every 10
python scripts/merge_why_not_look_at_shards.py --shards 8
python scripts/summarize_why_not_look_at_suite.py
```

Outputs: `data/why_not_look_at/` (`checkpoint.json` per shard, `summary_*.json`, `report.json`, `SUMMARY.md`).
