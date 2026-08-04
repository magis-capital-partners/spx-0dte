"""Low-latency IB market data: subscribe once, read from an in-memory cache.

Phase 1 — chain metadata cached at session start (no per-poll reqSecDefOptParams).
Phase 2 — reqMktData streaming updates via ticker.updateEvent.
Phase 3 support — fast cache reads for adaptive polling loops.
"""
from __future__ import annotations

import copy
import math
import time as _time
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from live_config import LiveConfig

try:
    from ib_insync import Contract, Index, Option
    HAS_IB = True
except Exception:
    HAS_IB = False

from mbh_simulator import OptionQuote


QuoteKey = Tuple[str, str, float]  # (expiry_iso, option_type, strike)


@dataclass
class CachedQuote:
    bid: float = 0.0
    ask: float = 0.0
    delta: Optional[float] = None
    iv: Optional[float] = None
    updated_at: float = 0.0


@dataclass(frozen=True)
class FeatureInputHealth:
    ok: bool
    reason: str = ""
    max_age_seconds: float = math.inf
    timestamp_dispersion_seconds: float = math.inf
    quote_count: int = 0


def _nearest_listed_strike(listed: Sequence[float], target: float) -> Optional[float]:
    if not listed:
        return None
    return min(listed, key=lambda strike: abs(strike - target))


def _ticker_bid_ask(ticker, *, delayed_fallback: bool) -> Tuple[float, float]:
    bid = float(ticker.bid) if ticker.bid and ticker.bid > 0 else 0.0
    ask = float(ticker.ask) if ticker.ask and ticker.ask > 0 else 0.0
    if not delayed_fallback or (bid > 0 and ask > 0):
        return bid, ask
    last = float(ticker.last) if ticker.last and ticker.last > 0 else 0.0
    close = float(ticker.close) if ticker.close and ticker.close > 0 else 0.0
    try:
        mid = float(ticker.marketPrice()) if ticker.marketPrice() and ticker.marketPrice() > 0 else 0.0
    except Exception:
        mid = 0.0
    ref = last or mid or close
    if ref <= 0:
        return bid, ask
    if bid <= 0 and ask > 0:
        bid = min(ref, ask * 0.98)
    elif ask <= 0 and bid > 0:
        ask = max(ref, bid * 1.02)
    else:
        bid = ref * 0.98
        ask = ref * 1.02
    return max(bid, 0.0), max(ask, 0.0)


def _ticker_greeks(ticker) -> Tuple[Optional[float], Optional[float]]:
    """Return plausible option delta/IV, rejecting IB sentinel values."""
    greeks = getattr(ticker, "modelGreeks", None)
    if greeks is None:
        return None, None
    delta_raw = getattr(greeks, "delta", None)
    iv_raw = getattr(greeks, "impliedVol", None)
    try:
        delta = float(delta_raw) if delta_raw is not None else None
    except (TypeError, ValueError):
        delta = None
    try:
        iv = float(iv_raw) if iv_raw is not None else None
    except (TypeError, ValueError):
        iv = None
    if delta is not None and (not math.isfinite(delta) or abs(delta) > 1.0):
        delta = None
    # Decimal annualized IV; even a 1,000% ceiling is deliberately generous.
    if iv is not None and (not math.isfinite(iv) or iv <= 0.0 or iv > 10.0):
        iv = None
    return delta, iv


def _expiry_iso(expiry_yyyymmdd: str) -> str:
    return f"{expiry_yyyymmdd[:4]}-{expiry_yyyymmdd[4:6]}-{expiry_yyyymmdd[6:8]}"


def _spot_from_ticker(ticker) -> float:
    # SPX index streams update ``last`` but commonly have no streaming bid/ask.
    # A startup reqTickers snapshot can leave an old bid/ask on the reused
    # ib_insync Ticker; marketPrice() then returns that stale midpoint as soon as
    # the live last trades outside the snapshot spread.  Prefer the exchange's
    # live index print and use marketPrice/close only as startup fallbacks.
    last = getattr(ticker, "last", None)
    if last and last > 0:
        return float(last)
    try:
        px = ticker.marketPrice()
        if px and px > 0:
            return float(px)
    except Exception:
        pass
    for attr in ("close",):
        val = getattr(ticker, attr, None)
        if val and val > 0:
            return float(val)
    return 0.0


