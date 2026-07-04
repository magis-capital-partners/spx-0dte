"""Interactive Brokers live/paper executor for the SPX 0DTE vertical-spread strategy.

This reuses the backtest engine's selection and risk logic (``StrategyConfig``,
``select_candidate_entries``, the short-leg stop with N-bar confirmation, and the
halt/flatten governor) so live decisions match the validated backtest. Market
data plugs in through ``SignalProvider`` -- an IB implementation is included; you
can also feed it from ThetaData for signals while routing execution through IB.

All runtime settings live in ``live/live_config.py`` (``ACTIVE``); the strategy
itself comes from a named profile in ``simulator/profiles.py``. There are no
command-line flags -- run a session with::

    python live/ib_executor.py

and edit ``ACTIVE`` (profile, mode, sizing, account, IB connection) to change it.

Modes:
  dry   -- compute and log intended trades, place nothing (safe default).
  paper -- route orders to IB paper (port 7497).
  live  -- route orders to IB live (port 7496); requires allow_live=True.
"""
from __future__ import annotations

import json
import logging
import sys
import time as _time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Protocol, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))

from mbh_simulator import (  # noqa: E402  (path injection above)
    CandidateRecord,
    OptionQuote,
    SignalSnapshot,
    StrategyConfig,
    TrancheSummary,
    _summarize_tranche_from_records,
    build_scored_candidates,
    candidate_margin_per_contract,
    is_entry_time,
    select_candidate_entries,
)

try:  # optional dependency; only needed for paper/live modes
    from ib_insync import IB, Index, Option, Contract, ComboLeg, Order, LimitOrder, StopOrder, TagValue
    HAS_IB = True
except Exception:  # pragma: no cover - import guard
    HAS_IB = False

from live_config import ACTIVE, LiveConfig  # noqa: E402
from profiles import schedule_multiplier  # noqa: E402
from strategy_profiles import resolve_strategy_config  # noqa: E402


LIVE_DIR = ROOT / "data" / "live"


# --------------------------------------------------------------------------- #
# Market data / signal interface
# --------------------------------------------------------------------------- #
class SignalProvider(Protocol):
    """Returns the current option-chain snapshot and signal features.

    A backtest-faithful live feed must produce the same fields the simulator's
    feature builder produced (z-scored straddle residual, skew, term ratio,
    trend, realized-vs-implied), using rolling historical baselines. See
    ``simulator/feature_builder.py`` and ``simulator/historical_baselines.py``.
    """

    def fetch(self, now: datetime) -> Tuple[List[OptionQuote], Optional[SignalSnapshot]]:
        ...


@dataclass
class OpenSpread:
    """A live spread position tracked for stop and flatten management."""
    candidate: CandidateRecord
    contracts: int
    short_entry_sell: float
    long_entry_buy: float
    stop_price: float
    combo_order_id: Optional[int] = None
    stop_order_id: Optional[int] = None
    stop_confirm_count: int = 0
    stopped: bool = False
    closed: bool = False

    @property
    def entry_credit(self) -> float:
        return self.short_entry_sell - self.long_entry_buy


# --------------------------------------------------------------------------- #
# IB market data implementation (skeleton)
# --------------------------------------------------------------------------- #
# IB codes that are normal on paper (delayed data, farm OK messages) — file only.
_QUIET_IB_ERROR_CODES = frozenset({10090, 10167, 2104, 2106, 2158})


