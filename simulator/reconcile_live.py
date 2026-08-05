"""Reconcile a live/paper session against the backtest for the same day.

Reads ``data/live/<date>/config.json``, ``fills.jsonl``, and ``tranches.jsonl``,
replays that date through ``simulate_day`` with the saved config (paper equity
and a normalized $13M scale), and emits a structured diff.

Usage:
    python simulator/reconcile_live.py --date 2026-07-02
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))
sys.path.insert(0, str(ROOT / "live"))

from expiry_calendar import (  # noqa: E402
    DEFAULT_RULES,
    discover_eligible_dates,
    load_era_rules,
    resolve_start_date,
)
from mbh_simulator import (  # noqa: E402
    OptionQuote,
    read_quotes_csv,
    read_signals_csv,
    simulate_day,
)
from regime_validation import apply_rolling_baseline, discover_dates  # noqa: E402
from index_daily import csv_path_for_symbol, load_index_daily  # noqa: E402

LIVE_DIR = ROOT / "data" / "live"
DEFAULT_PROCESSED = ROOT / "data" / "processed"
NORMALIZED_EQUITY = 13_000_000.0


def settlement_close(day: str) -> Optional[float]:
    """Official SPX closing print for ``day`` from data/calendar/spx_daily.csv.

    This is the cash-settlement reference for 0DTE SPXW spreads (European,
    settled off the index close) — distinct from the executor's own
    in-session mark, which is a live-quote snapshot and can be stale or
    wide right around the close.
    """
    path = csv_path_for_symbol("^GSPC")
    if not path.exists():
        return None
    by_date = load_index_daily(path)
    row = by_date.get(day)
    return row.close if row else None


def settlement_marked_pnl(entries: List[dict], events: List[dict], spot: float) -> float:
    """True EOD P&L: realized closes (stops/flattens) plus settlement value of
    whatever was still open at the close.

    Groups fills by (side, short_strike, long_strike). For each group, nets
    out contracts actually closed early via ``stop`` or ``flatten_fill``
    events against their real fill price (realized, not theoretical), and
    only prices the *remaining* open contracts at 0DTE cash-settlement
    intrinsic value using the official SPX close. Earlier versions of this
    function valued every entry fill at settlement regardless of whether it
    had already been bought back — double-counting any early close.

    ``stop`` events only report the short leg's buyback price; the long leg
    is assumed to ride to expiration with the rest of the group (typical for
    a short-leg-only stop), so its settlement value is still included.
    """
    groups: Dict[tuple, dict] = {}
    for entry in entries:
        key = (entry.get("side"), entry.get("short_strike"), entry.get("long_strike"))
        g = groups.setdefault(key, {"contracts": 0.0, "credit_dollars": 0.0, "closed_contracts": 0.0, "realized_dollars": 0.0})
        contracts = float(entry.get("contracts") or 0.0)
        credit = float(entry.get("credit") or 0.0)
        g["contracts"] += contracts
        g["credit_dollars"] += credit * contracts * 100.0

    for event in events:
        if event.get("event") == "flatten_fill":
            key = (event.get("side"), event.get("short_strike"), event.get("long_strike"))
            g = groups.get(key)
            if g is None:
                continue
            contracts = float(event.get("contracts") or 0.0)
            g["closed_contracts"] += contracts
            g["realized_dollars"] += float(event.get("fill_price") or 0.0) * contracts * 100.0
        elif event.get("event") == "stop":
            key = (event.get("side"), event.get("short_strike"), event.get("long_strike"))
            g = groups.get(key)
            if g is None:
                continue
            contracts = float(event.get("contracts") or 0.0)
            g["closed_contracts"] += contracts
            g["realized_dollars"] -= float(event.get("stop_fill") or 0.0) * contracts * 100.0

    total = 0.0
    for (side, short_strike, long_strike), g in groups.items():
        contracts = g["contracts"] or 1.0
        avg_credit = g["credit_dollars"] / contracts
        closed = min(g["closed_contracts"], g["contracts"])
        remaining = g["contracts"] - closed
        total += avg_credit * closed + g["realized_dollars"]
        if remaining > 0 and short_strike is not None and long_strike is not None:
            short_strike = float(short_strike)
            long_strike = float(long_strike)
            if side == "bear_call":
                value = max(0.0, spot - short_strike) - max(0.0, spot - long_strike)
            elif side == "bull_put":
                value = max(0.0, short_strike - spot) - max(0.0, long_strike - spot)
            else:
                continue
            total += avg_credit * remaining - value * 100.0 * remaining
    return round(total, 2)


def load_live_session(day: str) -> tuple:
    day_dir = LIVE_DIR / day
    config_path = day_dir / "config.json"
    fills_path = day_dir / "fills.jsonl"
    tranches_path = day_dir / "tranches.jsonl"
    if not config_path.exists():
        raise SystemExit(f"no session snapshot at {config_path}")
    snapshot = json.loads(config_path.read_text(encoding="utf-8"))
    events = []
    if fills_path.exists():
        for line in fills_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                events.append(json.loads(line))
    tranches = []
    if tranches_path.exists():
        for line in tranches_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                tranches.append(json.loads(line))
    return snapshot, events, tranches


def summarize_live(events: list) -> dict:
    entries = [e for e in events if e.get("event") == "entry"]
    stops = [e for e in events if e.get("event") == "stop"]
    submitted = [e for e in events if e.get("event") == "entry_submitted"]
    rejected = [e for e in events if e.get("event") == "order_rejected"]
    session_ends = [e for e in events if e.get("event") == "session_end"]
    slippages = [float(e["fill_slippage"]) for e in entries if e.get("fill_slippage") is not None]
    n = len(entries)
    marked = None
    if session_ends and session_ends[-1].get("marked_pnl") is not None:
        marked = float(session_ends[-1]["marked_pnl"])
    return {
        "entries": n,
        "entry_submitted": len(submitted),
        "order_rejected": len(rejected),
        "reject_reasons": dict(Counter(e.get("reason") for e in rejected)),
        "sides": dict(Counter(e.get("side") for e in entries)),
        "bear_call_pct": round(100.0 * sum(1 for e in entries if e.get("side") == "bear_call") / n, 1) if n else 0.0,
        "contracts": sum(int(e.get("contracts", 0)) for e in entries),
        "short_strikes": sorted({e.get("short_strike") for e in entries}),
        "avg_fill_slippage": round(sum(slippages) / len(slippages), 4) if slippages else None,
        "stops": len(stops),
        "flattened": any(e.get("event") == "flatten" for e in events),
        "halted": any(e.get("event") == "halt_entries" for e in events),
        "marked_pnl": marked,
        "gross_credit_sold": (
            float(session_ends[-1]["gross_credit_sold"])
            if session_ends and session_ends[-1].get("gross_credit_sold") is not None
            else None
        ),
    }


def _tranche_signal_diffs(
    signals: list,
    tranches: List[dict],
    executed_by_tranche: Dict[datetime, int],
) -> List[dict]:
    """Compare live rows with the canonical minute-by-minute replay signals."""
    replay_by_minute = {
        signal.timestamp.replace(tzinfo=None, second=0, microsecond=0): signal
        for signal in signals
    }
    diffs: List[dict] = []
    for row in tranches:
        ts_raw = row.get("timestamp") or row.get("entry_time")
        if not ts_raw:
            continue
        ts = datetime.fromisoformat(str(ts_raw).replace("Z", "")).replace(
            second=0, microsecond=0,
        )
        snap = replay_by_minute.get(ts)
        if snap is None:
            continue
        diffs.append(
            {
                "time": ts.strftime("%H:%M"),
                "live_skip": row.get("skip_reason") or ("entry" if row.get("executed") else ""),
                "live_executed": executed_by_tranche.get(ts, row.get("executed", 0)),
                "live_trend_z": round(float(row.get("trend_score", 0.0)), 3),
                "backtest_trend_z": round(snap.trend_score, 3),
                "trend_delta": round(float(row.get("trend_score", 0.0)) - snap.trend_score, 3),
                "live_skew_z": round(float(row.get("skew_z", 0.0)), 3),
                "backtest_skew_z": round(snap.skew_z, 3),
                "skew_delta": round(float(row.get("skew_z", 0.0)) - snap.skew_z, 3),
            }
        )
    return diffs


def _executed_by_tranche(events: List[dict]) -> Dict[datetime, int]:
    """Map asynchronous entry fills back to the tranche that created them."""
    counts: Dict[datetime, int] = {}
    for event in events:
        if event.get("event") != "entry":
            continue
        raw = event.get("tranche_time") or event.get("ts")
        if not raw:
            continue
        ts = datetime.fromisoformat(str(raw).replace("Z", "")).replace(
            second=0, microsecond=0,
        )
        counts[ts] = counts.get(ts, 0) + int(event.get("contracts", 0) or 0)
    return counts


class _AsRunSizePolicy:
    """Apply the baseline that was active at each signal timestamp."""

    def __init__(self, base_policy, changes: List[tuple]) -> None:
        self.base_policy = base_policy
        self.changes = sorted(changes, key=lambda item: item[0])

    def contracts(self, signal, config) -> int:
        baseline = config.baseline_contracts
        if signal is not None:
            for changed_at, value in self.changes:
                if changed_at > signal.timestamp.replace(tzinfo=None):
                    break
                baseline = value
        return self.base_policy.contracts(signal, replace(config, baseline_contracts=baseline))


def _build_sizing_policy(live_cfg, schedule, events: Optional[List[dict]] = None):
    """Match live VIX elevated / skip policy when enabled on the session snapshot."""
    if schedule is None:
        from unconditional_baseline import FixedSizePolicy

        policy = FixedSizePolicy()
    else:
        use_vix = bool(getattr(live_cfg, "use_vix_elevated_sizing", False) or getattr(live_cfg, "use_vix_session_gate", False))
        if use_vix:
            from vix_sizing_policies import VixElevatedSkipPolicy

            policy = VixElevatedSkipPolicy(
                schedule,
                elevated_min=float(getattr(live_cfg, "vix_elevated_min", 25.0)),
                elevated_max=float(getattr(live_cfg, "vix_elevated_max", 35.0)),
                elevated_scale=float(getattr(live_cfg, "vix_elevated_scale", 1.25))
                if getattr(live_cfg, "use_vix_elevated_sizing", False)
                else 1.0,
                skip_above=float(getattr(live_cfg, "vix_skip_open_above", 35.0))
                if getattr(live_cfg, "use_vix_session_gate", False)
                else 1e9,
                max_contracts=int(getattr(live_cfg, "max_contracts_per_tranche", 0) or 0) or None,
            )
        else:
            from time_of_day_sizing_runner import TimeOfDaySizePolicy

            policy = TimeOfDaySizePolicy(schedule)

    changes = []
    for event in events or []:
        if event.get("event") != "session_start" or event.get("baseline_contracts") is None:
            continue
        raw = event.get("ts")
        if raw:
            changes.append((datetime.fromisoformat(str(raw).replace("Z", "")), int(event["baseline_contracts"])))
    return _AsRunSizePolicy(policy, changes) if changes else policy


def resolve_replay_config(
    snapshot: dict,
    equity_override: Optional[float] = None,
):
    """Resolve a snapshot with the same live-only safety overlays as execution."""
    from live_config import LiveConfig
    from live_entry_risk import apply_live_risk_overlays
    from strategy_profiles import resolve_strategy_config

    saved_live = dict(snapshot.get("live_config") or {})
    live = LiveConfig(**saved_live)
    # Older snapshots predate the nearby-strike cluster cap. Treat absence as
    # disabled so a forensic replay does not retroactively apply a new safety.
    if "max_open_side_cluster" not in saved_live:
        live.max_open_side_cluster = 0
    if "side_cluster_points" not in saved_live:
        live.side_cluster_points = 0.0
    if equity_override is not None:
        live.account_equity = float(equity_override)
        live.contracts_per_tranche = 0

    config, schedule = resolve_strategy_config(live)
    config = apply_live_risk_overlays(config, live)
    if equity_override is not None:
        # The normalized replay answers the strategy-scale question; retain
        # stop/condor overlays but do not constrain it by a pilot-account lot cap.
        config = replace(
            config,
            max_open_contracts=0,
            max_open_contracts_per_side=0,
            max_open_contracts_same_strike=0,
            max_open_contracts_side_cluster=0,
            open_contract_side_cluster_points=0.0,
        )
    return live, config, schedule


def _summarize_backtest(result, *, available: bool = True, reason: str = "") -> dict:
    if not available:
        return {"available": False, "reason": reason}
    trades = result.trades
    n = len(trades)
    return {
        "available": True,
        "entries": n,
        "sides": dict(Counter(t.side for t in trades)),
        "bear_call_pct": round(100.0 * sum(1 for t in trades if t.side == "bear_call") / n, 1) if n else 0.0,
        "contracts": sum(int(t.contracts) for t in trades),
        "short_strikes": sorted({t.short_strike for t in trades}),
        "stops": sum(1 for t in trades if t.stopped),
        "net_pnl": round(result.net_pnl, 2),
        "halted": result.halted,
    }


def replay_backtest(
    day: str,
    snapshot: dict,
    processed_dir: Path,
    train_count: int,
    *,
    equity_override: Optional[float] = None,
    events: Optional[List[dict]] = None,
) -> dict:
    live, config, schedule = resolve_replay_config(snapshot, equity_override)
    floor, eras = load_era_rules(DEFAULT_RULES)
    processed_dates = discover_dates(processed_dir, "SPXW")
    resolved_start = resolve_start_date(processed_dates, floor, require_mon_and_wed=True)
    eligible = discover_eligible_dates(
        processed_dates,
        floor=resolved_start,
        end=processed_dates[-1],
        eras=eras,
    )
    if day not in eligible:
        return {"available": False, "reason": f"{day} not in eligible calendar"}
    idx = eligible.index(day)
    if idx < train_count:
        return {"available": False, "reason": f"need {train_count} prior eligible days; have {idx}"}

    train_dates = eligible[idx - train_count : idx]
    apply_rolling_baseline(processed_dir, "SPXW", train_dates, day, "signals_unconditional.csv")
    day_dir = processed_dir / "symbol=SPXW" / f"date={day}"
    quotes = read_quotes_csv(day_dir / "normalized_option_quotes.csv")
    signals = read_signals_csv(day_dir / "signals_unconditional.csv")
    policy = _build_sizing_policy(
        live,
        schedule,
        events if equity_override is None else None,
    )

    result = simulate_day(
        quotes,
        signals,
        config=config,
        policy=policy,
    )
    summary = _summarize_backtest(result)
    summary["account_equity"] = config.account_equity
    summary["baseline_contracts"] = config.baseline_contracts
    summary["train_dates"] = train_dates
    summary["quotes"] = quotes
    summary["signals"] = signals
    return summary


def _diff_block(live_summary: dict, bt: dict) -> dict:
    if not bt.get("available"):
        return {"available": False, "reason": bt.get("reason")}
    return {
        "available": True,
        "entries": live_summary["entries"] - bt["entries"],
        "contracts": live_summary["contracts"] - bt["contracts"],
        "bear_call_pct": round(live_summary["bear_call_pct"] - bt["bear_call_pct"], 1),
        "stops": live_summary["stops"] - bt["stops"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconcile a live/paper session vs backtest.")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--processed-dir", default=str(DEFAULT_PROCESSED))
    parser.add_argument("--train-count", type=int, default=40)
    parser.add_argument(
        "--normalized-equity",
        type=float,
        default=NORMALIZED_EQUITY,
        help="Second replay equity for dashboard-scale trade-count comparison.",
    )
    args = parser.parse_args()

    snapshot, events, tranches = load_live_session(args.date)
    live_summary = summarize_live(events)
    processed = Path(args.processed_dir)

    spot = settlement_close(args.date)
    live_summary["settlement_price"] = spot
    if spot is not None:
        entries = [e for e in events if e.get("event") == "entry"]
        theo = settlement_marked_pnl(entries, events, spot)
        live_summary["settlement_marked_pnl"] = theo
        if live_summary.get("marked_pnl") is not None:
            live_summary["marked_pnl_vs_settlement"] = round(theo - live_summary["marked_pnl"], 2)
    else:
        live_summary["settlement_marked_pnl"] = None
        live_summary["marked_pnl_vs_settlement"] = None

    paper_bt = replay_backtest(
        args.date, snapshot, processed, args.train_count, events=events,
    )
    quotes = paper_bt.pop("quotes", None) if paper_bt.get("available") else None
    signals = paper_bt.pop("signals", None) if paper_bt.get("available") else None
    train_dates = paper_bt.pop("train_dates", []) if paper_bt.get("available") else []

    # Normalized $13M replay (skip if paper equity already ~13M)
    paper_eq = float(snapshot.get("live_config", {}).get("account_equity") or 0.0)
    if abs(paper_eq - args.normalized_equity) < 1.0:
        norm_bt = dict(paper_bt)
        norm_bt["note"] = "paper equity already at normalized scale"
    else:
        norm_bt = replay_backtest(
            args.date,
            snapshot,
            processed,
            args.train_count,
            equity_override=args.normalized_equity,
        )
        norm_bt.pop("quotes", None)
        norm_bt.pop("signals", None)
        norm_bt.pop("train_dates", None)

    signal_diffs: List[dict] = []
    if signals is not None and tranches:
        signal_diffs = _tranche_signal_diffs(
            signals,
            tranches,
            _executed_by_tranche(events),
        )

    report = {
        "date": args.date,
        "profile": snapshot.get("live_config", {}).get("profile"),
        "sizing_scheme": snapshot.get("sizing_scheme"),
        "mode": snapshot.get("live_config", {}).get("mode"),
        "live": live_summary,
        "backtest_paper_scale": paper_bt,
        "backtest_normalized_13m": norm_bt,
        # Backward-compatible alias for older consumers
        "backtest": paper_bt,
        "diff_paper_scale": _diff_block(live_summary, paper_bt),
        "diff_normalized_13m": _diff_block(live_summary, norm_bt),
        "tranche_signals": signal_diffs[:50],
    }
    out_path = LIVE_DIR / args.date / "reconcile.json"
    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    print(f"=== Reconcile {args.date} (profile={report['profile']}, mode={report['mode']}) ===")
    print(
        f"LIVE     : entries={live_summary['entries']} contracts={live_summary['contracts']} "
        f"stops={live_summary['stops']} bear_call={live_summary['bear_call_pct']}% "
        f"sides={live_summary['sides']} marked_pnl={live_summary.get('marked_pnl')}"
    )
    if live_summary.get("settlement_price") is not None:
        print(
            f"SETTLE   : spx_close={live_summary['settlement_price']} "
            f"settlement_marked_pnl={live_summary.get('settlement_marked_pnl')} "
            f"vs_reported={live_summary.get('marked_pnl_vs_settlement')}"
        )
    for label, bt, diff in (
        ("PAPER $", paper_bt, report["diff_paper_scale"]),
        ("NORM $13M", norm_bt, report["diff_normalized_13m"]),
    ):
        if bt.get("available"):
            print(
                f"{label:9}: entries={bt['entries']} contracts={bt['contracts']} "
                f"stops={bt['stops']} bear_call={bt['bear_call_pct']}% "
                f"sides={bt['sides']} net_pnl=${bt['net_pnl']:,.0f} "
                f"equity=${bt.get('account_equity', 0):,.0f}"
            )
            if diff.get("available"):
                print(
                    f"  DIFF   : entries={diff['entries']:+d} contracts={diff['contracts']:+d} "
                    f"bear_call_pct={diff['bear_call_pct']:+.1f}pp stops={diff['stops']:+d}"
                )
        else:
            print(f"{label:9}: unavailable — {bt.get('reason')}")
    if signal_diffs:
        print(f"TRANCHES : {len(signal_diffs)} live tranche rows with replayed trend/skew z-scores")
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
