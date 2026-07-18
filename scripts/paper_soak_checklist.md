# Paper soak checklist (portable)

Run on whichever machine hosts the paper executor. Goal: prove restart, KILL,
disconnect, and mark/stale paths leave the expected events in `fills.jsonl`.

## Setup

```powershell
$env:SPX_SLACK_WEBHOOK_URL = "https://hooks.slack.com/services/..."   # optional
python scripts/refresh_live_baselines.py
# Terminal A
python live/ib_executor.py
# Terminal B (same machine)
.\scripts\run_live_watchdog.ps1
```

## Drills

| # | Drill | How | Expect in `data/live/<date>/fills.jsonl` |
|---|-------|-----|------------------------------------------|
| 1 | Restart after halt | Force a halt (or inject `halt_entries` then restart) | `governor_recovered` with `entries_halted=true`; no new `entry` while halted |
| 2 | KILL file | `echo. > data\live\KILL` (Windows) / `touch data/live/KILL` | `kill_switch`, `flatten`, `flatten_audit` |
| 3 | Disconnect | Stop IB Gateway briefly mid-session with open risk | `ib_disconnected`, then `ib_reconnect` or exit after flatten |
| 4 | Watchdog | Kill executor PID while open risk; leave watchdog running | Slack `watchdog_alert` (and optional session `KILL` if `-WriteKill`) |
| 5 | Stale / mark | Block market data briefly with open risk | `halt_entries` reason `stale_quotes` or `mark_*`; flatten only if mark unavailable ≥60s |

## Verify

```powershell
python scripts/verify_soak_events.py --date YYYY-MM-DD --expect kill,flatten,governor
```

Remove `data/live/KILL` (and session KILL) before the next start.