_MARKET_DATA_HELP = (
    "Could not obtain SPX spot from IB.\n"
    "\n"
    "IB error 10168 means: no live US Index (SPX) subscription, and delayed data is "
    "disabled in TWS.\n"
    "\n"
    "Option A — real-time (what you want):\n"
    "  1. IB Account Management -> Market Data Subscriptions:\n"
    "     - CBOE US Index (SPX) or US Securities Snapshot bundle\n"
    "     - OPRA (US options) for SPXW quotes\n"
    "  2. Restart TWS/Gateway after subs activate.\n"
    "  3. Keep live_config: market_data_type=1, auto_fallback_delayed=False.\n"
    "\n"
    "Option B — run today on free delayed data:\n"
    "  1. TWS -> Settings -> Market Data -> enable 'Allow delayed market data'.\n"
    "  2. Set auto_fallback_delayed=True (or market_data_type=3) in live_config.py.\n"
    "  3. Set entry_require_live_nbbo=False and delayed_quote_fallback=True for delayed.\n"
)


class IBStreamingMarketData:
    """Streaming SPX + SPXW quote cache with one-time chain discovery."""

    def __init__(self, ib, live: LiveConfig, config) -> None:
        self.ib = ib
        self.live = live
        self.config = config
        self._spx = Index("SPX", "CBOE", "USD")
        self._cache: Dict[QuoteKey, CachedQuote] = {}
        self._tickers: Dict[int, object] = {}
        self._contracts: Dict[int, Contract] = {}
        self._spx_ticker = None
        self._spxw = None
        self._expiry_0dte: Optional[str] = None
        self._expiry_next: Optional[str] = None
        self._listed_strikes: List[float] = []
        # Exact 0DTE legs belonging to open/recovered positions. These always
        # take priority over candidate-scanning lines and survive rebalances.
        self._required_0dte_legs: set[Tuple[str, float]] = set()
        self._anchor_spot: float = 0.0
        self._started = False
        self._next_expiry_quotes: List[OptionQuote] = []
        self._last_chain_log = 0.0
        self._last_spx_update_at = 0.0
        self._last_spx_value = 0.0
        self._effective_market_data_type = live.market_data_type
        self._delayed_fallback = live.delayed_quote_fallback and live.market_data_type != 1

    def _probe_spx_snapshot(self, wait_sec: float = 2.0) -> float:
        # ib_insync keys ticker instances by Python contract identity.  Probe
        # with a copy so snapshot bid/ask fields can never contaminate the
        # later streaming ticker for ``self._spx``.
        probe_contract = copy.copy(self._spx)
        [ticker] = self.ib.reqTickers(probe_contract)
        self.ib.sleep(wait_sec)
        return _spot_from_ticker(ticker)

    def _resolve_market_data_access(self) -> None:
        """Probe SPX quote; optionally auto-fallback live (1) -> delayed (3).

        Live mode never falls back to delayed — fail loud if OPRA/index missing.
        """
        requested = self.live.market_data_type
        allow_fallback = bool(self.live.auto_fallback_delayed)
        if getattr(self.live, "mode", "") == "live":
            allow_fallback = False
        self.ib.reqMarketDataType(requested)
        self._effective_market_data_type = requested
        self._delayed_fallback = (
            self.live.delayed_quote_fallback and self._effective_market_data_type != 1
        )

        if self._probe_spx_snapshot() > 0:
            print(
                f"[market] SPX spot OK with market_data_type={self._effective_market_data_type} "
                f"({'live' if self._effective_market_data_type == 1 else 'delayed'})"
            )
            return

        if not allow_fallback:
            raise RuntimeError(
                _MARKET_DATA_HELP
                + "\n"
                f"Probe failed with market_data_type={requested} and "
                f"auto_fallback_delayed=False"
                + (" (forced for live mode)." if getattr(self.live, "mode", "") == "live" else ".")
            )

        if requested != 3:
            print(
                "[market] live SPX/index data unavailable (IB 10168?) — "
                "falling back to delayed market data (type 3). "
                "If this also fails, enable 'Allow delayed market data' in TWS."
            )
            self.ib.reqMarketDataType(3)
            self._effective_market_data_type = 3
            self.live.market_data_type = 3
            self._delayed_fallback = True
            self.live.delayed_quote_fallback = True
            self.live.entry_require_live_nbbo = False
            if self._probe_spx_snapshot(wait_sec=3.0) > 0:
                print(
                    "[market] using delayed quotes (type 3); entry_require_live_nbbo=False "
                    "and delayed_quote_fallback=True applied for this session."
                )
                return

        raise RuntimeError(_MARKET_DATA_HELP)

    def _cancel_spx_feed(self) -> None:
        if self._spx_ticker is not None and self.live.use_streaming_quotes:
            try:
                self.ib.cancelMktData(self._spx)
            except Exception:
                pass
        self._spx_ticker = None

    def _setup_spx_feed(self) -> None:
        self._cancel_spx_feed()
        self._last_spx_update_at = 0.0
        self._last_spx_value = 0.0
        if self.live.use_streaming_quotes:
            self._spx_ticker = self.ib.reqMktData(self._spx, "", False, False)
            self._spx_ticker.updateEvent += self._on_spx_update
        else:
            [self._spx_ticker] = self.ib.reqTickers(self._spx)

    def _wait_for_spot(self) -> float:
        spot = self.spot()
        if spot > 0:
            self._last_spx_value = spot
            self._last_spx_update_at = _time.monotonic()
            return spot
        self.ib.sleep(max(self.live.streaming_warmup_seconds, 1.0))
        spot = self.spot()
        if spot > 0:
            self._last_spx_value = spot
            self._last_spx_update_at = _time.monotonic()
            return spot
        return self._probe_spx_snapshot(wait_sec=1.0)

    @property
    def expiry_0dte_iso(self) -> str:
        return _expiry_iso(self._expiry_0dte) if self._expiry_0dte else ""

    def start(self) -> None:
        if self._started or not HAS_IB:
            return
        self.ib.qualifyContracts(self._spx)
        self._resolve_market_data_access()

        chains = self.ib.reqSecDefOptParams("SPX", "", "IND", self._spx.conId)
        self._spxw = next((c for c in chains if c.tradingClass == "SPXW"), None)
        if self._spxw is None:
            raise RuntimeError("SPXW chain not found from IB")

        self._expiry_0dte = self._today_expiry(self._spxw)
        if not self._expiry_0dte:
            raise RuntimeError("No 0DTE SPXW expiry listed")

        future = sorted(e for e in self._spxw.expirations if e > self._expiry_0dte)
        self._expiry_next = future[0] if future else None

        self._setup_spx_feed()
        spot = self._wait_for_spot()
        if spot <= 0:
            raise RuntimeError(_MARKET_DATA_HELP)

        self._subscribe_strikes(spot)
        if self.live.use_streaming_quotes:
            self.ib.sleep(self.live.streaming_warmup_seconds)
        self._started = True
        mode = "streaming" if self.live.use_streaming_quotes else "snapshot-cache"
        md_label = "live" if self._effective_market_data_type == 1 else "delayed"
        print(
            f"[market] {mode} subscribed lines={len(self._tickers)} "
            f"0DTE={self.expiry_0dte_iso} "
            f"next={_expiry_iso(self._expiry_next) if self._expiry_next else 'n/a'} "
            f"spot={spot:.1f} market_data_type={self._effective_market_data_type} ({md_label})"
        )

    def shutdown(self) -> None:
        if not HAS_IB or not self._started:
            return
        for ticker in list(self._tickers.values()):
            try:
                self.ib.cancelMktData(ticker.contract)
            except Exception:
                pass
        if self._spx_ticker is not None and self.live.use_streaming_quotes:
            try:
                self.ib.cancelMktData(self._spx)
            except Exception:
                pass
        self._tickers.clear()
        self._contracts.clear()
        self._cache.clear()
        self._started = False

    def spot(self) -> float:
        if self._spx_ticker is None:
            return 0.0
        return _spot_from_ticker(self._spx_ticker)

    def spot_age_seconds(self) -> float:
        if self._last_spx_update_at <= 0:
            return float("inf")
        return max(_time.monotonic() - self._last_spx_update_at, 0.0)

    def spot_is_stale(self, max_age_seconds: float) -> bool:
        if max_age_seconds <= 0:
            return False
        return self.spot_age_seconds() > max_age_seconds

    def maybe_rebalance(self) -> None:
        spot = self.spot()
        if spot <= 0 or self._anchor_spot <= 0:
            return
        if abs(spot - self._anchor_spot) >= self.live.spot_rebalance_points:
            print(f"[market] spot moved {self._anchor_spot:.1f} -> {spot:.1f}; re-subscribing strikes")
            self._subscribe_strikes(spot)

    def refresh_next_expiry_at_tranche(self, now: datetime) -> None:
        """Snapshot next-expiry ATM straddle for term_ratio (tranche boundaries only)."""
        if not self.live.fetch_next_expiry_at_tranche or not self._expiry_next:
            self._next_expiry_quotes = []
            return
        spot = self.spot()
        if spot <= 0:
            return
        listed = sorted(self._spxw.strikes) if self._spxw else []
        if not listed:
            return
        atm = min(listed, key=lambda s: abs(s - spot))
        contracts = []
        for right in ("P", "C"):
            contracts.append(
                Option("SPX", self._expiry_next, atm, right, "CBOE", tradingClass="SPXW")
            )
        self.ib.qualifyContracts(*contracts)
        qualified = [c for c in contracts if c.conId]
        if len(qualified) != 2:
            return
        expiry_iso = _expiry_iso(self._expiry_next)
        rows: List[OptionQuote] = []
        tickers: List = []
        try:
            for contract in qualified:
                tickers.append(
                    self.ib.reqMktData(
                        contract,
                        self.live.streaming_generic_ticks,
                        False,
                        False,
                    )
                )
            deadline = _time.monotonic() + max(
                float(self.live.tranche_quote_timeout_seconds), 0.0,
            )
            while _time.monotonic() < deadline:
                prices = [
                    _ticker_bid_ask(
                        ticker, delayed_fallback=self._delayed_fallback,
                    )
                    for ticker in tickers
                ]
                if all(bid > 0 and ask > 0 for bid, ask in prices):
                    break
                self.ib.sleep(min(0.05, max(deadline - _time.monotonic(), 0.0)))
            for opt, tk in zip(qualified, tickers):
                bid, ask = _ticker_bid_ask(tk, delayed_fallback=self._delayed_fallback)
                delta, iv = _ticker_greeks(tk)
                rows.append(
                    OptionQuote(
                        timestamp=now,
                        expiry=expiry_iso,
                        option_type="CALL" if opt.right == "C" else "PUT",
                        strike=float(opt.strike),
                        bid=bid,
                        ask=ask,
                        delta=delta,
                        iv=iv,
                        underlying_price=spot,
                    )
                )
        finally:
            for contract in qualified[:len(tickers)]:
                try:
                    self.ib.cancelMktData(contract)
                except Exception:
                    pass
        self._next_expiry_quotes = rows

    def build_option_quotes(self, now: datetime) -> List[OptionQuote]:
        spot = self.spot()
        if spot <= 0 or not self._expiry_0dte:
            return []
        expiry_iso = self.expiry_0dte_iso
        quotes: List[OptionQuote] = []
        for (exp, opt_type, strike), cached in self._cache.items():
            if exp != expiry_iso:
                continue
            quotes.append(
                OptionQuote(
                    timestamp=now,
                    expiry=exp,
                    option_type=opt_type,
                    strike=strike,
                    bid=cached.bid,
                    ask=cached.ask,
                    delta=cached.delta,
                    iv=cached.iv,
                    underlying_price=spot,
                )
            )
        self._log_chain_health(spot, quotes)
        return quotes

    def next_expiry_quotes(self) -> List[OptionQuote]:
        return list(self._next_expiry_quotes)

    def quote_age_seconds(self, option_type: str, strike: float) -> Optional[float]:
        key: QuoteKey = (self.expiry_0dte_iso, option_type, float(strike))
        cached = self._cache.get(key)
        if cached is None or cached.updated_at <= 0:
            return None
        return _time.time() - cached.updated_at

    def quote_update_time(self, option_type: str, strike: float) -> Optional[float]:
        cached = self._cache.get((self.expiry_0dte_iso, option_type, float(strike)))
        return cached.updated_at if cached is not None and cached.updated_at > 0 else None

    def feature_input_health(
        self,
        spot: float,
        *,
        max_age_seconds: float,
        max_dispersion_seconds: float,
    ) -> FeatureInputHealth:
        """Validate the ATM and 25-delta cross-section used by live alpha."""
        rows = [
            (key, quote) for key, quote in self._cache.items()
            if key[0] == self.expiry_0dte_iso
            and quote.bid > 0 and quote.ask > quote.bid
            and quote.delta is not None and math.isfinite(float(quote.delta))
            and quote.iv is not None and math.isfinite(float(quote.iv)) and quote.iv > 0
            and quote.updated_at > 0
        ]
        selected: List[CachedQuote] = []
        for option_type in ("CALL", "PUT"):
            side = [(key, quote) for key, quote in rows if key[1] == option_type]
            if not side:
                return FeatureInputHealth(False, "missing_feature_quotes", quote_count=len(selected))
            selected.append(min(side, key=lambda row: abs(row[0][2] - spot))[1])
            selected.append(min(side, key=lambda row: abs(abs(float(row[1].delta)) - 0.25))[1])
        if len({id(row) for row in selected}) < 4:
            return FeatureInputHealth(False, "missing_feature_quotes", quote_count=len(selected))
        now = _time.time()
        ages = [max(0.0, now - row.updated_at) for row in selected]
        updates = [row.updated_at for row in selected]
        max_age = max(ages)
        dispersion = max(updates) - min(updates)
        if max_age_seconds > 0 and max_age > max_age_seconds:
            return FeatureInputHealth(False, "stale_feature_quotes", max_age, dispersion, 4)
        if max_dispersion_seconds > 0 and dispersion > max_dispersion_seconds:
            return FeatureInputHealth(False, "unsynchronized_feature_quotes", max_age, dispersion, 4)
        return FeatureInputHealth(True, max_age_seconds=max_age, timestamp_dispersion_seconds=dispersion, quote_count=4)

    def refresh_spread_legs(
        self,
        now: datetime,
        short_type: str,
        short_strike: float,
        long_strike: float,
    ) -> Tuple[Optional[OptionQuote], Optional[OptionQuote]]:
        """Read the latest subscribed spread-leg quotes without blocking."""
        if not self._expiry_0dte:
            return None, None
        expiry_iso = self.expiry_0dte_iso
        spot = self.spot()
        quotes: List[OptionQuote] = []
        opt_type = "PUT" if short_type.upper() in {"P", "PUT"} else "CALL"
        for strike in (float(short_strike), float(long_strike)):
            cached = self._cache.get((expiry_iso, opt_type, strike))
            if cached is None:
                continue
            quotes.append(
                OptionQuote(
                    timestamp=now,
                    expiry=expiry_iso,
                    option_type=opt_type,
                    strike=strike,
                    bid=cached.bid,
                    ask=cached.ask,
                    delta=cached.delta,
                    iv=cached.iv,
                    underlying_price=spot,
                )
            )
        short_q = next((q for q in quotes if q.strike == float(short_strike)), None)
        long_q = next((q for q in quotes if q.strike == float(long_strike)), None)
        return short_q, long_q

    def _today_expiry(self, spxw) -> Optional[str]:
        today = datetime.now().date().strftime("%Y%m%d")
        if today in spxw.expirations:
            return today
        future = sorted(e for e in spxw.expirations if e >= today)
        return future[0] if future else None

    def _select_strikes(self, spot: float) -> List[float]:
        max_strikes = max(8, self.live.max_chain_lines // 2)
        lo = spot - self.live.chain_points_below
        hi = spot + self.live.chain_points_above
        listed = sorted(s for s in self._spxw.strikes if lo <= s <= hi)
        if not listed:
            return []

        cfg = self.config
        put_wing = cfg.put_wing_width if cfg.put_wing_width > 0 else cfg.wing_width
        call_wing = cfg.call_wing_width if cfg.call_wing_width > 0 else cfg.wing_width
        # Keep enough near-spot strikes subscribed that an exact configured
        # wing can be built after ordinary intraday moves. Wider offsets are
        # still retained for candidate diversity.
        short_offsets = (20.0, 30.0, 40.0, 55.0, 75.0, 100.0)
        priority: List[float] = []
        seen: set[float] = set()

        def add(strike: Optional[float]) -> None:
            if strike is None or strike not in listed or strike in seen:
                return
            seen.add(strike)
            priority.append(strike)

        for offset in short_offsets:
            put_short = _nearest_listed_strike(listed, spot - offset)
            call_short = _nearest_listed_strike(listed, spot + offset)
            if put_short is not None:
                add(put_short)
                add(_nearest_listed_strike(listed, put_short - put_wing))
            if call_short is not None:
                add(call_short)
                add(_nearest_listed_strike(listed, call_short + call_wing))

        for strike in sorted(listed, key=lambda s: abs(s - spot)):
            add(strike)
            if len(priority) >= max_strikes:
                break
        return priority[:max_strikes]

    def _desired_contract_specs(self, spot: float) -> List[Tuple[str, float]]:
        """Build a bounded subscription plan with open-risk legs first.

        Required position legs displace candidate-scanning lines. If open risk
        alone exceeds the configured budget, all risk legs are still retained
        and the scanning grid is sacrificed.
        """
        required = sorted(
            self._required_0dte_legs,
            key=lambda item: (item[1], item[0]),
        )
        grid_specs = [
            (right, float(strike))
            for strike in self._select_strikes(spot)
            for right in ("P", "C")
        ]
        budget = max(int(self.live.max_chain_lines), len(required))
        desired = list(required)
        seen = set(required)
        if len(desired) >= budget:
            return desired
        for spec in grid_specs:
            if spec in seen:
                continue
            desired.append(spec)
            seen.add(spec)
            if len(desired) >= budget:
                break
        return desired

    def set_required_0dte_legs(
        self,
        legs: Sequence[Tuple[str, float]],
    ) -> bool:
        """Pin exact open-position legs; return True when the set changed."""
        normalized = {
            (str(right).strip().upper(), float(strike))
            for right, strike in legs
        }
        bad = [right for right, _ in normalized if right not in {"P", "C"}]
        if bad:
            raise ValueError(
                f"unsupported required option rights: {sorted(set(bad))}"
            )
        if normalized == self._required_0dte_legs:
            return False
        self._required_0dte_legs = normalized
        if self._started:
            spot = self.spot()
            if spot <= 0:
                raise RuntimeError(
                    "cannot refresh required SPXW legs without SPX spot"
                )
            self._subscribe_strikes(spot)
            if self.live.use_streaming_quotes:
                self.ib.sleep(max(self.live.streaming_warmup_seconds, 0.0))
        return True

    def missing_required_quotes(self) -> List[Tuple[str, float]]:
        """Required legs without a fresh markable quote.

        A zero bid is valid for a far-OTM protective long; a positive ask proves
        the contract has received a market update and also protects short-leg
        stop monitoring.
        """
        expiry = self.expiry_0dte_iso
        missing: List[Tuple[str, float]] = []
        for right, strike in sorted(self._required_0dte_legs):
            opt_type = "CALL" if right == "C" else "PUT"
            cached = self._cache.get((expiry, opt_type, float(strike)))
            if (
                cached is None
                or cached.updated_at <= 0
                or cached.ask <= 0
            ):
                missing.append((right, float(strike)))
        return missing

    def wait_for_required_quotes(
        self,
        timeout_seconds: float,
    ) -> List[Tuple[str, float]]:
        """Wait for all open-risk legs to warm; return any remaining gaps."""
        deadline = _time.time() + max(float(timeout_seconds), 0.0)
        missing = self.missing_required_quotes()
        while missing and _time.time() < deadline:
            self.ib.sleep(min(0.25, max(deadline - _time.time(), 0.0)))
            missing = self.missing_required_quotes()
        return missing

    def _prune_0dte_cache(
        self,
        active_specs: set[Tuple[str, float]],
    ) -> None:
        """Remove one-off snapshot quotes that are not in the active plan."""
        expiry = self.expiry_0dte_iso
        active_keys = {
            (
                expiry,
                "CALL" if right == "C" else "PUT",
                float(strike),
            )
            for right, strike in active_specs
        }
        for key in list(self._cache):
            if key[0] == expiry and key not in active_keys:
                self._cache.pop(key, None)

    def _subscribe_strikes(self, spot: float) -> None:
        specs = self._desired_contract_specs(spot)
        self._listed_strikes = sorted({strike for _, strike in specs})
        self._anchor_spot = spot

        new_contracts: List[Contract] = []
        for right, strike in specs:
            new_contracts.append(
                Option(
                    "SPX",
                    self._expiry_0dte,
                    strike,
                    right,
                    "CBOE",
                    tradingClass="SPXW",
                )
            )
        self.ib.qualifyContracts(*new_contracts)
        qualified = [c for c in new_contracts if c.conId]
        qualified_specs = {
            (str(contract.right), float(contract.strike))
            for contract in qualified
        }
        unqualified_required = self._required_0dte_legs - qualified_specs
        if unqualified_required:
            raise RuntimeError(
                "IB could not qualify required open-position leg(s): "
                + ", ".join(
                    f"{right}{strike:g}"
                    for right, strike in sorted(unqualified_required)
                )
            )
        self._prune_0dte_cache(qualified_specs)
        new_ids = {c.conId for c in qualified}

        if self.live.use_streaming_quotes:
            for con_id, ticker in list(self._tickers.items()):
                if con_id not in new_ids:
                    try:
                        self.ib.cancelMktData(ticker.contract)
                    except Exception:
                        pass
                    contract = self._contracts.get(con_id)
                    if contract is not None:
                        opt_type = "CALL" if contract.right == "C" else "PUT"
                        self._cache.pop(
                            (
                                self.expiry_0dte_iso,
                                opt_type,
                                float(contract.strike),
                            ),
                            None,
                        )
                    self._tickers.pop(con_id, None)
                    self._contracts.pop(con_id, None)

        for contract in qualified:
            if contract.conId in self._tickers:
                continue
            if self.live.use_streaming_quotes:
                ticker = self.ib.reqMktData(
                    contract,
                    self.live.streaming_generic_ticks,
                    False,
                    False,
                )
                ticker.updateEvent += lambda t, c=contract: self._on_option_update(c, t)
            else:
                [ticker] = self.ib.reqTickers(contract)
                self._update_cache_from_ticker(contract, ticker)
            self._tickers[contract.conId] = ticker
            self._contracts[contract.conId] = contract

        if not self.live.use_streaming_quotes:
            self._refresh_snapshot_cache()

    def _refresh_snapshot_cache(self) -> None:
        """Non-streaming fallback: batched reqTickers refresh."""
        contracts = list(self._contracts.values())
        if not contracts:
            return
        tickers: List = []
        batch = 40
        for i in range(0, len(contracts), batch):
            tickers.extend(self.ib.reqTickers(*contracts[i:i + batch]))
        for contract, ticker in zip(contracts, tickers):
            self._update_cache_from_ticker(contract, ticker)

    def _on_spx_update(self, ticker) -> None:
        last = getattr(ticker, "last", None)
        if last and last > 0:
            self._last_spx_value = float(last)
            self._last_spx_update_at = _time.monotonic()

    def _on_option_update(self, contract: Contract, ticker) -> None:
        self._update_cache_from_ticker(contract, ticker)

    def _update_cache_from_ticker(self, contract: Contract, ticker) -> None:
        opt_type = "CALL" if contract.right == "C" else "PUT"
        key: QuoteKey = (self.expiry_0dte_iso, opt_type, float(contract.strike))
        bid, ask = _ticker_bid_ask(ticker, delayed_fallback=self._delayed_fallback)
        delta, iv = _ticker_greeks(ticker)
        self._cache[key] = CachedQuote(bid=bid, ask=ask, delta=delta, iv=iv, updated_at=_time.time())

    def _log_chain_health(self, spot: float, quotes: Sequence[OptionQuote]) -> None:
        now_ts = _time.time()
        if now_ts - self._last_chain_log < 60:
            return
        self._last_chain_log = now_ts
        with_delta = sum(1 for q in quotes if q.delta is not None)
        with_bidask = sum(1 for q in quotes if q.bid > 0 and q.ask > 0)
        strikes = sorted({q.strike for q in quotes})
        span = f"{strikes[0]:.0f}-{strikes[-1]:.0f}" if strikes else "n/a"
        fb = " fallback=on" if self._delayed_fallback else ""
        print(f"[chain] spot={spot:.1f} quotes={len(quotes)} strikes={span} "
              f"bid/ask={with_bidask} delta={with_delta}{fb}")
