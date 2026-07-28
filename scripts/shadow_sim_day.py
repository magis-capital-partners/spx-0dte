"""Daily shadow-sim: compare production simulator vs live fills.jsonl (Phase 5).

  python scripts/shadow_sim_day.py --date 2026-07-28
  python scripts/shadow_sim_day.py --date today

Writes data/live/<date>/shadow_diff.json with entry/stop/PnL gaps.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
SIM = ROOT / "simulator"
sys.path.insert(0, str(SIM))
sys.path.insert(0, str(ROOT / "live"))

from mbh_simulator import read_quotes_csv, read_signals_csv, simulate_day  # noqa: E402
from profiles import (  # noqa: E402
    PRODUCTION_ACCOUNT_EQUITY,
    SCHEMES,
    build_p3_poststop_cooldown_config,
)
from vix_sizing_policies import build_production_vix_policy  # noqa: E402

PROCESSED = ROOT / "data" / "processed"
LIVE_DIR = ROOT / "data" / "live"


def _load_fills(day: str) -> List[dict]:
    path = LIVE_DIR / day / "fills.jsonl"
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _entry_keys(events: List[dict]) -> List[Tuple[str, float, float, int]]:
    out = []
    for e in events:
        if e.get("event") != "entry":
            continue
        out.append((
            str(e.get("side")),
            float(e.get("short_strike")),
            float(e.get("long_strike")),
            int(e.get("contracts") or 0),
        ))
    return out


def _stop_keys(events: List[dict]) -> List[Tuple[str, float]]:
    out = []
    for e in events:
        if e.get("event") != "stop":
            continue
        out.append((str(e.get("side")), float(e.get("short_strike"))))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default="today", help="YYYY-MM-DD or 'today'")
    parser.add_argument("--equity", type=float, default=None)
    args = parser.parse_args()
    day = date.today().isoformat() if args.date == "today" else args.date
    equity = float(args.equity or PRODUCTION_ACCOUNT_EQUITY)

    fills = _load_fills(day)
    live_entries = _entry_keys(fills)
    live_stops = _stop_keys(fills)

    qpath = PROCESSED / f"symbol=SPXW/date={day}/normalized_option_quotes.csv"
    spath = PROCESSED / f"symbol=SPXW/date={day}/signals_unconditional.csv"
    sim_trades = []
    sim_pnl = None
    sim_note = ""
    if qpath.is_file():
        quotes = read_quotes_csv(qpath)
        signals = read_signals_csv(spath) if spath.is_file() else []
        cfg = build_p3_poststop_cooldown_config(account_equity=equity)
        cfg = type(cfg)(**{**cfg.__dict__, "target_expiry": day}) if hasattr(cfg, "__dict__") else cfg
        from dataclasses import replace
        cfg = replace(cfg, target_expiry=day)
        policy = build_production_vix_policy(SCHEMES["linear_decay_downsize"])
        result = simulate_day(quotes, signals, config=cfg, policy=policy)
        sim_trades = result.trades
        sim_pnl = result.net_pnl
    else:
        sim_note = f"no processed quotes at {qpath}"

    sim_entries = [
        (t.side, float(t.short_strike), float(t.long_strike), int(t.contracts))
        for t in sim_trades
    ]
    sim_stops = [(t.side, float(t.short_strike)) for t in sim_trades if t.stopped]

    # Rough set diffs (order-insensitive).
    live_e_set = set(live_entries)
    sim_e_set = set(sim_entries)
    live_s_set = set(live_stops)
    sim_s_set = set(sim_stops)

    diff = {
        "date": day,
        "generated_at": datetime.now().isoformat(),
        "sim_note": sim_note,
        "live_entries": len(live_entries),
        "sim_entries": len(sim_entries),
        "entries_only_live": [list(x) for x in sorted(live_e_set - sim_e_set)],
        "entries_only_sim": [list(x) for x in sorted(sim_e_set - live_e_set)],
        "live_stops": len(live_stops),
        "sim_stops": len(sim_stops),
        "stops_only_live": [list(x) for x in sorted(live_s_set - sim_s_set)],
        "stops_only_sim": [list(x) for x in sorted(sim_s_set - live_s_set)],
        "sim_net_pnl": sim_pnl,
        "stop_rate_gap_pp": (
            (len(live_stops) / max(len(live_entries), 1) - len(sim_stops) / max(len(sim_entries), 1))
            * 100.0
        ),
        "alert": False,
        "alert_reasons": [],
    }
    reasons = []
    if abs(diff["stop_rate_gap_pp"]) > 5.0 and (live_entries or sim_entries):
        reasons.append(f"stop_rate_gap_pp={diff['stop_rate_gap_pp']:.1f}")
    if abs(len(live_entries) - len(sim_entries)) >= 2:
        reasons.append("entry_count_gap>=2")
    diff["alert"] = bool(reasons)
    diff["alert_reasons"] = reasons

    out_dir = LIVE_DIR / day
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "shadow_diff.json"
    out_path.write_text(json.dumps(diff, indent=2), encoding="utf-8")
    print(json.dumps(diff, indent=2))
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
