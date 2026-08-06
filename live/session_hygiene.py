"""Pre-session state hygiene checks and stale-file pruning.

Reports on the control files that gate a trading session, and prunes only what
is provably dead. Deliberately conservative about what it will delete:

  * ``data/live/KILL`` (global)      — reported as a hard failure, never removed.
    It blocks every future session, so it is either an intentional stop-everything
    switch or an incident worth a human look. Silently clearing it would defeat
    the only mechanism that survives a restart.
  * ``data/live/<today>/KILL``       — reported, never removed. Same reasoning,
    scoped to today.
  * ``data/live/<today>/CLEAR_*``    — reported. A leftover clear file is one-shot
    and would silently release the first matching halt of the session, so an
    unexplained one is a warning, not something to tidy away.
  * ``data/live/<past date>/KILL``   — pruned. A past date can never trade again,
    so these are pure clutter.

Also answers "did the executor actually start?", which is the failure mode no
existing task covers: the executor is manual by design, so a forgotten start
means the day silently passes with no trades and no alert.

Usage:
    python live/session_hygiene.py                  # pre-session checks + prune
    python live/session_hygiene.py --check-started   # assert session_start exists
    python live/session_hygiene.py --prune-days 5
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parents[1]
LIVE_DIR = ROOT / "data" / "live"

# Clear files are one-shot operator authorizations; a leftover is suspicious.
CLEAR_FILE_NAMES = ("CLEAR_STALE_HALT", "CLEAR_FLATTEN_HALT")

EXIT_OK = 0
EXIT_WARN = 1
EXIT_BLOCKED = 2


@dataclass
class HygieneReport:
    blockers: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    pruned: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        if self.blockers:
            return EXIT_BLOCKED
        if self.warnings:
            return EXIT_WARN
        return EXIT_OK

    def render(self) -> str:
        lines: List[str] = []
        for item in self.blockers:
            lines.append(f"BLOCKED: {item}")
        for item in self.warnings:
            lines.append(f"WARN:    {item}")
        for item in self.pruned:
            lines.append(f"pruned:  {item}")
        for item in self.notes:
            lines.append(f"ok:      {item}")
        return "\n".join(lines) if lines else "ok:      nothing to report"


def _parse_day(name: str) -> Optional[date]:
    try:
        return datetime.strptime(name, "%Y-%m-%d").date()
    except ValueError:
        return None


def check_kill_files(
    today: str, *, live_dir: Path = LIVE_DIR, report: HygieneReport
) -> None:
    global_kill = live_dir / "KILL"
    if global_kill.is_file():
        detail = global_kill.read_text(encoding="utf-8", errors="replace").strip()
        report.blockers.append(
            f"global KILL present at {global_kill} — blocks every session until "
            f"removed by hand{f' ({detail})' if detail else ''}"
        )
    else:
        report.notes.append("no global KILL")

    today_kill = live_dir / today / "KILL"
    if today_kill.is_file():
        detail = today_kill.read_text(encoding="utf-8", errors="replace").strip()
        report.blockers.append(
            f"today's KILL present at {today_kill} — the executor will refuse to "
            f"start until removed{f' ({detail})' if detail else ''}"
        )
    else:
        report.notes.append(f"no KILL for {today}")


def check_clear_files(
    today: str, *, live_dir: Path = LIVE_DIR, report: HygieneReport
) -> None:
    found = [
        name
        for name in CLEAR_FILE_NAMES
        if (live_dir / today / name).is_file()
    ]
    if found:
        report.warnings.append(
            f"leftover operator clear file(s) for {today}: {', '.join(found)} — "
            "these are one-shot and will release the first matching halt of the "
            "session. Remove unless you placed them deliberately."
        )
    else:
        report.notes.append(f"no leftover clear files for {today}")


def prune_stale_kill_files(
    today: str,
    *,
    live_dir: Path = LIVE_DIR,
    keep_days: int = 0,
    report: HygieneReport,
    dry_run: bool = False,
) -> None:
    """Remove KILL files from dates that can never trade again."""
    today_date = _parse_day(today)
    if today_date is None or not live_dir.exists():
        return
    for day_path in sorted(live_dir.iterdir()):
        if not day_path.is_dir():
            continue
        day = _parse_day(day_path.name)
        if day is None or day >= today_date:
            continue
        if (today_date - day).days <= keep_days:
            continue
        kill = day_path / "KILL"
        if not kill.is_file():
            continue
        if dry_run:
            report.pruned.append(f"would remove {kill}")
            continue
        try:
            kill.unlink()
        except OSError as exc:
            report.warnings.append(f"could not remove {kill}: {exc!r}")
        else:
            report.pruned.append(f"removed stale {kill}")


def session_started(today: str, *, live_dir: Path = LIVE_DIR) -> bool:
    """True when today's fills log records at least one session_start."""
    fills = live_dir / today / "fills.jsonl"
    if not fills.is_file():
        return False
    for line in fills.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") == "session_start":
            return True
    return False


