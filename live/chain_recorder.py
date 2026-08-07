"""Per-minute recording of the IB option chain the live signal actually saw.

Motivation (2026-08-08): the ThetaData subscription is ending. The backtest,
rolling baselines, and reconcile replay all consume vendor-built processed
days; once the vendor stops, the only sustainable source is the chain the
executor already streams from IB. This module records the *sampler-aggregated*
minute sample — the exact quotes `compute_raw_features_once_per_minute`
consumes — so `scripts/build_processed_from_ib.py` can rebuild a processed
day by replaying the identical live code path. Baselines built from these
recordings match live behaviour by construction, which permanently removes
the vendor-vs-IB IV parity problem for the features.

Safety: this runs inside the live executor. Every public method swallows its
own exceptions (with a once-per-session stderr note) — recording must never
be able to take down or delay a trading loop.

Format: JSONL, one row per canonical minute (append-only, crash-safe):

    {"minute": "2026-08-10T09:31", "spot": 7712.4, "obs": 5,
     "quotes": [[expiry, type, strike, bid, ask, delta_or_null, iv_or_null], ...]}

When the tranche poll fetches next-expiry quotes for the same minute, an
*upgrade row* is appended with the additional key ``next_quotes``. The
converter groups rows by minute and prefers the row carrying next_quotes.
A mid-session restart simply resumes appending; duplicate minutes are
resolved at conversion time.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional, Sequence


def _quote_row(quote) -> list:
    return [
        str(quote.expiry),
        str(quote.option_type).upper(),
        float(quote.strike),
        float(quote.bid),
        float(quote.ask),
        None if quote.delta is None else float(quote.delta),
        None if quote.iv is None else float(quote.iv),
    ]


class ChainMinuteRecorder:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._last_minute: Optional[str] = None
        self._last_minute_had_next: bool = False
        self._failed_once = False

    # ------------------------------------------------------------------ #
    def record_sample(self, sample) -> None:
        """Record the canonical 0DTE minute sample (once per minute)."""
        try:
            minute = sample.timestamp.isoformat(timespec="minutes")
            if minute == self._last_minute:
                return
            row = {
                "minute": minute,
                "spot": float(sample.spot),
                "obs": int(getattr(sample, "observation_count", 0) or 0),
                "quotes": [_quote_row(q) for q in sample.quotes],
            }
            self._append(row)
            self._last_minute = minute
            self._last_minute_had_next = False
        except Exception as exc:  # noqa: BLE001 — never disturb the trading loop
            self._note_failure(exc)

    def record_next_expiry(self, sample, next_quotes: Sequence) -> None:
        """Append an upgrade row carrying the tranche-time next-expiry quotes."""
        try:
            if not next_quotes:
                return
            minute = sample.timestamp.isoformat(timespec="minutes")
            if minute == self._last_minute and self._last_minute_had_next:
                return
            row = {
                "minute": minute,
                "spot": float(sample.spot),
                "obs": int(getattr(sample, "observation_count", 0) or 0),
                "quotes": [_quote_row(q) for q in sample.quotes],
                "next_quotes": [_quote_row(q) for q in next_quotes],
            }
            self._append(row)
            self._last_minute = minute
            self._last_minute_had_next = True
        except Exception as exc:  # noqa: BLE001
            self._note_failure(exc)

    # ------------------------------------------------------------------ #
    def _append(self, row: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, separators=(",", ":")) + "\n")

    def _note_failure(self, exc: Exception) -> None:
        if not self._failed_once:
            self._failed_once = True
            print(
                f"[chain-recorder] disabled-for-error: {exc!r} "
                f"(path={self.path}) — trading unaffected",
                file=sys.stderr,
            )


def load_chain_minutes(path: Path) -> dict:
    """Read a recording back as {minute: merged_row}, resolving duplicates.

    Later rows win; a row with next_quotes is preferred over one without so a
    restart or upgrade row never loses the tranche-time term-structure data.
    """
    merged: dict = {}
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue  # torn final line from a crash — ignore
            minute = row.get("minute")
            if not minute or not row.get("quotes"):
                continue
            prev = merged.get(minute)
            if prev is not None and prev.get("next_quotes") and not row.get("next_quotes"):
                continue
            merged[minute] = row
    return merged