def setup_ib_logging(today: str) -> Path:
    """Send ib_insync library logs to data/live/<date>/ib.log, not the console.

    IB warning 10090 is logged at INFO by ib_insync's wrapper; routing the
    ``ib_insync`` logger to a file keeps CMD clean while preserving full detail.
    """
    day_dir = LIVE_DIR / today
    day_dir.mkdir(parents=True, exist_ok=True)
    ib_log = day_dir / "ib.log"

    ib_logger = logging.getLogger("ib_insync")
    ib_logger.handlers.clear()
    ib_logger.propagate = False
    ib_logger.setLevel(logging.DEBUG)

    handler = logging.FileHandler(ib_log, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    ib_logger.addHandler(handler)

    # Belt-and-suspenders: ib_insync also hooks the root console at INFO by default.
    if HAS_IB:
        from ib_insync import util
        util.logToConsole(logging.ERROR)

    return ib_log


def register_ib_error_handler(ib: "IB", today: str) -> None:
    """Structured IB error/warning log at data/live/<date>/ib_errors.jsonl."""
    errors_path = LIVE_DIR / today / "ib_errors.jsonl"

    def _on_error(reqId: int, errorCode: int, errorString: str, contract) -> None:
        record = {
            "ts": datetime.now().isoformat(),
            "reqId": reqId,
            "errorCode": errorCode,
            "errorString": errorString,
            "contract": str(contract) if contract else None,
            "quiet": errorCode in _QUIET_IB_ERROR_CODES,
        }
        errors_path.parent.mkdir(parents=True, exist_ok=True)
        with errors_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        if errorCode not in _QUIET_IB_ERROR_CODES:
            label = getattr(contract, "localSymbol", None) or getattr(contract, "symbol", "")
            print(f"IB error {errorCode} (reqId {reqId}): {errorString}"
                  + (f" [{label}]" if label else ""))

    ib.errorEvent += _on_error


def _nearest_listed_strike(listed: Sequence[float], target: float) -> Optional[float]:
    if not listed:
        return None
    return min(listed, key=lambda strike: abs(strike - target))


def _ticker_bid_ask(ticker, *, delayed_fallback: bool) -> Tuple[float, float]:
    """Return (bid, ask), optionally filling gaps from last/mid on delayed feeds."""
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


class IBSignalProvider:
    """Pulls the live SPXW 0DTE chain from IB and builds an OptionQuote snapshot.

    Uses snapshot quotes (``reqTickers``) capped to stay under IB's ~100-line limit.
    Strike selection is wing-aware so 200/75pt spreads include both short and long legs.
    """

    def __init__(
        self,
        ib: "IB",
        live: LiveConfig,
        config: StrategyConfig,
        baselines_path: Optional[Path] = None,
    ):
        self.ib = ib
        self.live = live
        self.config = config
        self.baselines = self._load_baselines(baselines_path) if baselines_path else None
        self._spx = Index("SPX", "CBOE", "USD")
        self.ib.qualifyContracts(self._spx)
        self._last_chain_log = 0.0
        self._delayed_fallback = (
            live.delayed_quote_fallback and live.market_data_type != 1
        )

    @staticmethod
    def _load_baselines(path: Path) -> dict:
        if not path.exists():
            raise FileNotFoundError(f"baselines file not found: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _spx_spot(self) -> float:
        [ticker] = self.ib.reqTickers(self._spx)
        return float(ticker.marketPrice() or ticker.close or 0.0)

    def _today_expiry(self, spxw) -> Optional[str]:
        """Return YYYYMMDD for today's 0DTE, or the nearest listed expiry."""
        today = datetime.now().date().strftime("%Y%m%d")
        if today in spxw.expirations:
            return today
        future = sorted(e for e in spxw.expirations if e >= today)
        return future[0] if future else None

    def _select_strikes(self, spxw, spot: float) -> List[float]:
        """Pick strikes for short legs AND fixed-width wings within the line budget."""
        max_strikes = max(8, self.live.max_chain_lines // 2)
        lo = spot - self.live.chain_points_below
        hi = spot + self.live.chain_points_above
        listed = sorted(s for s in spxw.strikes if lo <= s <= hi)
        if not listed:
            return []

        cfg = self.config
        put_wing = cfg.put_wing_width if cfg.put_wing_width > 0 else cfg.wing_width
        call_wing = cfg.call_wing_width if cfg.call_wing_width > 0 else cfg.wing_width

        # Typical ~20Δ shorts sit ~50–120 pts from spot on 0DTE; anchor there and
        # force-include the corresponding long-wing strikes.
        short_offsets = (50.0, 80.0, 110.0)
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

        return sorted(priority[:max_strikes])

    def _req_tickers_batched(self, contracts: List[Contract], batch_size: int = 40) -> List:
        """Snapshot quotes in batches (reqTickers auto-closes; no manual cancel)."""
        tickers: List = []
        for i in range(0, len(contracts), batch_size):
            batch = contracts[i:i + batch_size]
            tickers.extend(self.ib.reqTickers(*batch))
        return tickers

    def _log_chain_health(self, spot: float, quotes: Sequence[OptionQuote]) -> None:
        """Periodic one-line status so you know data is flowing."""
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

    def fetch(self, now: datetime) -> Tuple[List[OptionQuote], Optional[SignalSnapshot]]:
        spot = self._spx_spot()
        if spot <= 0:
            return [], None

        chains = self.ib.reqSecDefOptParams("SPX", "", "IND", self._spx.conId)
        spxw = next((c for c in chains if c.tradingClass == "SPXW"), None)
        if spxw is None:
            return [], None

        expiry = self._today_expiry(spxw)
        if expiry is None:
            return [], None

        strikes = self._select_strikes(spxw, spot)
        if not strikes:
            return [], None

        contracts: List[Contract] = []
        for strike in strikes:
            for right in ("P", "C"):
                contracts.append(
                    Option("SPX", expiry, strike, right, "CBOE", tradingClass="SPXW")
                )

        # qualifyContracts drops strikes that don't exist for this expiry (Error 200).
        self.ib.qualifyContracts(*contracts)
        qualified = [c for c in contracts if c.conId]
        if not qualified:
            return [], None

        tickers = self._req_tickers_batched(qualified)

        today_iso = now.date().isoformat()
        quotes: List[OptionQuote] = []
        for opt, tk in zip(qualified, tickers):
            bid, ask = _ticker_bid_ask(tk, delayed_fallback=self._delayed_fallback)
            delta = tk.modelGreeks.delta if tk.modelGreeks is not None else None
            quotes.append(
                OptionQuote(
                    timestamp=now,
                    expiry=today_iso,
                    option_type="CALL" if opt.right == "C" else "PUT",
                    strike=float(opt.strike),
                    bid=bid,
                    ask=ask,
                    delta=delta,
                    underlying_price=spot,
                )
            )

        self._log_chain_health(spot, quotes)
        signal = self._build_signal(now, quotes, spot)
        return quotes, signal

    def _build_signal(self, now: datetime, quotes: Sequence[OptionQuote], spot: float) -> Optional[SignalSnapshot]:
        # === SEAM: assemble z-scored features from live chain + baselines ===
        # Compute the live ATM straddle value, delta-matched skew, and (if you
        # subscribe to the next expiry) the 0DTE/1DTE term ratio, then convert
        # to z-scores using self.baselines for this time-of-day bucket, exactly
        # as simulator/historical_baselines.transform_rows does. Until wired,
        # return a neutral snapshot so dry-run reports chain health without
        # fabricating signals.
        if self.baselines is None:
            return SignalSnapshot(timestamp=now)
        # TODO: implement live z-score assembly against self.baselines.
        return SignalSnapshot(timestamp=now)


# --------------------------------------------------------------------------- #
# Order construction
# --------------------------------------------------------------------------- #
def _round_spx_premium(price: float) -> float:
    """SPX/SPXW options use $0.05 minimum increment for premiums under $3."""
    if price <= 0:
        return price
    tick = 0.05 if price < 3.0 else 0.10
    return round(round(price / tick) * tick, 2)


def _candidate_option_type(candidate: CandidateRecord) -> str:
    """Vertical spreads use the same option type on both legs."""
    return candidate.short_type


def _trade_rejection_reason(trade) -> str:
    """Return a non-empty reason when IB rejected or cancelled the order."""
    for entry in reversed(trade.log):
        msg = entry.message or ""
        code = getattr(entry, "errorCode", 0) or 0
        if code in {201, 202, 203, 110} or "reject" in msg.lower() or "not allowed" in msg.lower():
            return msg or f"error {code}"
        if msg and "permission" in msg.lower():
            return msg
    status = (trade.orderStatus.status or "").lower()
    if status in {"cancelled", "inactive", "apicancelled"} or "reject" in status:
        return trade.orderStatus.status or "rejected"
    return ""


def _wait_for_combo_order(ib: "IB", trade, *, timeout_sec: float = 8.0) -> Tuple[str, str]:
    """Poll until the combo is filled, rejected, or times out.

    Returns ``(state, reason)`` where state is ``filled``, ``rejected``, or
    ``pending``.
    """
    deadline = _time.time() + timeout_sec
    while _time.time() < deadline:
        ib.sleep(0.25)
        reason = _trade_rejection_reason(trade)
        if reason:
            return "rejected", reason
        status = (trade.orderStatus.status or "").lower()
        filled = float(trade.orderStatus.filled or 0)
        if status == "filled" or filled >= float(trade.order.totalQuantity):
            return "filled", ""
        if status in {"cancelled", "inactive", "apicancelled"}:
            return "rejected", status
    return "pending", ""


def build_combo(ib: "IB", candidate: CandidateRecord, today: str) -> "Contract":
    """Build an SPXW vertical-spread BAG combo: sell the short leg, buy the wing."""
    expiry = today.replace("-", "")
    right = "P" if candidate.short_type == "PUT" else "C"
    short_leg_opt = Option("SPX", expiry, candidate.short_strike, right, "CBOE", tradingClass="SPXW")
    long_leg_opt = Option("SPX", expiry, candidate.long_strike, right, "CBOE", tradingClass="SPXW")
    ib.qualifyContracts(short_leg_opt, long_leg_opt)

    bag = Contract()
    bag.symbol = "SPX"
    bag.secType = "BAG"
    bag.currency = "USD"
    bag.exchange = "SMART"
    short_leg = ComboLeg(conId=short_leg_opt.conId, ratio=1, action="SELL", exchange="SMART")
    long_leg = ComboLeg(conId=long_leg_opt.conId, ratio=1, action="BUY", exchange="SMART")
    bag.comboLegs = [short_leg, long_leg]
    return bag, short_leg_opt


def _short_option(ib: "IB", candidate: CandidateRecord, today: str) -> "Option":
    expiry = today.replace("-", "")
    right = "P" if candidate.short_type == "PUT" else "C"
    opt = Option("SPX", expiry, candidate.short_strike, right, "CBOE", tradingClass="SPXW")
    ib.qualifyContracts(opt)
    return opt


def place_spread(ib: "IB", candidate: CandidateRecord, contracts: int, config: StrategyConfig,
                 today: str, dry: bool) -> Tuple[OpenSpread, bool]:
    """Place the spread as a net-credit limit combo.

    IB combo convention: **open** any vertical with ``BUY`` and a **negative**
    limit price (net credit). ``SELL`` on the bag inverts leg actions and triggers
    error 201 (riskless combination).

    The short-leg stop is managed synthetically in the run loop (mark + N-bar
    confirmation) so it matches the backtest; a wide native STP is attached only
    as a crash backstop.
    """
    short_sell = candidate.short_quote.bid if candidate.short_quote else 0.0
    long_buy = candidate.long_quote.ask if candidate.long_quote else 0.0
    spread = OpenSpread(
        candidate=candidate,
        contracts=contracts,
        short_entry_sell=short_sell,
        long_entry_buy=long_buy,
        stop_price=short_sell * config.stop_multiple,
    )
    if dry:
        return spread, True

    bag, short_leg_opt = build_combo(ib, candidate, today)
    credit = _round_spx_premium(candidate.credit)
    combo_order = LimitOrder("BUY", contracts, -credit)
    combo_order.tif = "DAY"
    trade = ib.placeOrder(bag, combo_order)
    spread.combo_order_id = trade.order.orderId

    state, reason = _wait_for_combo_order(ib, trade)
    if state == "pending":
        ib.cancelOrder(trade.order)
        ib.sleep(0.5)
        reason = _trade_rejection_reason(trade) or "timeout_unfilled"
        state = "rejected"
    if state != "filled":
        log_event(today, {
            "event": "order_rejected",
            "side": candidate.side,
            "short_strike": candidate.short_strike,
            "long_strike": candidate.long_strike,
            "contracts": contracts,
            "credit": credit,
            "status": trade.orderStatus.status,
            "reason": reason,
        })
        print(f"[{datetime.now().isoformat()}] ORDER REJECTED {candidate.side} "
              f"{candidate.short_strike}/{candidate.long_strike} "
              f"status={trade.orderStatus.status} reason={reason}")
        return spread, False

    # Native STP backstop only after the combo fills (well beyond the synthetic
    # trigger) in case the process dies; the loop's synthetic stop is primary.
    backstop = _round_spx_premium(spread.stop_price * 1.5)
    if backstop > 0:
        stop_order = StopOrder("BUY", contracts, backstop)
        stop_order.tif = "DAY"
        stop_trade = ib.placeOrder(short_leg_opt, stop_order)
        spread.stop_order_id = stop_trade.order.orderId
        stop_reason = _trade_rejection_reason(stop_trade)
        if stop_reason:
            ib.cancelOrder(stop_trade.order)
            log_event(today, {
                "event": "stop_backstop_rejected",
                "short_strike": candidate.short_strike,
                "backstop": backstop,
                "reason": stop_reason,
            })
    return spread, True


def manage_stops(ib: "IB", open_spreads: Sequence[OpenSpread], quotes: Sequence[OptionQuote],
                 config: StrategyConfig, today: str, dry: bool) -> List[OpenSpread]:
    """Short-leg stop with N-bar confirmation (mirrors simulator.process_stops).

    Triggers when the short leg's ask reaches ``stop_price`` for
    ``stop_confirmation_count`` consecutive polls, then buys back the SHORT leg
    only and keeps the long wing to settle -- exactly as the backtest does.
    """
    if not config.use_short_leg_stops or not quotes:
        return []
    lookup = {(q.option_type, q.strike): q for q in quotes}
    newly_stopped: List[OpenSpread] = []
    for spread in open_spreads:
        if spread.stopped or spread.closed:
            continue
        sq = lookup.get((spread.candidate.short_type, spread.candidate.short_strike))
        if sq is None or sq.ask <= 0:
            spread.stop_confirm_count = 0
            continue
        if sq.ask >= spread.stop_price:
            spread.stop_confirm_count += 1
        else:
            spread.stop_confirm_count = 0
        if spread.stop_confirm_count < config.stop_confirmation_count:
            continue

        spread.stopped = True
        if not dry and HAS_IB:
            short_opt = _short_option(ib, spread.candidate, today)
            ib.placeOrder(short_opt, Order(action="BUY", totalQuantity=spread.contracts, orderType="MKT"))
        newly_stopped.append(spread)
        log_event(today, {"event": "stop", "side": spread.candidate.side,
                          "short_strike": spread.candidate.short_strike,
                          "stop_price": round(spread.stop_price, 2),
                          "short_ask": round(sq.ask, 2), "contracts": spread.contracts, "dry": dry})
        print(f"[{datetime.now().isoformat()}] STOP short {spread.candidate.short_strike} "
              f"ask={sq.ask:.2f}>={spread.stop_price:.2f} (keep long wing){' (dry)' if dry else ''}")
    return newly_stopped


def flatten_all(ib: "IB", open_spreads: Sequence[OpenSpread], today: str, dry: bool) -> None:
    """Flatten governor: close every open spread immediately (both legs)."""
    for spread in open_spreads:
        if spread.closed:
            continue
        if not dry and HAS_IB:
            bag, _short_leg_opt = build_combo(ib, spread.candidate, today)
            # Close an opened combo with SELL (inverse of opening BUY).
            ib.placeOrder(bag, Order(action="SELL", totalQuantity=spread.contracts, orderType="MKT"))
        spread.closed = True


# --------------------------------------------------------------------------- #
# Logging / session snapshot
# --------------------------------------------------------------------------- #
def log_event(today: str, event: dict) -> None:
    day_dir = LIVE_DIR / today
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / "fills.jsonl"
    event = {"ts": datetime.now().isoformat(), **event}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event) + "\n")


def log_tranche(today: str, record: dict) -> None:
    day_dir = LIVE_DIR / today
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / "tranches.jsonl"
    payload = {"ts": datetime.now().isoformat(), **record}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, default=str) + "\n")


