"""Daily equity-index OHLC from Yahoo Finance chart API.

Stores calendars under data/calendar/ for market-factor analysis:
  ^GSPC → spx_daily.csv
  ^IXIC → ixic_daily.csv
  ^RUT  → rut_daily.csv

Same fetch/merge pattern as vix_daily.py.
"""
from __future__ import annotations

import csv
import json
import math
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
CALENDAR_DIR = ROOT / "data" / "calendar"

USER_AGENT = "spx-0dte/1.0 (index daily backfill; research use)"

# Yahoo symbol → (csv stem, friendly label)
INDEX_SPECS: Dict[str, Tuple[str, str]] = {
    "^GSPC": ("spx", "S&P 500"),
    "^IXIC": ("ixic", "NASDAQ Composite"),
    "^RUT": ("rut", "Russell 2000"),
}

DEFAULT_SYMBOLS: Tuple[str, ...] = ("^GSPC", "^IXIC", "^RUT")


@dataclass(frozen=True)
class IndexDay:
    trade_date: str
    open: float
    high: float
    low: float
    close: float
    prior_close: Optional[float] = None

    @property
    def simple_return(self) -> Optional[float]:
        if self.prior_close is None or self.prior_close == 0:
            return None
        return self.close / self.prior_close - 1.0


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


def csv_path_for_symbol(symbol: str, calendar_dir: Path = CALENDAR_DIR) -> Path:
    if symbol not in INDEX_SPECS:
        raise KeyError(f"unsupported index symbol {symbol!r}; known={sorted(INDEX_SPECS)}")
    stem, _ = INDEX_SPECS[symbol]
    return calendar_dir / f"{stem}_daily.csv"


def fetch_index_daily_yahoo(symbol: str, start_date: str, end_date: str) -> List[IndexDay]:
    """Download daily OHLC for an index from Yahoo Finance chart API."""
    period1, period2 = _unix_range(start_date, end_date)
    encoded = urllib.parse.quote(symbol, safe="")
    url = (
        f"https://query2.finance.yahoo.com/v8/finance/chart/{encoded}"
        f"?interval=1d&period1={period1}&period2={period2}"
    )
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))

    chart = payload.get("chart") or {}
    results = chart.get("result") or []
    if not results:
        raise RuntimeError(f"Yahoo chart API returned no data for {symbol} {start_date}..{end_date}")

    result = results[0]
    timestamps = result.get("timestamp") or []
    quote = (result.get("indicators") or {}).get("quote") or [{}]
    quote = quote[0] if quote else {}
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []

    rows: List[IndexDay] = []
    for index, ts in enumerate(timestamps):
        open_px = _safe_float(opens[index] if index < len(opens) else None)
        high_px = _safe_float(highs[index] if index < len(highs) else None)
        low_px = _safe_float(lows[index] if index < len(lows) else None)
        close_px = _safe_float(closes[index] if index < len(closes) else None)
        if open_px is None or close_px is None:
            continue
        trade_date = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
        rows.append(
            IndexDay(
                trade_date=trade_date,
                open=open_px,
                high=high_px if high_px is not None else open_px,
                low=low_px if low_px is not None else open_px,
                close=close_px,
            )
        )

    rows.sort(key=lambda row: row.trade_date)
    prior_close: Optional[float] = None
    enriched: List[IndexDay] = []
    for row in rows:
        enriched.append(
            IndexDay(
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


def write_index_csv(path: Path, rows: Iterable[IndexDay]) -> int:
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


def load_index_daily(path: Path) -> Dict[str, IndexDay]:
    if not path.exists():
        return {}
    rows: Dict[str, IndexDay] = {}
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
            rows[trade_date] = IndexDay(
                trade_date=trade_date,
                open=open_px,
                high=high_px,
                low=low_px,
                close=close_px,
                prior_close=prior,
            )
    return rows


def merge_index_rows(existing: Dict[str, IndexDay], fresh: Iterable[IndexDay]) -> List[IndexDay]:
    merged = dict(existing)
    for row in fresh:
        merged[row.trade_date] = row
    ordered = sorted(merged.values(), key=lambda row: row.trade_date)
    prior_close: Optional[float] = None
    rebuilt: List[IndexDay] = []
    for row in ordered:
        rebuilt.append(
            IndexDay(
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
    symbol: str,
    *,
    start_date: str,
    end_date: str,
    path: Optional[Path] = None,
) -> Tuple[int, str, str]:
    out = path or csv_path_for_symbol(symbol)
    existing = load_index_daily(out)
    fresh = fetch_index_daily_yahoo(symbol, start_date, end_date)
    if not fresh:
        raise RuntimeError(f"No rows downloaded for {symbol} {start_date}..{end_date}")
    merged = merge_index_rows(existing, fresh)
    count = write_index_csv(out, merged)
    return count, merged[0].trade_date, merged[-1].trade_date


def download_all(
    symbols: Sequence[str] = DEFAULT_SYMBOLS,
    *,
    start_date: str,
    end_date: str,
    calendar_dir: Path = CALENDAR_DIR,
) -> List[dict]:
    results: List[dict] = []
    for symbol in symbols:
        path = csv_path_for_symbol(symbol, calendar_dir)
        count, first, last = download_and_save(
            symbol,
            start_date=start_date,
            end_date=end_date,
            path=path,
        )
        stem, label = INDEX_SPECS[symbol]
        results.append(
            {
                "symbol": symbol,
                "label": label,
                "stem": stem,
                "path": str(path),
                "rows": count,
                "first_date": first,
                "last_date": last,
            }
        )
    return results


def close_to_close_returns(by_date: Dict[str, IndexDay]) -> Dict[str, float]:
    """Map trade_date → simple close-to-close return (requires prior_close)."""
    out: Dict[str, float] = {}
    for day, row in by_date.items():
        ret = row.simple_return
        if ret is not None and math.isfinite(ret):
            out[day] = ret
    return out
