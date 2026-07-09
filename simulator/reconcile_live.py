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
from live_features import (  # noqa: E402
    SessionFeatureState,
    compute_raw_features,
    raw_to_signal_snapshot,
    split_session_quotes,
)
from mbh_simulator import (  # noqa: E402
    OptionQuote,
    read_quotes_csv,
    read_signals_csv,
    simulate_day,
)
from historical_baselines import compute_baselines  # noqa: E402
from regime_validation import apply_rolling_baseline, discover_dates  # noqa: E402

LIVE_DIR = ROOT / "data" / "live"
DEFAULT_PROCESSED = ROOT / "data" / "processed"
NORMALIZED_EQUITY = 13_000_000.0


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


def _quotes_at_time(quotes: List[OptionQuote], ts: datetime) -> List[OptionQuote]:
    key = ts.replace(tzinfo=None).isoformat(timespec="seconds")
    return [q for q in quotes if q.timestamp.replace(tzinfo=None).isoformat(timespec="seconds") == key]


def _tranche_signal_diffs(
    day: str,
    quotes: List[OptionQuote],
    train_dates: List[str],
    processed_dir: Path,
    tranches: List[dict],
) -> List[dict]:
    """Compare live tranche skip reasons vs replayed signals (when tranches.jsonl exists)."""
    baselines = compute_baselines(processed_dir, "SPXW", train_dates)
    state = SessionFeatureState()
    diffs: List[dict] = []
    for row in tranches:
        ts_raw = row.get("timestamp") or row.get("entry_time")
        if not ts_raw:
            continue
        ts = datetime.fromisoformat(str(ts_raw).replace("Z", ""))
        bucket = _quotes_at_time(quotes, ts)
        if not bucket:
            continue
        zero_q, next_q = split_session_quotes(bucket, day)
        if not zero_q:
            continue
        spot_vals = [q.underlying_price for q in zero_q if q.underlying_price]
        if not spot_vals:
            continue
        next_bucket = None
        if next_q:
            next_by_ts: Dict[str, list] = {}
            for q in next_q:
                k = q.timestamp.replace(tzinfo=None).isoformat(timespec="seconds")
                next_by_ts.setdefault(k, []).append(q)
            ts_key = ts.replace(tzinfo=None).isoformat(timespec="seconds")
            next_bucket = next_by_ts.get(ts_key)

        raw = compute_raw_features(zero_q, float(spot_vals[0]), ts, state, next_expiry_quotes=next_bucket)
        snap = raw_to_signal_snapshot(raw, baselines, ts)
        diffs.append(
            {
                "time": ts.strftime("%H:%M"),
                "live_skip": row.get("skip_reason") or ("entry" if row.get("executed") else ""),
                "live_executed": row.get("executed", 0),
                "trend_z": round(snap.trend_score, 3),
                "skew_z": round(snap.skew_z, 3),
            }
        )
    return diffs


def _build_sizing_policy(live_cfg, schedule):
    """Match live VIX elevated / skip policy when enabled on the session snapshot."""
    if schedule is None:
        from unconditional_baseline import FixedSizePolicy

        return FixedSizePolicy()

    use_vix = bool(getattr(live_cfg, "use_vix_elevated_sizing", False) or getattr(live_cfg, "use_vix_session_gate", False))
    if use_vix:
        from vix_sizing_policies import VixElevatedSkipPolicy

        return VixElevatedSkipPolicy(
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

    from time_of_day_sizing_runner import TimeOfDaySizePolicy

    return TimeOfDaySizePolicy(schedule)


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
) -> dict:
    from live_config import LiveConfig
    from strategy_profiles import resolve_strategy_config

    live = LiveConfig(**snapshot["live_config"])
    if equity_override is not None:
        live.account_equity = float(equity_override)
        # Clear paper lot override so resolve_strategy_config scales from equity.
        live.contracts_per_tranche = 0

    config, schedule = resolve_strategy_config(live)
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
    policy = _build_sizing_policy(live, schedule)

    result = simulate_day(
        quotes,
        read_signals_csv(day_dir / "signals_unconditional.csv"),
        config=config,
        policy=policy,
    )
    summary = _summarize_backtest(result)
    summary["account_equity"] = config.account_equity
    summary["baseline_contracts"] = config.baseline_contracts
    summary["train_dates"] = train_dates
    summary["quotes"] = quotes
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

    paper_bt = replay_backtest(args.date, snapshot, processed, args.train_count)
    quotes = paper_bt.pop("quotes", None) if paper_bt.get("available") else None
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
        norm_bt.pop("train_dates", None)

    signal_diffs: List[dict] = []
    if quotes is not None and tranches:
        signal_diffs = _tranche_signal_diffs(
            args.date,
            quotes,
            train_dates,
            processed,
            tranches,
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