def _tranche_log_dict(summary: TrancheSummary) -> dict:
    row = asdict(summary)
    row["timestamp"] = summary.timestamp.isoformat()
    return row


def _format_tranche_console(summary: TrancheSummary, executed: int) -> str:
    clock = summary.timestamp.strftime("%H:%M")
    if summary.skip_reason:
        return (f"[tranche] {clock} SKIP {summary.skip_reason} "
                f"(policy={summary.policy_contracts})")
    if executed > 0:
        return (f"[tranche] {clock} ENTRY x{executed} "
                f"{summary.selected_summary or 'selected'}")
    reason = summary.top_reject_reason or "no_pass"
    best = summary.best_pass_score
    best_txt = f"{best:.3f}" if best is not None else "n/a"
    return (f"[tranche] {clock} SKIP {reason} "
            f"candidates={summary.candidates_total} pass={summary.candidates_pass} "
            f"best_pass={best_txt}")


def write_session_snapshot(today: str, live: LiveConfig, config: StrategyConfig, sizing_scheme: str) -> None:
    """Persist the fully-resolved config so every live day is self-describing
    and reconciliation can replay the exact same parameters."""
    day_dir = LIVE_DIR / today
    day_dir.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "date": today,
        "generated_at": datetime.now().isoformat(),
        "live_config": asdict(live),
        "sizing_scheme": sizing_scheme,
        "strategy_config": asdict(config),
    }
    (day_dir / "config.json").write_text(
        json.dumps(snapshot, indent=2, default=str), encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# Main run loop
# --------------------------------------------------------------------------- #
def _tranche_base_contracts(config: StrategyConfig, sizing_schedule, now: datetime) -> int:
    """Flat baseline, optionally reshaped by a Test-3G time-of-day schedule."""
    base = config.baseline_contracts
    if sizing_schedule:
        base = round(base * schedule_multiplier(now.time(), sizing_schedule))
    return max(0, base)


def _process_tranche(
    *,
    now: datetime,
    today: str,
    quotes: Sequence[OptionQuote],
    signal: Optional[SignalSnapshot],
    config: StrategyConfig,
    sizing_schedule,
    live: LiveConfig,
    ib: Optional["IB"],
    dry: bool,
    entries_halted: bool,
    open_spreads: List[OpenSpread],
    gross_credit_sold: float,
    daily_credit_cap: float,
    sleeve_margin_used: dict,
    portfolio_margin_used: float,
) -> Tuple[int, float, float, float]:
    """Evaluate one entry tranche; log diagnostics; place any selected spreads."""
    base_contracts = _tranche_base_contracts(config, sizing_schedule, now)
    skip_reason = ""
    if entries_halted:
        skip_reason = "entries_halted"
    elif signal is None:
        skip_reason = "no_signal"
    elif not quotes:
        skip_reason = "empty_chain"
    elif base_contracts <= 0:
        skip_reason = "zero_base_contracts"

    records: List[CandidateRecord] = []
    selected: List[CandidateRecord] = []
    if not skip_reason:
        records = build_scored_candidates(quotes, signal, config)
        selected, records = select_candidate_entries(
            quotes, signal, base_contracts, config, records=records
        )

    executed = 0
    credit_added = 0.0
    margin_added = 0.0
    order_rejected = False
    for cand in selected:
        if cand.short_quote is None or cand.long_quote is None:
            continue
        contracts = _size_with_caps(
            cand, config, gross_credit_sold + credit_added,
            daily_credit_cap, sleeve_margin_used, portfolio_margin_used + margin_added,
        )
        contracts = min(contracts, live.max_contracts_per_tranche)
        if contracts <= 0:
            cand.status = "blocked"
            cand.reason = "risk_blocked_size_cap"
            continue
        spread, placed = place_spread(ib, cand, contracts, config, today, dry)
        if not placed:
            if not dry:
                order_rejected = True
            continue
        open_spreads.append(spread)
        executed += 1
        if cand.credit > 0:
            credit_added += cand.credit * contracts * config.multiplier
        margin = candidate_margin_per_contract(cand, config) * contracts
        sleeve = cand.sleeve or "core"
        sleeve_margin_used[sleeve] = sleeve_margin_used.get(sleeve, 0.0) + margin
        margin_added += margin
        log_event(today, {"event": "entry", "side": cand.side, "sleeve": sleeve,
                          "short_strike": cand.short_strike, "long_strike": cand.long_strike,
                          "contracts": contracts, "credit": round(cand.credit, 2),
                          "score": round(cand.score, 3), "dry": dry})
        print(f"[{now.isoformat()}] ENTRY {cand.side} {cand.short_strike}/{cand.long_strike} "
              f"x{contracts} credit={cand.credit:.2f} score={cand.score:.2f}"
              f"{' (dry)' if dry else ''}")

    if not skip_reason and executed == 0 and selected:
        skip_reason = "order_rejected" if order_rejected else "risk_blocked_size_cap"

    summary = _summarize_tranche_from_records(
        now,
        entries_halted,
        signal,
        base_contracts,
        records,
        skip_reason=skip_reason,
        executed_count=executed,
    )
    tranche_row = _tranche_log_dict(summary)
    tranche_row["executed"] = executed
    log_tranche(today, tranche_row)
    print(f"[{now.isoformat()}] {_format_tranche_console(summary, executed)}")

    return executed, credit_added, margin_added, portfolio_margin_used + margin_added


def run(live: LiveConfig = ACTIVE) -> None:
    if live.mode not in ("dry", "paper", "live"):
        raise SystemExit(f"invalid mode {live.mode!r}; choose dry|paper|live")
    if live.mode == "live" and not live.allow_live:
        raise SystemExit("live mode requires allow_live=True in LiveConfig (safety interlock).")

    config, sizing_schedule = resolve_strategy_config(live)
    today = datetime.now().date().isoformat()
    dry = live.mode == "dry"

    print(f"[{datetime.now().isoformat()}] mode={live.mode} profile={live.profile} "
          f"scheme={live.sizing_scheme or 'flat'} equity=${live.account_equity:,.0f} "
          f"baseline_contracts={config.baseline_contracts} stop={config.stop_multiple}x/"
          f"{config.stop_confirmation_count}bar halt={config.daily_loss_limit_pct:.2%} "
          f"flatten={config.flatten_loss_limit_pct or config.daily_loss_limit_pct:.2%}")
    write_session_snapshot(today, live, config, live.sizing_scheme)
    log_event(today, {"event": "session_start", "mode": live.mode, "profile": live.profile,
                      "sizing_scheme": live.sizing_scheme, "baseline_contracts": config.baseline_contracts})

    ib = None
    provider: SignalProvider
    connect_ib = (live.mode in ("paper", "live")) or (dry and live.dry_with_ib)
    if not connect_ib:
        if dry:
            print("Dry mode without IB: logging intended trades with neutral signals.")
        provider = _NeutralProvider()
    else:
        if not HAS_IB:
            raise SystemExit("ib_insync required for IB connection: pip install ib_insync")
        ib = IB()
        ib_log = setup_ib_logging(today)
        port = live.port or (7497 if live.mode == "paper" else 7496)
        ib.connect(live.host, port, clientId=live.client_id)
        ib.reqMarketDataType(live.market_data_type)
        register_ib_error_handler(ib, today)
        print(f"IB connected (port {port}, market_data_type={live.market_data_type} "
              f"{'live' if live.market_data_type == 1 else 'delayed' if live.market_data_type == 3 else 'other'})")
        print(f"IB messages -> {ib_log} and data/live/{today}/ib_errors.jsonl")
        print(f"Tranche diagnostics -> data/live/{today}/tranches.jsonl")
        provider = IBSignalProvider(
            ib,
            live,
            config,
            Path(live.baselines_path) if live.baselines_path else None,
        )

    open_spreads: List[OpenSpread] = []
    gross_credit_sold = 0.0
    daily_credit_cap = config.account_equity * config.daily_credit_cap_pct
    # Two-threshold governor: halt NEW entries at daily_loss_limit_pct, force-
    # flatten OPEN positions at the (deeper) flatten_loss_limit_pct when set.
    halt_limit = -config.account_equity * config.daily_loss_limit_pct
    flatten_pct = config.flatten_loss_limit_pct or config.daily_loss_limit_pct
    flatten_limit = -config.account_equity * flatten_pct
    portfolio_margin_used = 0.0
    sleeve_margin_used = {"core": 0.0, "exploratory": 0.0, "condor": 0.0,
                          "one_dte": 0.0, "trend_debit": 0.0, "long_put_hedge": 0.0}
    entries_halted = False
    flattened = False
    traded_tranches: set = set()

    try:
        while datetime.now().time() <= config.force_flat_time:
            now = datetime.now()
            quotes, signal = provider.fetch(now)

            # 1. Synthetic short-leg stops (confirmation-gated), keep long wings.
            manage_stops(ib, open_spreads, quotes, config, today, dry)

            # 2. Governor: halt then (deeper) flatten on marked loss.
            marked = _mark_book(open_spreads, quotes, config)
            if not entries_halted and marked <= halt_limit:
                entries_halted = True
                print(f"[{now.isoformat()}] HALT new entries (marked ${marked:,.0f} <= ${halt_limit:,.0f}).")
                log_event(today, {"event": "halt_entries", "marked_pnl": round(marked, 2)})
            if (config.flatten_on_daily_loss and not flattened and marked <= flatten_limit):
                flattened = True
                entries_halted = True
                print(f"[{now.isoformat()}] FLATTEN (marked ${marked:,.0f} <= ${flatten_limit:,.0f}).")
                flatten_all(ib, open_spreads, today, dry)
                log_event(today, {"event": "flatten", "marked_pnl": round(marked, 2)})

            # 3. Entries on tranche boundaries (once per tranche).
            tranche_key = (now.hour, now.minute)
            if is_entry_time(now, config) and tranche_key not in traded_tranches:
                traded_tranches.add(tranche_key)
                executed, credit_added, _, portfolio_margin_used = _process_tranche(
                    now=now,
                    today=today,
                    quotes=quotes,
                    signal=signal,
                    config=config,
                    sizing_schedule=sizing_schedule,
                    live=live,
                    ib=ib,
                    dry=dry,
                    entries_halted=entries_halted,
                    open_spreads=open_spreads,
                    gross_credit_sold=gross_credit_sold,
                    daily_credit_cap=daily_credit_cap,
                    sleeve_margin_used=sleeve_margin_used,
                    portfolio_margin_used=portfolio_margin_used,
                )
                gross_credit_sold += credit_added

            _time.sleep(live.poll_seconds)
    except Exception as exc:  # safety: never leave positions unmanaged
        print(f"[{datetime.now().isoformat()}] ERROR: {exc!r} -- flattening and exiting.")
        log_event(today, {"event": "error_flatten", "error": repr(exc)})
        if not dry:
            flatten_all(ib, [s for s in open_spreads if not s.closed], today, dry)
        raise
    finally:
        if ib is not None:
            ib.disconnect()

    # End of day: SPXW 0DTE is cash-settled on the close, so open defined-risk
    # spreads are left to settle (matches the backtest's settle-at-close). Only
    # the governor or an error flattens early.
    log_event(today, {"event": "session_end", "spreads": len(open_spreads),
                      "stopped": sum(1 for s in open_spreads if s.stopped),
                      "flattened": flattened, "gross_credit_sold": round(gross_credit_sold, 2)})
    print(f"[{datetime.now().isoformat()}] session end. spreads={len(open_spreads)} "
          f"stopped={sum(1 for s in open_spreads if s.stopped)} gross_credit=${gross_credit_sold:,.0f}")


def _size_with_caps(cand: CandidateRecord, config: StrategyConfig, gross_credit_sold: float,
                    daily_credit_cap: float, sleeve_margin_used: dict, portfolio_margin_used: float) -> int:
    import math
    contracts = cand.contracts
    if cand.credit > 0:
        remaining = daily_credit_cap - gross_credit_sold
        contracts = min(contracts, math.floor(remaining / (cand.credit * config.multiplier)))
    if config.use_portfolio_allocator:
        mpc = candidate_margin_per_contract(cand, config)
        if mpc > 0:
            sleeve = cand.sleeve or "core"
            from mbh_simulator import sleeve_margin_budget_pct
            sleeve_budget = config.account_equity * sleeve_margin_budget_pct(sleeve, config)
            port_budget = config.account_equity * config.portfolio_margin_budget_pct
            by_sleeve = math.floor(max(sleeve_budget - sleeve_margin_used.get(sleeve, 0.0), 0.0) / mpc)
            by_port = math.floor(max(port_budget - portfolio_margin_used, 0.0) / mpc)
            contracts = min(contracts, by_sleeve, by_port)
    return max(0, contracts)


def _mark_book(open_spreads: Sequence[OpenSpread], quotes: Sequence[OptionQuote], config: StrategyConfig) -> float:
    if not quotes:
        return 0.0
    lookup = {(q.option_type, q.strike): q for q in quotes}
    total = 0.0
    for spread in open_spreads:
        if spread.closed:
            continue
        cand = spread.candidate
        opt_type = _candidate_option_type(cand)
        lq = lookup.get((opt_type, cand.long_strike))
        if lq is None:
            continue
        if spread.stopped:
            # Short already bought back; only the long wing marks from here.
            per_contract = spread.short_entry_sell - spread.stop_price - spread.long_entry_buy + lq.bid
        else:
            sq = lookup.get((opt_type, cand.short_strike))
            if sq is None:
                continue
            per_contract = spread.short_entry_sell - sq.ask - spread.long_entry_buy + lq.bid
        total += per_contract * spread.contracts * config.multiplier
    return total


class _NeutralProvider:
    """Dry-run provider with no IB: emits neutral signals so the loop exercises
    end-to-end wiring without market data."""

    def fetch(self, now: datetime):
        return [], SignalSnapshot(timestamp=now)


if __name__ == "__main__":
    run(ACTIVE)
