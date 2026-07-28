# Outage / restart runbook (Phase 4)

Paper or live session disruption. Keep `allow_live=false` until Phases 1–2 are verified in paper.

## Always true after remediation

- Entry fills in `fills.jsonl` include `short_entry_sell` and `long_entry_buy`.
- Recovery rebuilds `stop_price = short_entry_sell × stop_multiple` (production **3× short premium**).
- Missing `short_entry_sell` → **refuse start** (SystemExit). Do not guess from net credit.
- Native STP default trigger is **4.5×** short premium (wider than synthetic 3×).
- Synthetic stop requires **120s** continuous breach before buy-to-cover.

## Scenario A — Process killed with open verticals

1. Restart `python live/ib_executor.py`.
2. Expect `session_recovered` with open spreads; verify printed stop ≈ `3 × short_entry_sell`.
3. Native STPs re-arm at 4.5×.
4. Feature state reloads from `data/live/<date>/feature_state.json` when present.

## Scenario B — Native STP fired while process was dead (short covered, long remains)

1. Restart. Recovery detects IB short flat + long held → marks spread **stopped** (manage-only wing).
2. Warning: `marked stopped wing … likely native STP while down`.
3. Do **not** manually short the short strike again; let the wing settle or flatten if governors demand.

## Scenario C — Unexplained IB residual (not a stopped wing)

1. Executor SystemExit with expected / ib / residual nets.
2. Reconcile in TWS (flatten or match fills), then restart.
3. Never delete `fills.jsonl` to “force” a clean start while risk is open.

## Scenario D — Gateway disconnect ≤120s

1. Entries halt; pending entry cancels; reconnect backoff.
2. On reconnect: book verified, native STPs re-armed.
3. If reconnect fails with open risk → flatten attempt + exit.

## Kill drill (paper)

1. Open at least one paper vertical.
2. Kill the executor PID.
3. Restart within a few minutes; confirm stop_price and STP re-arm.
4. Optionally trigger a wide print past 4.5× while dead, then restart and confirm manage-only wing path.
5. Attach `fills.jsonl` + console excerpt to the session notes.
