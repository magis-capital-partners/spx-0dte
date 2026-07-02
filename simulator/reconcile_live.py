"""Reconcile a live/paper session against the backtest for the same day.

Reads a session's ``data/live/<date>/config.json`` + ``fills.jsonl``, rebuilds
the exact ``StrategyConfig`` that ran, and replays that date through
``simulate_day`` (once the processed chain exists). Emits a side-by-side diff so
divergence can be attributed to execution (fills/slippage/timing) vs logic.

This closes the iteration loop: every config change is validated end-to-end by
comparing what live actually did against what the backtest would have done.

Usage (date defaults to today):
    python simulator/reconcile_live.py --date 2026-07-02
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))
sys.path.insert(0, str(ROOT / "live"))

from mbh_simulator import read_quotes_csv, read_signals_csv, simulate_day  # noqa: E402
from regime_validation import apply_rolling_baseline, discover_dates  # noqa: E402
from unconditional_baseline import FixedSizePolicy  # noqa: E402

LIVE_DIR = ROOT / "data" / "live"
DEFAULT_PROCESSED = ROOT / "data" / "processed"


def load_live_session(day: str) -> tuple:
    day_dir = LIVE_DIR / day
    config_path = day_dir / "config.json"
    fills_path = day_dir / "fills.jsonl"
    if not config_path.exists():
        raise SystemExit(f"no session snapshot at {config_path}")
    snapshot = json.loads(config_path.read_text(encoding="utf-8"))
    events = []
    if fills_path.exists():
        for line in fills_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return snapshot, events


def summarize_live(events: list) -> dict:
    entries = [e for e in events if e.get("event") == "entry"]
    stops = [e for e in events if e.get("event") == "stop"]
    return {
        "entries": len(entries),
        "sides": dict(Counter(e.get("side") for e in entries)),
        "contracts": sum(int(e.get("contracts", 0)) for e in entries),
        "short_strikes": sorted({e.get("short_strike") for e in entries}),
        "stops": len(stops),
        "flattened": any(e.get("event") == "flatten" for e in events),
        "halted": any(e.get("event") == "halt_entries" for e in events),
    }


def replay_backtest(day: str, snapshot: dict, processed_dir: Path, train_count: int) -> dict:
    """Replay the same date through the backtest using the saved config."""
    from live_config import LiveConfig
    from strategy_profiles import resolve_strategy_config

    live = LiveConfig(**snapshot["live_config"])
    config, schedule = resolve_strategy_config(live)

    dates = discover_dates(processed_dir, "SPXW")
    if day not in dates:
        return {"available": False, "reason": f"no processed chain for {day} yet"}
    idx = dates.index(day)
    if idx < train_count:
        return {"available": False, "reason": f"need {train_count} prior days; have {idx}"}

    train_dates = dates[idx - train_count:idx]
    apply_rolling_baseline(processed_dir, "SPXW", train_dates, day, "signals_unconditional.csv")
    day_dir = processed_dir / "symbol=SPXW" / f"date={day}"
    policy = FixedSizePolicy()
    if schedule is not None:
        from time_of_day_sizing_runner import TimeOfDaySizePolicy

        policy = TimeOfDaySizePolicy(schedule)

    result = simulate_day(
        read_quotes_csv(day_dir / "normalized_option_quotes.csv"),
        read_signals_csv(day_dir / "signals_unconditional.csv"),
        config=config,
        policy=policy,
    )
    trades = result.trades
    return {
        "available": True,
        "entries": len(trades),
        "sides": dict(Counter(t.side for t in trades)),
        "contracts": sum(int(t.contracts) for t in trades),
        "short_strikes": sorted({t.short_strike for t in trades}),
        "stops": sum(1 for t in trades if t.stopped),
        "net_pnl": round(result.net_pnl, 2),
        "halted": result.halted,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconcile a live/paper session vs backtest.")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--processed-dir", default=str(DEFAULT_PROCESSED))
    parser.add_argument("--train-count", type=int, default=40)
    args = parser.parse_args()

    snapshot, events = load_live_session(args.date)
    live_summary = summarize_live(events)
    bt_summary = replay_backtest(args.date, snapshot, Path(args.processed_dir), args.train_count)

    report = {
        "date": args.date,
        "profile": snapshot.get("live_config", {}).get("profile"),
        "sizing_scheme": snapshot.get("sizing_scheme"),
        "mode": snapshot.get("live_config", {}).get("mode"),
        "live": live_summary,
        "backtest": bt_summary,
    }
    out_path = LIVE_DIR / args.date / "reconcile.json"
    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    print(f"=== Reconcile {args.date} (profile={report['profile']}, mode={report['mode']}) ===")
    print(f"LIVE     : entries={live_summary['entries']} contracts={live_summary['contracts']} "
          f"stops={live_summary['stops']} sides={live_summary['sides']}")
    if bt_summary.get("available"):
        print(f"BACKTEST : entries={bt_summary['entries']} contracts={bt_summary['contracts']} "
              f"stops={bt_summary['stops']} sides={bt_summary['sides']} net_pnl=${bt_summary['net_pnl']:,.0f}")
        d_entries = live_summary["entries"] - bt_summary["entries"]
        d_contracts = live_summary["contracts"] - bt_summary["contracts"]
        print(f"DIFF     : entries={d_entries:+d} contracts={d_contracts:+d} "
              f"(differences beyond fills/slippage indicate a logic gap -- e.g. live signal parity)")
    else:
        print(f"BACKTEST : unavailable -- {bt_summary.get('reason')}")
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
