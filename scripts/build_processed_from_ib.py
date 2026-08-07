"""Rebuild a processed day from the executor's recorded IB chain.

Post-ThetaData data path: the live executor records its sampler-aggregated
minute chain to ``data/live/<date>/chain_minutes.jsonl`` (live/chain_recorder.py).
This script replays those minutes through the EXACT live feature code
(``SessionFeatureState`` + ``compute_raw_features_once_per_minute``) and emits
a vendor-schema processed day:

    data/processed/symbol=SPXW/date=<d>/signals.csv
    data/processed/symbol=SPXW/date=<d>/normalized_option_quotes.csv
    data/processed/symbol=SPXW/date=<d>/source.json   {"source": "ib_live"}

so ``refresh_live_baselines.py``, ``reconcile_live.py`` and the backtest keep
working unchanged. Because signals come from the live code path, baselines
built from these days match live behaviour by construction — no vendor-vs-IB
IV parity gap is possible.

The ``source.json`` marker is load-bearing: baseline windows must never mix
vendor and IB days (systematic IV offset between the two IV engines was
measured at 0.15-0.5 z). See simulator/data_sources.py.

Usage:
    python scripts/build_processed_from_ib.py --date 2026-08-10
    python scripts/build_processed_from_ib.py --auto      # all missing days
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))
sys.path.insert(0, str(ROOT / "live"))

from chain_recorder import load_chain_minutes  # noqa: E402
import feature_enricher  # noqa: E402
import vix_signal_enricher  # noqa: E402
from vix_daily import DEFAULT_VIX_CSV, load_vix_daily  # noqa: E402
from live_features import (  # noqa: E402
    SessionFeatureState,
    _atm_straddle_from_quotes,
    compute_raw_features_once_per_minute,
    minutes_to_close,
)
from mbh_simulator import OptionQuote  # noqa: E402

DEFAULT_LIVE = ROOT / "data" / "live"
DEFAULT_PROCESSED = ROOT / "data" / "processed"
IB_SOURCE = "ib_live"

SIGNAL_BASE_COLUMNS = [
    "timestamp", "straddle", "linear_decay_baseline",
    "straddle_residual_z", "skew_z", "term_ratio_z", "trend_score",
    "realized_vs_implied_z", "vix", "underlying_price",
    "atm_call_strike", "atm_put_strike",
]
QUOTE_COLUMNS = [
    "timestamp", "expiry", "option_type", "strike",
    "bid", "ask", "delta", "iv", "underlying_price",
]


def _quotes_from_rows(rows: list, ts: datetime, spot: float) -> List[OptionQuote]:
    out: List[OptionQuote] = []
    for expiry, opt_type, strike, bid, ask, delta, iv in rows:
        out.append(OptionQuote(
            timestamp=ts,
            expiry=str(expiry),
            option_type=str(opt_type),
            strike=float(strike),
            bid=float(bid),
            ask=float(ask),
            delta=None if delta is None else float(delta),
            iv=None if iv is None else float(iv),
            underlying_price=spot,
        ))
    return out


def convert_day(
    day: str,
    *,
    live_dir: Path,
    processed_dir: Path,
    symbol: str,
    force: bool = False,
) -> bool:
    recording = live_dir / day / "chain_minutes.jsonl"
    if not recording.is_file():
        raise FileNotFoundError(f"no chain recording: {recording}")

    day_dir = processed_dir / f"symbol={symbol}" / f"date={day}"
    marker = day_dir / "source.json"
    if (day_dir / "signals.csv").exists() and not marker.exists() and not force:
        # A vendor-built day must never be silently replaced by an IB rebuild.
        raise SystemExit(
            f"{day} already has vendor-built processed data; refusing to "
            "overwrite without --force"
        )

    merged = load_chain_minutes(recording)
    if not merged:
        raise SystemExit(f"{recording} contains no usable minutes")

    state = SessionFeatureState()
    signal_rows: List[dict] = []
    quote_rows: List[dict] = []

    for minute in sorted(merged):
        row = merged[minute]
        ts = datetime.fromisoformat(minute)
        spot = float(row["spot"])
        quotes = _quotes_from_rows(row["quotes"], ts, spot)
        next_quotes = (
            _quotes_from_rows(row["next_quotes"], ts, spot)
            if row.get("next_quotes")
            else None
        )
        raw = compute_raw_features_once_per_minute(
            quotes, spot, ts, state, next_expiry_quotes=next_quotes,
        )
        straddle, atm_call, atm_put = _atm_straddle_from_quotes(quotes, spot)
        baseline = ""
        straddle_out = ""
        if state.first_straddle is not None and straddle == straddle:  # not NaN
            straddle_out = round(straddle, 4)
            mins = minutes_to_close(ts)
            baseline = state.first_straddle
            if state.first_minutes and state.first_minutes > 0:
                baseline = state.first_straddle * (mins / state.first_minutes)
            baseline = round(baseline, 4)
        signal_rows.append({
            "timestamp": ts.isoformat(timespec="seconds"),
            "straddle": straddle_out,
            "linear_decay_baseline": baseline,
            "straddle_residual_z": raw["straddle_residual_z"],
            "skew_z": raw["skew_z"],
            "term_ratio_z": raw["term_ratio_z"],
            "trend_score": raw["trend_score"],
            "realized_vs_implied_z": raw["realized_vs_implied_z"],
            "vix": "",  # filled by vix_signal_enricher below
            "underlying_price": spot,
            "atm_call_strike": atm_call.strike if atm_call else "",
            "atm_put_strike": atm_put.strike if atm_put else "",
        })
        for quote in quotes + (next_quotes or []):
            quote_rows.append({
                "timestamp": ts.isoformat(timespec="seconds"),
                "expiry": quote.expiry,
                "option_type": quote.option_type,
                "strike": quote.strike,
                "bid": quote.bid,
                "ask": quote.ask,
                "delta": "" if quote.delta is None else quote.delta,
                "iv": "" if quote.iv is None else quote.iv,
                "underlying_price": spot,
            })

    day_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(day_dir / "signals.csv", SIGNAL_BASE_COLUMNS, signal_rows)
    _write_csv(day_dir / "normalized_option_quotes.csv", QUOTE_COLUMNS, quote_rows)

    # Same enrichment stages the vendor pipeline runs (adds
    # minutes_to_close_norm / abs_* / overnight gap + prior-day features, then
    # fills vix columns from the free Yahoo calendar).
    feature_enricher.enrich_symbol(processed_dir, symbol, dates=[day])
    vix_signal_enricher.enrich_symbol(
        processed_dir, symbol, load_vix_daily(DEFAULT_VIX_CSV), dates=[day],
    )

    marker.write_text(json.dumps({
        "source": IB_SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "recording": str(recording),
        "minutes": len(signal_rows),
        "quote_rows": len(quote_rows),
    }, indent=2), encoding="utf-8")
    return True


def _write_csv(path: Path, columns: List[str], rows: List[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def discover_auto_days(live_dir: Path, processed_dir: Path, symbol: str) -> List[str]:
    """Recorded sessions with no processed day yet (never touches vendor days)."""
    days: List[str] = []
    if not live_dir.is_dir():
        return days
    for session in sorted(live_dir.iterdir()):
        if not session.is_dir() or not (session / "chain_minutes.jsonl").is_file():
            continue
        day_dir = processed_dir / f"symbol={symbol}" / f"date={session.name}"
        if not (day_dir / "signals.csv").exists():
            days.append(session.name)
    return days


def main() -> None:
    parser = argparse.ArgumentParser(description="Build processed days from IB chain recordings.")
    parser.add_argument("--date", action="append", default=[], help="Session date (repeatable).")
    parser.add_argument("--auto", action="store_true",
                        help="Convert every recorded session that lacks a processed day.")
    parser.add_argument("--force", action="store_true",
                        help="Allow overwriting an existing vendor-built day.")
    parser.add_argument("--live-dir", default=str(DEFAULT_LIVE))
    parser.add_argument("--processed-dir", default=str(DEFAULT_PROCESSED))
    parser.add_argument("--symbol", default="SPXW")
    args = parser.parse_args()

    live_dir = Path(args.live_dir)
    processed_dir = Path(args.processed_dir)
    days = list(args.date)
    if args.auto:
        days += discover_auto_days(live_dir, processed_dir, args.symbol)
    if not days:
        print("No recorded sessions to convert.")
        return

    failures = 0
    for day in sorted(set(days)):
        try:
            convert_day(
                day,
                live_dir=live_dir,
                processed_dir=processed_dir,
                symbol=args.symbol,
                force=args.force,
            )
            print(f"converted {day} -> processed (source={IB_SOURCE})")
        except FileNotFoundError as exc:
            print(f"skip {day}: {exc}")
        except SystemExit as exc:
            print(f"ERROR {day}: {exc}")
            failures += 1
    if failures:
        raise SystemExit(f"{failures} day(s) failed to convert")


if __name__ == "__main__":
    main()
