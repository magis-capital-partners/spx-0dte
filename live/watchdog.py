"""Portable local watchdog: same machine as the executor.

Monitors ``executor.lock`` + ``heartbeat.json``. Alerts via Slack when the
executor dies or heartbeats stall while open risk remains. Optionally writes
a local KILL file.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "live"))

from heartbeat import heartbeat_age_seconds, read_heartbeat  # noqa: E402
from kill_switch import kill_paths  # noqa: E402
from session_recovery import _pid_alive, lock_path_for  # noqa: E402
from slack_notify import notify_slack  # noqa: E402

LIVE_DIR = ROOT / "data" / "live"


def watchdog_target_date(
    pinned_date: Optional[str],
    *,
    now: Optional[datetime] = None,
) -> str:
    """Resolve the session date, rolling automatically unless explicitly pinned."""
    if pinned_date:
        return pinned_date
    return (now or datetime.now()).date().isoformat()


def evaluate_watchdog(
    today: str,
    *,
    max_heartbeat_age: float = 30.0,
    startup_grace_seconds: float = 120.0,
    live_dir: Path = LIVE_DIR,
    now: Optional[datetime] = None,
) -> Optional[str]:
    """Return an alert reason, or None if healthy / nothing to watch."""
    clock = now or datetime.now()
    lock = lock_path_for(today, live_dir=live_dir)
    hb = read_heartbeat(today, live_dir=live_dir)

    lock_pid = None
    lock_started_at: Optional[datetime] = None
    if lock.is_file():
        try:
            lock_payload = json.loads(lock.read_text(encoding="utf-8"))
            lock_pid = int(lock_payload.get("pid", -1))
            if lock_payload.get("started_at"):
                lock_started_at = datetime.fromisoformat(lock_payload["started_at"])
        except Exception:
            lock_pid = -1

    if hb is None and lock_pid is None:
        return None  # no session

    open_count = int((hb or {}).get("open_count") or 0)
    age = heartbeat_age_seconds(hb, now=clock) if hb else None
    heartbeat_pid = int((hb or {}).get("pid") or -1)

    if lock_pid is not None and lock_pid > 0 and not _pid_alive(lock_pid):
        if open_count > 0 or hb is None:
            return f"executor_pid_dead pid={lock_pid} open_count={open_count}"
        return None

    # During a controlled restart, the new process acquires the lock before its
    # first heartbeat. The prior heartbeat can still report open risk and become
    # stale while IB book recovery and quote warmup run. Treat an alive, newly
    # started lock with a different PID as startup—not as a stalled old process.
    # Without this grace the watchdog wrote KILL during recovery on 2026-08-05,
    # causing an unintended flatten.
    if (
        lock_pid is not None
        and lock_pid > 0
        and heartbeat_pid != lock_pid
        and lock_started_at is not None
    ):
        lock_age = max((clock - lock_started_at).total_seconds(), 0.0)
        if lock_age <= startup_grace_seconds:
            return None

    if open_count > 0 and age is not None and age > max_heartbeat_age:
        return f"heartbeat_stale age={age:.0f}s open_count={open_count}"

    if open_count > 0 and hb is None and lock_pid is not None:
        return f"missing_heartbeat open_count_unknown lock_pid={lock_pid}"

    return None


def run_watchdog_loop(
    *,
    today: Optional[str] = None,
    poll_seconds: float = 10.0,
    max_heartbeat_age: float = 30.0,
    write_kill: bool = False,
    live_dir: Path = LIVE_DIR,
) -> None:
    watched_day = watchdog_target_date(today)
    print(
        f"[{datetime.now().isoformat()}] watchdog watching {watched_day} "
        f"(max_age={max_heartbeat_age}s, write_kill={write_kill})"
    )
    alerted = False
    while True:
        target_day = watchdog_target_date(today)
        if target_day != watched_day:
            print(
                f"[{datetime.now().isoformat()}] watchdog date rollover "
                f"{watched_day} -> {target_day}",
                flush=True,
            )
            watched_day = target_day
            alerted = False

        reason = evaluate_watchdog(
            watched_day,
            max_heartbeat_age=max_heartbeat_age,
            live_dir=live_dir,
        )
        if reason and not alerted:
            msg = f"[spx-0dte] watchdog_alert — {reason} date={watched_day}"
            print(f"[{datetime.now().isoformat()}] {msg}")
            notify_slack(msg, enabled=True)
            if write_kill:
                _global_kill, session_kill = kill_paths(
                    watched_day,
                    live_dir=live_dir,
                )
                session_kill.parent.mkdir(parents=True, exist_ok=True)
                session_kill.write_text(f"watchdog: {reason}\n", encoding="utf-8")
                print(f"[{datetime.now().isoformat()}] wrote KILL -> {session_kill}")
            alerted = True
        elif not reason:
            alerted = False
        time.sleep(poll_seconds)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Local live executor watchdog")
    parser.add_argument(
        "--date",
        default=None,
        help="Pin a session date for drills; omit for automatic daily rollover",
    )
    parser.add_argument("--poll-seconds", type=float, default=10.0)
    parser.add_argument("--max-heartbeat-age", type=float, default=30.0)
    parser.add_argument(
        "--write-kill",
        action="store_true",
        help="Write data/live/<date>/KILL when an alert fires",
    )
    parser.add_argument("--once", action="store_true", help="Evaluate once and exit")
    args = parser.parse_args(argv)

    if args.once:
        target_day = watchdog_target_date(args.date)
        reason = evaluate_watchdog(
            target_day,
            max_heartbeat_age=args.max_heartbeat_age,
        )
        if reason:
            print(reason)
            notify_slack(
                f"[spx-0dte] watchdog_alert — {reason}",
                enabled=True,
            )
            return 1
        print("ok")
        return 0

    run_watchdog_loop(
        today=args.date,
        poll_seconds=args.poll_seconds,
        max_heartbeat_age=args.max_heartbeat_age,
        write_kill=args.write_kill,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