def run_pre_session(
    today: str,
    *,
    live_dir: Path = LIVE_DIR,
    keep_days: int = 0,
    dry_run: bool = False,
) -> HygieneReport:
    report = HygieneReport()
    check_kill_files(today, live_dir=live_dir, report=report)
    check_clear_files(today, live_dir=live_dir, report=report)
    prune_stale_kill_files(
        today,
        live_dir=live_dir,
        keep_days=keep_days,
        report=report,
        dry_run=dry_run,
    )
    return report


def is_trading_day(day: str) -> bool:
    """Weekday and not a US market holiday.

    Mirrors scripts/is_spx_trading_day.py so a holiday cannot raise a spurious
    "executor never started" alert.
    """
    parsed = _parse_day(day)
    if parsed is None or parsed.weekday() >= 5:
        return False
    try:
        sys.path.insert(0, str(ROOT / "simulator"))
        from backfill_history import US_HOLIDAYS  # noqa: WPS433
    except Exception:  # noqa: BLE001
        return True  # fail toward alerting rather than silently skipping
    return day not in US_HOLIDAYS


def run_check_started(today: str, *, live_dir: Path = LIVE_DIR) -> HygieneReport:
    report = HygieneReport()
    if not is_trading_day(today):
        report.notes.append(f"{today} is not a trading day — no session expected")
        return report
    if session_started(today, live_dir=live_dir):
        report.notes.append(f"executor session_start recorded for {today}")
    else:
        report.blockers.append(
            f"no session_start recorded for {today} — the executor is manual and "
            "appears not to be running. Start it: "
            "powershell -ExecutionPolicy Bypass -File scripts/run_ib_executor_supervised.ps1"
        )
    return report


def _notify(message: str) -> None:
    """Best-effort Slack alert; never fail the check because Slack is down."""
    try:
        sys.path.insert(0, str(ROOT / "live"))
        from slack_notify import notify_slack  # noqa: WPS433

        notify_slack(message)
    except Exception as exc:  # noqa: BLE001
        print(f"(slack notify skipped: {exc!r})")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pre-session state hygiene for the SPX 0DTE live executor.",
    )
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--live-dir", type=Path, default=LIVE_DIR)
    parser.add_argument(
        "--check-started",
        action="store_true",
        help="Assert the executor recorded a session_start (run after the open).",
    )
    parser.add_argument(
        "--prune-days",
        type=int,
        default=0,
        help="Keep KILL files from the last N days before today (default 0).",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--notify",
        action="store_true",
        help="Send blockers and warnings to Slack.",
    )
    args = parser.parse_args()

    if args.check_started:
        report = run_check_started(args.date, live_dir=args.live_dir)
    else:
        report = run_pre_session(
            args.date,
            live_dir=args.live_dir,
            keep_days=args.prune_days,
            dry_run=args.dry_run,
        )

    print(f"=== session hygiene {args.date} ===")
    print(report.render())

    if args.notify and (report.blockers or report.warnings):
        lines = [f"[spx-0dte] session hygiene {args.date}"]
        lines += [f"BLOCKED: {b}" for b in report.blockers]
        lines += [f"WARN: {w}" for w in report.warnings]
        _notify("\n".join(lines))

    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
