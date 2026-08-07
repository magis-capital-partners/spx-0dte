# Size ladder (Phase 5)

Size against **stressed** Calmar / max DD from `data/stop_fill_stress/`, not the optimistic dashboard 19.85% / 8.29%.

| Stage | Contracts / equity | Minimum evidence |
|---|---|---|
| Pilot | 4-lot @ $500k (`live_config.ACTIVE`, cap 5 on elevated VIX; raised from 2 on 2026-08-06) | Phases 1–2 in code; recovery stop = 3× short premium |
| Paper stress | Same size, ≥10 sessions + daily `shadow_sim_day.py` | Median shadow gap acceptable; no recovery refuse-starts |
| Small live | Same size live only if `allow_live=True` | Stressed holdout known; outage kill-drill done |
| Scale | Step toward ToD schedule using stressed DD risk budget | N live weeks without Phase-1/2 class bugs |

Commands:

```text
python scripts/run_stop_fill_stress.py --max-days 60
python scripts/shadow_sim_day.py --date today
```
