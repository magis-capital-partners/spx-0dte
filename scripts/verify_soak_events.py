"""Verify a live session's fills.jsonl contains expected soak-drill events."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIVE_DIR = ROOT / "data" / "live"

ALIASES = {
    "kill": "kill_switch",
    "flatten": "flatten",
    "governor": "governor_recovered",
    "halt": "halt_entries",
    "disconnect": "ib_disconnected",
    "reconnect": "ib_reconnect",
    "audit": "flatten_audit",
    "stale": "halt_entries",  # checked with reason filter below
}


def load_events(today: str) -> list[dict]:
    path = LIVE_DIR / today / "fills.jsonl"
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument(
        "--expect",
        default="kill,flatten",
        help="Comma-separated aliases: kill,flatten,governor,halt,disconnect,reconnect,audit,stale",
    )
    args = parser.parse_args(argv)
    events = load_events(args.date)
    names = {e.get("event") for e in events}
    wanted = [a.strip() for a in args.expect.split(",") if a.strip()]
    missing = []
    for alias in wanted:
        event_name = ALIASES.get(alias, alias)
        if alias == "stale":
            ok = any(
                e.get("event") == "halt_entries" and e.get("reason") == "stale_quotes"
                for e in events
            )
            if not ok:
                missing.append("stale_quotes halt")
            continue
        if event_name not in names:
            missing.append(event_name)
    if missing:
        print(f"MISSING: {', '.join(missing)}")
        print(f"present: {sorted(n for n in names if n)}")
        return 1
    print(f"OK — found {wanted} in {args.date} ({len(events)} events)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
