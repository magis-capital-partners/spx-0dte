"""Data-source tagging for processed days and homogeneous baseline windows.

Two IV engines feed this repo: ThetaData model IV (vendor-built days) and IB
``modelGreeks`` IV (days rebuilt from live chain recordings after the vendor
subscription ended, tagged by a ``source.json`` marker). The two disagree by
a systematic 0.15-0.5 z on skew_z — mixing them inside one 40-day baseline
window corrupts every z-score computed across the seam (the exact bug class
behind the Aug 2026 bear_call side-selection investigation).

``resolve_homogeneous_train_dates`` therefore never returns a mixed window:

- Enough same-source days as the most recent data -> normal rolling window.
- Not yet (transition period)                     -> the last full window of
  the PRIOR source, frozen, with a loud cutover-countdown note. This is what
  live already z-scores against, so reconcile replays mirror live exactly.
- Neither source has a full window               -> hard failure.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import List, Sequence, Tuple

VENDOR_SOURCE = "thetadata"
IB_SOURCE = "ib_live"


@lru_cache(maxsize=None)
def _day_source_cached(marker_str: str) -> str:
    marker = Path(marker_str)
    if not marker.is_file():
        return VENDOR_SOURCE
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return VENDOR_SOURCE
    return str(payload.get("source") or VENDOR_SOURCE)


def day_source(processed_dir: Path, symbol: str, trade_date: str) -> str:
    """Source tag for one processed day; untagged days are vendor-built.

    Memoized (markers are immutable within a run): rolling-window exports call
    this O(days^2) times across a 1,600-day history.
    """
    marker = Path(processed_dir) / f"symbol={symbol}" / f"date={trade_date}" / "source.json"
    return _day_source_cached(str(marker))


def resolve_homogeneous_train_dates(
    processed_dir: Path,
    symbol: str,
    eligible_prior: Sequence[str],
    train_count: int,
) -> Tuple[List[str], str, str]:
    """Pick a single-source training window from dates strictly before as-of.

    Returns (train_dates, source, note). ``note`` is non-empty during the
    transition (frozen prior-source window) and should be surfaced loudly.
    Raises SystemExit when no source can fill a window.
    """
    if len(eligible_prior) < train_count:
        raise SystemExit(
            f"Need at least {train_count} eligible dates for baselines; "
            f"have {len(eligible_prior)}"
        )
    sources = {d: day_source(processed_dir, symbol, d) for d in eligible_prior}
    latest_source = sources[eligible_prior[-1]]
    same = [d for d in eligible_prior if sources[d] == latest_source]
    if len(same) >= train_count:
        return same[-train_count:], latest_source, ""

    other = [d for d in eligible_prior if sources[d] != latest_source]
    other_source = sources[other[-1]] if other else ""
    if len(other) >= train_count:
        note = (
            f"SOURCE CUTOVER PENDING: only {len(same)}/{train_count} "
            f"{latest_source} days recorded; using frozen {other_source} "
            f"window ending {other[-1]}. Auto-cutover after "
            f"{train_count - len(same)} more {latest_source} sessions."
        )
        return other[-train_count:], other_source, note

    raise SystemExit(
        f"No single data source has {train_count} eligible days "
        f"({latest_source}: {len(same)}, {other_source or 'other'}: {len(other)}); "
        "cannot build an unmixed baseline window"
    )
