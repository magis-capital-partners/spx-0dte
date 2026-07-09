"""Daily VIX history from free public sources (Yahoo Finance ^VIX).

Stored at data/calendar/vix_daily.csv (tracked in git). Used to populate the
``vix`` column in processed signals.csv for regime analysis and sizing tests.

We use same-day open as the decision-time VIX proxy: constant across intraday
rows on a trading day, aligned with entries from the open onward.
"""
from __future__ import annotations

import csv
import json
import math
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VIX_CSV = ROOT / "data" / "calendar" / "vix_daily.csv"

YAHOO_CHART_URL = "https://query2.finance.yahoo.com/v8/finance/chart/%5EVIX"
USER_AGENT = "spx-0dte/1.0 (VIX daily backfill; research use)"


@dataclass(frozen=True)
class VixDay:
    trade_date: str
    open: float
    high: float
    low: float
    close: float
    prior_close: Optional[float] = None

    @property
    def decision_vix(self) -> float:
        """VIX level used for intraday signal rows (same-day open)."""
        return self.open


def _parse_date(value: str) -> str:
    text = str(value).strip()[:10]
    datetime.strptime(text, "%Y-%m-%d")
    return text


def _safe_float(value: object) -> Optional[float]:
    if value in {"", None}:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _unix_range(start: str, end: str) -> Tuple[int, int]:
    start_dt = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(end, "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
    return int(start_dt.timestamp()), int(end_dt.timestamp())


def fetch_vix_daily_yahoo(start_date: str, end_date: str) -> List[VixDay]:
    """Download daily OHLC for ^VIX from Yahoo Finance chart API."""
    period1, period2 = _unix_range(start_date, end_date)
    url = f"{YAHOO_CHART_URL}?interval=1d&period1={period1}&period2={period2}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))

    chart = payload.get("chart") or {}
    results = chart.get("result") or []
    if not results:
        raise RuntimeError(f"Yahoo chart API returned no data for {start_date}..{end_date}")

    result = results[0]
    timestamps = result.get("timestamp") or []
    quote = (result.get("indicators") or {}).get("quote") or [{}]
    quote = quote[0] if quote else {}
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []

    rows: List[VixDay] = []
    for index, ts in enumerate(timestamps):
        open_px = _safe_float(opens[index] if index < len(opens) else None)
        high_px = _safe_float(highs[index] if index < len(highs) else None)
        low_px = _safe_float(lows[index] if index < len(lows) else None)
        close_px = _safe_float(closes[index] if index < len(closes) else None)
        if open_px is None or close_px is None:
            continue
        trade_date = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
        rows.append(
            VixDay(
                trade_date=trade_date,
                open=open_px,
                high=high_px if high_px is not None else open_px,
                low=low_px if low_px is not None else open_px,
                close=close_px,
            )
        )

    rows.sort(key=lambda row: row.trade_date)
    prior_close: Optional[float] = None
    enriched: List[VixDay] = []
    for row in rows:
        enriched.append(
            VixDay(
                trade_date=row.trade_date,
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                prior_close=prior_close,
            )
        )
        prior_close = row.close
    return enriched


def write_vix_csv(path: Path, rows: Iterable[VixDay]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(rows, key=lambda row: row.trade_date)
    fieldnames = ["date", "open", "high", "low", "close", "prior_close"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in ordered:
            writer.writerow(
                {
                    "date": row.trade_date,
                    "open": f"{row.open:.4f}",
                    "high": f"{row.high:.4f}",
                    "low": f"{row.low:.4f}",
                    "close": f"{row.close:.4f}",
                    "prior_close": "" if row.prior_close is None else f"{row.prior_close:.4f}",
                }
            )
    return len(ordered)


def load_vix_daily(path: Path = DEFAULT_VIX_CSV) -> Dict[str, VixDay]:
    if not path.exists():
        return {}
    rows: Dict[str, VixDay] = {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for raw in csv.DictReader(handle):
            trade_date = _parse_date(raw["date"])
            open_px = _safe_float(raw.get("open"))
            close_px = _safe_float(raw.get("close"))
            if open_px is None or close_px is None:
                continue
            high_px = _safe_float(raw.get("high")) or open_px
            low_px = _safe_float(raw.get("low")) or open_px
            prior = _safe_float(raw.get("prior_close"))
            rows[trade_date] = VixDay(
                trade_date=trade_date,
                open=open_px,
                high=high_px,
                low=low_px,
                close=close_px,
                prior_close=prior,
            )
    return rows


def merge_vix_rows(existing: Dict[str, VixDay], fresh: Iterable[VixDay]) -> List[VixDay]:
    merged = dict(existing)
    for row in fresh:
        merged[row.trade_date] = row
    ordered = sorted(merged.values(), key=lambda row: row.trade_date)
    prior_close: Optional[float] = None
    rebuilt: List[VixDay] = []
    for row in ordered:
        rebuilt.append(
            VixDay(
                trade_date=row.trade_date,
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                prior_close=prior_close,
            )
        )
        prior_close = row.close
    return rebuilt


def download_and_save(
    *,
    start_date: str,
    end_date: str,
    path: Path = DEFAULT_VIX_CSV,
) -> Tuple[int, str, str]:
    existing = load_vix_daily(path)
    fresh = fetch_vix_daily_yahoo(start_date, end_date)
    if not fresh:
        raise RuntimeError(f"No VIX rows downloaded for {start_date}..{end_date}")
    merged = merge_vix_rows(existing, fresh)
    count = write_vix_csv(path, merged)
    return count, merged[0].trade_date, merged[-1].trade_date


def vix_for_date(vix_by_date: Dict[str, VixDay], trade_date: str) -> Optional[VixDay]:
    return vix_by_date.get(trade_date)


def summarize_coverage(vix_by_date: Dict[str, VixDay], trade_dates: Iterable[str]) -> dict:
    dates = sorted(trade_dates)
    if not dates:
        return {"requested": 0, "covered": 0, "missing": [], "coverage_pct": 0.0}
    missing = [d for d in dates if d not in vix_by_date]
    covered = len(dates) - len(missing)
    return {
        "requested": len(dates),
        "covered": covered,
        "missing_count": len(missing),
        "missing_sample": missing[:20],
        "coverage_pct": round(covered / len(dates) * 100.0, 2),
        "first_requested": dates[0],
        "last_requested": dates[-1],
        "first_vix": min(vix_by_date) if vix_by_date else "",
        "last_vix": max(vix_by_date) if vix_by_date else "",
    }


def regime_bucket(vix: float) -> str:
    if vix < 12.0:
        return "ultra_low_lt12"
    if vix < 15.0:
        return "low_12_15"
    if vix < 17.0:
        return "low_mid_15_17"
    if vix <= 25.0:
        return "optimal_17_25"
    if vix <= 35.0:
        return "elevated_25_35"
    return "extreme_gt35"
