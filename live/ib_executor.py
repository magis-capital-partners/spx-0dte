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
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Protocol, Sequence, Tuple

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
    select_candidate_entries,
)

try:  # optional dependency; only needed for paper/live modes
    from ib_insync import IB, Index, Option, Contract, ComboLeg, Order, LimitOrder, StopOrder, TagValue
    HAS_IB = True
except Exception:  # pragma: no cover - import guard
    HAS_IB = False

from live_config import ACTIVE, LiveConfig  # noqa: E402
from entry_execution import (  # noqa: E402
    PendingEntry,
    entry_limit_credit,
    entry_quote_block_reason,
    natural_credit,
    poll_pending_entry,
    round_spx_premium,
    work_deadline,
)
from live_features import (  # noqa: E402
    SessionFeatureState,
    compute_raw_features,
    extract_baselines_core,
    raw_to_signal_snapshot,
    split_session_quotes,
    validate_baselines_freshness,
)
from ib_market_data import IBStreamingMarketData  # noqa: E402
from loop_timing import adaptive_sleep_seconds, should_fire_tranche  # noqa: E402
from risk_gates import apply_side_stop_cooldowns, side_stop_cooldown_block_reason  # noqa: E402
from vix_session import (  # noqa: E402
    check_vix_session_allowed,
    format_vix_session_banner,
    resolve_session_vix_open,
    vix_elevated_sizing_multiplier,
)
from session_recovery import (  # noqa: E402
    acquire_executor_lock,
    recover_session_book,
    release_executor_lock,
)
from expiry_calendar import DEFAULT_RULES, is_live_tradable_day, load_era_rules  # noqa: E402
from profiles import schedule_multiplier  # noqa: E402
from strategy_profiles import resolve_strategy_config  # noqa: E402


LIVE_DIR = ROOT / "data" / "live"


def gates_require_signals(config: StrategyConfig) -> bool:
    """True when entry gates need real z-scored trend/skew (not neutral stubs)."""
    return (
        config.candidate_max_adverse_trend < 50.0
        or config.candidate_max_adverse_skew < 50.0
    )


def load_baselines_file(path: Path, max_age_days: int) -> tuple[dict, dict]:
    """Return (full payload, core baselines dict for z-scoring)."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    validate_baselines_freshness(payload, max_age_days)
    return payload, extract_baselines_core(payload)


def check_session_eligible(today: date) -> tuple[bool, str]:
    floor, eras = load_era_rules(DEFAULT_RULES)
    return is_live_tradable_day(today, floor=floor, end=today.isoformat(), eras=eras)


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
    stop_fill_price: Optional[float] = None
    fill_credit: Optional[float] = None

    @property
    def entry_credit(self) -> float:
        return self.short_entry_sell - self.long_entry_buy


# --------------------------------------------------------------------------- #
# IB market data implementation (skeleton)
# --------------------------------------------------------------------------- #
# IB codes that are normal on paper (delayed data, farm OK messages) — file only.
_QUIET_IB_ERROR_CODES = frozenset({10090, 10167, 10197, 202, 2104, 2106, 2158})


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


class IBSignalProvider:
    """Streaming SPXW quotes + z-scored signals (Phases 1–2).

    Chain metadata is discovered once at ``start()``; quotes update via
    ``reqMktData`` tick events. ``fetch(at_tranche=True)`` refreshes next-expiry
    lines for term_ratio.
    """

    def __init__(
        self,
        ib: "IB",
        live: LiveConfig,
        config: StrategyConfig,
        baselines_core: Optional[dict] = None,
    ):
        self.ib = ib
        self.live = live
        self.config = config
        self.baselines = baselines_core
        self._feature_state = SessionFeatureState()
        self._stream = IBStreamingMarketData(ib, live, config)

    @staticmethod
    def load_baselines(path: Path, max_age_days: int) -> tuple[dict, dict]:
        return load_baselines_file(path, max_age_days)

    def start(self) -> None:
        self._stream.start()

    def shutdown(self) -> None:
        self._stream.shutdown()

    def fetch(
        self, now: datetime, *, at_tranche: bool = False
    ) -> Tuple[List[OptionQuote], Optional[SignalSnapshot]]:
        self._stream.maybe_rebalance()
        if not self.live.use_streaming_quotes:
            self._stream._refresh_snapshot_cache()
        if at_tranche:
            self._stream.refresh_next_expiry_at_tranche(now)

        quotes = self._stream.build_option_quotes(now)
        spot = self._stream.spot()
        if spot <= 0:
            return [], None

        signal = self._build_signal(now, quotes, spot, at_tranche=at_tranche)
        return quotes, signal

    def refresh_candidate_legs(self, candidate: CandidateRecord, now: datetime) -> None:
        if not self.live.refresh_legs_before_entry:
            return
        short_q, long_q = self._stream.refresh_spread_legs(
            now,
            candidate.short_type,
            candidate.short_strike,
            candidate.long_strike,
        )
        if short_q is not None:
            candidate.short_quote = short_q
        if long_q is not None:
            candidate.long_quote = long_q
        nat = natural_credit(candidate)
        candidate.credit = nat
        width = abs(candidate.long_strike - candidate.short_strike)
        candidate.credit_to_width = nat / width if width else 0.0

    def leg_quote_ages(self, candidate: CandidateRecord) -> List[Optional[float]]:
        return [
            self._stream.quote_age_seconds(candidate.short_type, candidate.short_strike),
            self._stream.quote_age_seconds(candidate.short_type, candidate.long_strike),
        ]

    def _build_signal(
        self,
        now: datetime,
        quotes: Sequence[OptionQuote],
        spot: float,
        *,
        at_tranche: bool = False,
    ) -> Optional[SignalSnapshot]:
        if self.baselines is None:
            return SignalSnapshot(timestamp=now)
        session = now.date().isoformat()
        zero_q, _ = split_session_quotes(quotes, session)
        next_q = self._stream.next_expiry_quotes() if at_tranche else None
        raw = compute_raw_features(zero_q, spot, now, self._feature_state, next_expiry_quotes=next_q)
        return raw_to_signal_snapshot(raw, self.baselines, now)


# --------------------------------------------------------------------------- #
# Order construction
# --------------------------------------------------------------------------- #
def _round_spx_premium(price: float) -> float:
    return round_spx_premium(price)


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


def _wait_for_order(ib: "IB", trade, *, timeout_sec: float = 8.0) -> Tuple[str, str]:
    """Poll until filled, rejected, or timed out."""
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


def _wait_for_combo_order(ib: "IB", trade, *, timeout_sec: float = 8.0) -> Tuple[str, str]:
    return _wait_for_order(ib, trade, timeout_sec=timeout_sec)


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


def _same_short_leg(a: CandidateRecord, b: CandidateRecord) -> bool:
    return a.short_type == b.short_type and a.short_strike == b.short_strike


def _cancel_order_by_id(ib: "IB", order_id: Optional[int]) -> bool:
    if order_id is None:
        return False
    for trade in ib.openTrades():
        if trade.order.orderId == order_id:
            ib.cancelOrder(trade.order)
            ib.sleep(0.25)
            return True
    return False


def _short_leg_contract_key(candidate: CandidateRecord, today: str) -> Tuple[str, str, float, str]:
    expiry = today.replace("-", "")
    right = "P" if candidate.short_type == "PUT" else "C"
    return ("SPX", expiry, float(candidate.short_strike), right)


def _contract_is_short_leg(contract, candidate: CandidateRecord, today: str) -> bool:
    if contract is None or getattr(contract, "secType", "") != "OPT":
        return False
    key = _short_leg_contract_key(candidate, today)
    strike = float(getattr(contract, "strike", 0) or 0)
    return (
        getattr(contract, "symbol", "") == key[0]
        and getattr(contract, "lastTradeDateOrContractMonth", "") == key[1]
        and strike == key[2]
        and getattr(contract, "right", "") == key[3]
    )


def _open_order_status(trade) -> str:
    return (trade.orderStatus.status or "").lower()


def _cancel_open_orders_on_short_leg(
    ib: "IB",
    candidate: CandidateRecord,
    today: str,
) -> int:
    """Cancel every live IB order on this short option (orphan backstops included)."""
    cancelled = 0
    for trade in list(ib.openTrades()):
        if not _contract_is_short_leg(trade.contract, candidate, today):
            continue
        status = _open_order_status(trade)
        if status in {"filled", "cancelled", "inactive", "apicancelled"}:
            continue
        ib.cancelOrder(trade.order)
        cancelled += 1
    if cancelled:
        ib.sleep(0.5)
    return cancelled


def cancel_stop_backstop(
    ib: "IB",
    spread: OpenSpread,
    today: str,
    *,
    dry: bool = False,
    reason: str = "",
) -> None:
    """Cancel the optional native STP backstop on the short leg."""
    if dry or not HAS_IB or spread.stop_order_id is None:
        return
    order_id = spread.stop_order_id
    if _cancel_order_by_id(ib, order_id):
        spread.stop_order_id = None
        log_event(today, {
            "event": "stop_backstop_cancelled",
            "short_strike": spread.candidate.short_strike,
            "order_id": order_id,
            "reason": reason or "unspecified",
        })


def clear_short_leg_backstops(
    ib: "IB",
    candidate: CandidateRecord,
    open_spreads: Sequence[OpenSpread],
    today: str,
    *,
    dry: bool,
    reason: str,
) -> None:
    """Drop open orders on this short strike before a conflicting IB action.

    IB rejects error 201 when a BUY stop and a new SELL (combo open) coexist on
    the same US option contract. Clears tracked backstops and any orphan orders
    left in TWS from a prior session.
    """
    for spread in open_spreads:
        if spread.closed:
            continue
        if _same_short_leg(spread.candidate, candidate):
            cancel_stop_backstop(ib, spread, today, dry=dry, reason=reason)

    if dry or not HAS_IB:
        return

    cancelled = _cancel_open_orders_on_short_leg(ib, candidate, today)
    if cancelled:
        for spread in open_spreads:
            if not spread.closed and _same_short_leg(spread.candidate, candidate):
                spread.stop_order_id = None
        log_event(today, {
            "event": "short_leg_orders_cancelled",
            "short_strike": candidate.short_strike,
            "short_type": candidate.short_type,
            "count": cancelled,
            "reason": reason or "unspecified",
        })


def cancel_pending_entry(
    ib: "IB",
    pending: Optional[PendingEntry],
    today: str,
    *,
    reason: str,
    dry: bool,
) -> None:
    if pending is None or dry or not HAS_IB:
        return
    try:
        ib.cancelOrder(pending.trade.order)
        ib.sleep(0.25)
    except Exception:
        pass
    log_event(today, {
        "event": "entry_cancelled",
        "side": pending.candidate.side,
        "short_strike": pending.candidate.short_strike,
        "long_strike": pending.candidate.long_strike,
        "reason": reason,
        "limit_credit": round(pending.limit_credit, 2),
    })
    if reason in {"new_tranche", "flatten", "error"}:
        log_event(today, {
            "event": "order_rejected",
            "side": pending.candidate.side,
            "short_strike": pending.candidate.short_strike,
            "long_strike": pending.candidate.long_strike,
            "contracts": pending.contracts,
            "natural_credit": round(pending.natural_credit, 2),
            "limit_credit": round(pending.limit_credit, 2),
            "credit": round(pending.limit_credit, 2),
            "status": "Cancelled",
            "reason": f"entry_cancelled_{reason}",
        })


def submit_spread_entry(
    ib: "IB",
    candidate: CandidateRecord,
    contracts: int,
    config: StrategyConfig,
    today: str,
    dry: bool,
    live: LiveConfig,
    open_spreads: Sequence[OpenSpread],
    *,
    now: datetime,
    provider: Optional["IBSignalProvider"] = None,
) -> Tuple[Optional[OpenSpread], Optional[PendingEntry], str]:
    """Validate quotes and submit a working combo limit (non-blocking when not dry)."""
    if provider is not None:
        provider.refresh_candidate_legs(candidate, now)
    leg_ages = provider.leg_quote_ages(candidate) if provider is not None else None
    block = entry_quote_block_reason(candidate, live, leg_ages=leg_ages)
    if block:
        return None, None, block

    nat = natural_credit(candidate)
    limit = entry_limit_credit(nat, live)
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
        spread.fill_credit = limit
        return spread, None, ""

    clear_short_leg_backstops(
        ib, candidate, open_spreads, today, dry=dry, reason="pre_entry",
    )
    bag, _short_leg_opt = build_combo(ib, candidate, today)
    combo_order = LimitOrder("BUY", contracts, -limit)
    combo_order.tif = "DAY"
    trade = ib.placeOrder(bag, combo_order)
    spread.combo_order_id = trade.order.orderId

    submitted = now
    pending = PendingEntry(
        spread=spread,
        trade=trade,
        candidate=candidate,
        contracts=contracts,
        natural_credit=nat,
        limit_credit=limit,
        submitted_at=submitted,
        work_until=work_deadline(submitted, live, config.entry_interval_minutes),
        next_ladder_at=submitted + timedelta(seconds=live.entry_ladder_interval_seconds),
        tranche_time=now,
        sleeve=candidate.sleeve or "core",
        score=candidate.score,
    )
    log_event(today, {
        "event": "entry_submitted",
        "side": candidate.side,
        "short_strike": candidate.short_strike,
        "long_strike": candidate.long_strike,
        "contracts": contracts,
        "natural_credit": round(nat, 2),
        "limit_credit": round(limit, 2),
        "score": round(candidate.score, 3),
    })
    print(
        f"[{now.isoformat()}] ENTRY working {candidate.side} "
        f"{candidate.short_strike}/{candidate.long_strike} x{contracts} "
        f"natural={nat:.2f} limit={limit:.2f}"
    )
    return None, pending, ""


def apply_pending_resolution(
    event: dict,
    pending: PendingEntry,
    *,
    open_spreads: List[OpenSpread],
    config: StrategyConfig,
    sleeve_margin_used: dict,
    portfolio_margin_used: float,
) -> Tuple[int, float, float, float]:
    """Apply a filled or rejected pending entry."""
    if event.get("event") == "entry":
        spread = pending.spread
        fill_credit = float(event.get("credit", pending.limit_credit))
        spread.fill_credit = fill_credit
        open_spreads.append(spread)
        contracts = pending.contracts
        credit_added = fill_credit * contracts * config.multiplier
        margin = candidate_margin_per_contract(pending.candidate, config) * contracts
        sleeve = pending.sleeve or "core"
        sleeve_margin_used[sleeve] = sleeve_margin_used.get(sleeve, 0.0) + margin
        print(
            f"[{datetime.now().isoformat()}] ENTRY filled {pending.candidate.side} "
            f"{pending.candidate.short_strike}/{pending.candidate.long_strike} "
            f"x{contracts} fill={fill_credit:.2f} "
            f"(natural={event.get('natural_credit')} slippage={event.get('fill_slippage')})"
        )
        return 1, credit_added, margin, portfolio_margin_used + margin

    print(
        f"[{datetime.now().isoformat()}] ENTRY failed {pending.candidate.side} "
        f"{pending.candidate.short_strike}/{pending.candidate.long_strike} "
        f"reason={event.get('reason')}"
    )
    return 0, 0.0, 0.0, portfolio_margin_used


def place_spread(
    ib: "IB",
    candidate: CandidateRecord,
    contracts: int,
    config: StrategyConfig,
    today: str,
    dry: bool,
    live: LiveConfig,
    open_spreads: Sequence[OpenSpread] = (),
    *,
    now: Optional[datetime] = None,
    provider: Optional["IBSignalProvider"] = None,
) -> Tuple[OpenSpread, bool]:
    """Dry-run helper and legacy sync wrapper (dry mode only)."""
    spread, pending, block = submit_spread_entry(
        ib,
        candidate,
        contracts,
        config,
        today,
        dry,
        live,
        open_spreads,
        now=now or datetime.now(),
        provider=provider,
    )
    if block:
        return OpenSpread(
            candidate=candidate,
            contracts=contracts,
            short_entry_sell=0.0,
            long_entry_buy=0.0,
            stop_price=0.0,
        ), False
    if dry and spread is not None:
        return spread, True
    return spread or pending.spread, pending is not None


def _stop_limit_price(ask: float, live: LiveConfig) -> float:
    capped = max(ask * (1.0 + live.stop_limit_slippage_pct), ask + live.stop_limit_slippage_abs)
    return _round_spx_premium(capped)


def _buy_short_leg_stop(
    ib: "IB",
    spread: OpenSpread,
    candidate: CandidateRecord,
    today: str,
    short_ask: float,
    live: LiveConfig,
) -> Tuple[bool, float]:
    """Phase 4: limit at ask + buffer, escalate to MKT if unfilled."""
    clear_short_leg_backstops(
        ib, candidate, [spread], today, dry=False, reason="synthetic_stop",
    )
    short_opt = _short_option(ib, candidate, today)
    limit_px = _stop_limit_price(short_ask, live)
    limit_order = LimitOrder("BUY", spread.contracts, limit_px)
    limit_order.tif = "DAY"
    trade = ib.placeOrder(short_opt, limit_order)
    state, reason = _wait_for_order(ib, trade, timeout_sec=live.stop_limit_timeout_seconds)
    if state == "filled":
        fill = float(trade.orderStatus.avgFillPrice or limit_px)
        return True, fill
    if state == "pending":
        ib.cancelOrder(trade.order)
        ib.sleep(0.25)
    print(f"[{datetime.now().isoformat()}] STOP limit unfilled @ {limit_px:.2f} ({reason}) — MKT fallback")
    mkt = ib.placeOrder(
        short_opt,
        Order(action="BUY", totalQuantity=spread.contracts, orderType="MKT"),
    )
    mkt_state, _ = _wait_for_order(ib, mkt, timeout_sec=5.0)
    if mkt_state == "filled":
        fill = float(mkt.orderStatus.avgFillPrice or short_ask)
        return True, fill
    return False, short_ask


def manage_stops(
    ib: "IB",
    open_spreads: Sequence[OpenSpread],
    quotes: Sequence[OptionQuote],
    config: StrategyConfig,
    today: str,
    dry: bool,
    live: LiveConfig,
) -> List[OpenSpread]:
    """Short-leg stop with N-bar confirmation; limit buy then MKT fallback."""
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
        fill_px = sq.ask
        if not dry and HAS_IB and ib is not None:
            ok, fill_px = _buy_short_leg_stop(
                ib, spread, spread.candidate, today, sq.ask, live
            )
            if not ok:
                spread.stopped = False
                spread.stop_confirm_count = 0
                continue
        spread.stop_fill_price = fill_px
        newly_stopped.append(spread)
        log_event(
            today,
            {
                "event": "stop",
                "side": spread.candidate.side,
                "short_strike": spread.candidate.short_strike,
                "stop_price": round(spread.stop_price, 2),
                "short_ask": round(sq.ask, 2),
                "stop_fill": round(fill_px, 2),
                "contracts": spread.contracts,
                "dry": dry,
            },
        )
        print(
            f"[{datetime.now().isoformat()}] STOP short {spread.candidate.short_strike} "
            f"ask={sq.ask:.2f}>={spread.stop_price:.2f} fill={fill_px:.2f} (keep long wing)"
            f"{' (dry)' if dry else ''}"
        )
    return newly_stopped


def flatten_all(ib: "IB", open_spreads: Sequence[OpenSpread], today: str, dry: bool) -> None:
    """Flatten governor: close every open spread immediately (both legs)."""
    for spread in open_spreads:
        if spread.closed:
            continue
        if not dry and HAS_IB:
            clear_short_leg_backstops(
                ib, spread.candidate, [spread], today, dry=dry, reason="flatten",
            )
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
def _tranche_base_contracts(
    config: StrategyConfig,
    sizing_schedule,
    now: datetime,
    *,
    vix_sizing_multiplier: float = 1.0,
) -> int:
    """Flat baseline, optionally reshaped by time-of-day and VIX elevated band."""
    base = config.baseline_contracts
    if sizing_schedule:
        base = round(base * schedule_multiplier(now.time(), sizing_schedule))
    if vix_sizing_multiplier != 1.0:
        base = round(base * vix_sizing_multiplier)
    return max(0, base)


def _finalize_spread_entry(
    ib: "IB",
    spread: OpenSpread,
    candidate: CandidateRecord,
    contracts: int,
    config: StrategyConfig,
    today: str,
    live: LiveConfig,
    short_leg_opt,
) -> None:
    if live.use_native_stop_backstop and HAS_IB and ib is not None:
        backstop = _round_spx_premium(spread.stop_price * 1.5)
        if backstop > 0:
            stop_order = StopOrder("BUY", contracts, backstop)
            stop_order.tif = "DAY"
            stop_trade = ib.placeOrder(short_leg_opt, stop_order)
            spread.stop_order_id = stop_trade.order.orderId
            stop_reason = _trade_rejection_reason(stop_trade)
            if stop_reason:
                ib.cancelOrder(stop_trade.order)
                spread.stop_order_id = None
                log_event(today, {
                    "event": "stop_backstop_rejected",
                    "short_strike": candidate.short_strike,
                    "backstop": backstop,
                    "reason": stop_reason,
                })


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
    provider: Optional["IBSignalProvider"] = None,
    pending_entry: Optional[PendingEntry] = None,
    side_stop_cooldown_until: Optional[Dict[str, datetime]] = None,
    vix_sizing_multiplier: float = 1.0,
) -> Tuple[int, float, float, float, Optional[PendingEntry], str]:
    """Evaluate one entry tranche; log diagnostics; submit any selected spreads."""
    if pending_entry is not None and not dry:
        cancel_pending_entry(ib, pending_entry, today, reason="new_tranche", dry=dry)
        pending_entry = None

    base_contracts = _tranche_base_contracts(
        config, sizing_schedule, now, vix_sizing_multiplier=vix_sizing_multiplier,
    )
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
    entry_working = False
    new_pending: Optional[PendingEntry] = None

    for cand in selected:
        if cand.short_quote is None or cand.long_quote is None:
            continue
        cooldown_reason = ""
        if side_stop_cooldown_until is not None:
            cooldown_reason = side_stop_cooldown_block_reason(
                cand.side, now, config, side_stop_cooldown_until
            )
        if cooldown_reason:
            cand.status = "blocked"
            cand.reason = cooldown_reason
            log_event(today, {
                "event": "entry_blocked",
                "side": cand.side,
                "short_strike": cand.short_strike,
                "long_strike": cand.long_strike,
                "reason": cooldown_reason,
            })
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

        spread, pending, block = submit_spread_entry(
            ib,
            cand,
            contracts,
            config,
            today,
            dry,
            live,
            open_spreads,
            now=now,
            provider=provider,
        )
        if block:
            cand.status = "blocked"
            cand.reason = block
            order_rejected = True
            log_event(today, {
                "event": "entry_blocked",
                "side": cand.side,
                "short_strike": cand.short_strike,
                "long_strike": cand.long_strike,
                "reason": block,
            })
            continue
        if dry and spread is not None:
            open_spreads.append(spread)
            executed += 1
            fill_credit = spread.fill_credit or cand.credit
            if fill_credit > 0:
                credit_added += fill_credit * contracts * config.multiplier
            margin = candidate_margin_per_contract(cand, config) * contracts
            sleeve = cand.sleeve or "core"
            sleeve_margin_used[sleeve] = sleeve_margin_used.get(sleeve, 0.0) + margin
            margin_added += margin
            log_event(today, {
                "event": "entry",
                "side": cand.side,
                "sleeve": sleeve,
                "short_strike": cand.short_strike,
                "long_strike": cand.long_strike,
                "contracts": contracts,
                "credit": round(fill_credit, 2),
                "score": round(cand.score, 3),
                "dry": True,
            })
            print(
                f"[{now.isoformat()}] ENTRY {cand.side} {cand.short_strike}/{cand.long_strike} "
                f"x{contracts} credit={fill_credit:.2f} score={cand.score:.2f} (dry)"
            )
            continue
        if pending is not None:
            new_pending = pending
            entry_working = True
            cand.status = "selected"
            break

    if not skip_reason and executed == 0 and not entry_working and selected:
        if any(r.status == "blocked" for r in selected):
            skip_reason = "entry_quote_blocked" if not order_rejected else "order_rejected"
        else:
            skip_reason = "order_rejected" if order_rejected else "risk_blocked_size_cap"
    elif entry_working:
        skip_reason = "entry_working"

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
    tranche_row["entry_working"] = entry_working
    log_tranche(today, tranche_row)
    print(f"[{now.isoformat()}] {_format_tranche_console(summary, executed)}")

    return executed, credit_added, margin_added, portfolio_margin_used + margin_added, new_pending, skip_reason


def run(live: LiveConfig = ACTIVE) -> None:
    if live.mode not in ("dry", "paper", "live"):
        raise SystemExit(f"invalid mode {live.mode!r}; choose dry|paper|live")
    if live.mode == "live" and not live.allow_live:
        raise SystemExit("live mode requires allow_live=True in LiveConfig (safety interlock).")

    config, sizing_schedule = resolve_strategy_config(live)
    today_date = datetime.now().date()
    today = today_date.isoformat()
    dry = live.mode == "dry"
    needs_signals = gates_require_signals(config)

    # Single-instance lock before any IB work so two executors cannot share a day.
    lock_path = acquire_executor_lock(today)
    print(f"[{datetime.now().isoformat()}] executor lock acquired → {lock_path}")

    eligible, skip_reason = check_session_eligible(today_date)
    if not eligible:
        release_executor_lock(lock_path)
        raise SystemExit(
            f"{today} is not an eligible SPXW session ({skip_reason}) — "
            "backtest CAGR uses eligible-calendar days only."
        )

    vix_open, vix_source = resolve_session_vix_open(today, live)
    vix_blocked, vix_skip_reason = check_vix_session_allowed(vix_open, live)
    if vix_blocked:
        release_executor_lock(lock_path)
        vix_txt = f"{vix_open:.2f}" if vix_open is not None else "n/a"
        raise SystemExit(
            f"{today} skipped — {vix_skip_reason} (VIX open={vix_txt}, source={vix_source}). "
            f"Threshold is >{live.vix_skip_open_above:.0f}. "
            "Refresh calendar: python scripts/download_vix_daily.py"
        )
    vix_sizing_mult = vix_elevated_sizing_multiplier(vix_open, live)

    baselines_core: Optional[dict] = None
    if needs_signals and live.require_baselines:
        if not live.baselines_path:
            raise SystemExit(
                "profile requires signal baselines — set baselines_path in LiveConfig "
                "and run scripts/refresh_live_baselines.py"
            )
        baseline_path = ROOT / live.baselines_path
        if not baseline_path.is_file():
            raise SystemExit(
                f"baselines not found at {baseline_path} — run scripts/refresh_live_baselines.py"
            )
        _, baselines_core = load_baselines_file(baseline_path, live.baselines_max_age_days)

    print(f"[{datetime.now().isoformat()}] mode={live.mode} profile={live.profile} "
          f"scheme={live.sizing_scheme or 'flat'} equity=${live.account_equity:,.0f} "
          f"baseline_contracts={config.baseline_contracts} "
          f"wings=put{config.put_wing_width:.0f}/call{config.call_wing_width:.0f} "
          f"gates=trend<={config.candidate_max_adverse_trend}/skew<={config.candidate_max_adverse_skew} "
          f"stop={config.stop_multiple}x/{config.stop_confirmation_count}bar "
          f"side_cooldown={config.same_side_stop_cooldown_minutes}min "
          f"halt={config.daily_loss_limit_pct:.2%} "
          f"flatten={config.flatten_loss_limit_pct or config.daily_loss_limit_pct:.2%} "
          f"polling={'adaptive' if live.use_adaptive_polling else f'{live.poll_seconds}s'} "
          f"quotes={'stream' if live.use_streaming_quotes else 'snapshot'} "
          f"entry=concession ${live.entry_limit_concession:.2f} "
          f"work={live.entry_work_seconds:.0f}s ladder={live.entry_ladder_step:.2f} "
          f"{format_vix_session_banner(vix_open, vix_source=vix_source, skip_reason='', sizing_multiplier=vix_sizing_mult, live=live)}")
    write_session_snapshot(today, live, config, live.sizing_scheme)
    log_event(today, {"event": "session_start", "mode": live.mode, "profile": live.profile,
                      "sizing_scheme": live.sizing_scheme, "baseline_contracts": config.baseline_contracts,
                      "eligible": True, "vix_open": vix_open, "vix_source": vix_source,
                      "vix_sizing_multiplier": vix_sizing_mult})

    ib = None
    provider: Optional[SignalProvider] = None
    open_spreads: List[OpenSpread] = []
    gross_credit_sold = 0.0
    pending_entry: Optional[PendingEntry] = None
    flattened = False
    last_quotes: List[OptionQuote] = []
    last_marked_pnl: float = 0.0
    connect_ib = (live.mode in ("paper", "live")) or (dry and live.dry_with_ib)

    try:
        if not connect_ib:
            if dry:
                if needs_signals:
                    raise SystemExit(
                        "Dry mode without IB cannot run gated profile — set dry_with_ib=True "
                        "or use profile 3d_flatten_3_5 (gates off)."
                    )
                print("Dry mode without IB: logging intended trades with neutral signals.")
            provider = _NeutralProvider()
        else:
            if not HAS_IB:
                raise SystemExit("ib_insync required for IB connection: pip install ib_insync")
            if needs_signals and baselines_core is None:
                raise SystemExit("gated profile requires baselines — run scripts/refresh_live_baselines.py")
            ib = IB()
            ib_log = setup_ib_logging(today)
            port = live.port or (7497 if live.mode == "paper" else 7496)
            ib.connect(live.host, port, clientId=live.client_id)
            register_ib_error_handler(ib, today)
            print(f"IB connected (port {port}, market_data_type={live.market_data_type} requested)")
            print(f"IB messages -> {ib_log} and data/live/{today}/ib_errors.jsonl")
            print(f"Tranche diagnostics -> data/live/{today}/tranches.jsonl")
            if baselines_core is not None:
                print(f"Signal baselines -> {live.baselines_path}")
            provider = IBSignalProvider(ib, live, config, baselines_core=baselines_core)
            provider.start()

        # Rebuild open book from today's fills and verify against IB positions.
        recovered = recover_session_book(
            today=today,
            stop_multiple=config.stop_multiple,
            OpenSpread=OpenSpread,
            CandidateRecord=CandidateRecord,
            ib=ib if (ib is not None and not dry) else None,
            fail_on_unmatched=True,
        )
        open_spreads = list(recovered.spreads)
        gross_credit_sold = float(recovered.gross_credit_sold)
        for warn in recovered.warnings:
            print(f"[{datetime.now().isoformat()}] RECOVERY WARN: {warn}")
        if open_spreads or recovered.source_entries:
            print(
                f"[{datetime.now().isoformat()}] recovered "
                f"{len(open_spreads)} open spread(s) from {recovered.source_entries} fill event(s); "
                f"gross_credit=${gross_credit_sold:,.0f}; ib_matched_legs={recovered.ib_matched_legs}"
            )
            log_event(today, {
                "event": "session_recovered",
                "open_spreads": len(open_spreads),
                "source_entries": recovered.source_entries,
                "gross_credit_sold": round(gross_credit_sold, 2),
                "ib_matched_legs": recovered.ib_matched_legs,
                "warnings": recovered.warnings,
            })

        daily_credit_cap = config.account_equity * config.daily_credit_cap_pct
        # Two-threshold governor: halt NEW entries at daily_loss_limit_pct, force-
        # flatten OPEN positions at the (deeper) flatten_loss_limit_pct when set.
        halt_limit = -config.account_equity * config.daily_loss_limit_pct
        flatten_pct = config.flatten_loss_limit_pct or config.daily_loss_limit_pct
        flatten_limit = -config.account_equity * flatten_pct
        portfolio_margin_used = 0.0
        sleeve_margin_used = {"core": 0.0, "exploratory": 0.0, "condor": 0.0,
                              "one_dte": 0.0, "trend_debit": 0.0, "long_put_hedge": 0.0}
        # Recreate sleeve/portfolio margin used from recovered open risk.
        for spread in open_spreads:
            mpc = candidate_margin_per_contract(spread.candidate, config) * spread.contracts
            sleeve = spread.candidate.sleeve or "core"
            sleeve_margin_used[sleeve] = sleeve_margin_used.get(sleeve, 0.0) + mpc
            portfolio_margin_used += mpc
        entries_halted = False
        flattened = False
        traded_tranches: set = set()
        pending_entry = None
        side_stop_cooldown_until: Dict[str, datetime] = {}
        last_quotes = []
        last_marked_pnl = 0.0
        ib_provider: Optional[IBSignalProvider] = None
        if isinstance(provider, IBSignalProvider):
            ib_provider = provider

        try:
            while datetime.now().time() <= config.force_flat_time:
                now = datetime.now()
                at_tranche = should_fire_tranche(now, config, traded_tranches)
                quotes, signal = provider.fetch(now, at_tranche=at_tranche)
                last_quotes = list(quotes)

                if pending_entry is not None and ib is not None and not dry:
                    active_pending = pending_entry
                    pending_entry, resolution = poll_pending_entry(
                        ib, active_pending, live, today, now, log_event=log_event,
                    )
                    if resolution is not None:
                        log_event(today, resolution)
                        _, credit_added, _, portfolio_margin_used = apply_pending_resolution(
                            resolution,
                            active_pending,
                            open_spreads=open_spreads,
                            config=config,
                            sleeve_margin_used=sleeve_margin_used,
                            portfolio_margin_used=portfolio_margin_used,
                        )
                        gross_credit_sold += credit_added

                newly_stopped = manage_stops(ib, open_spreads, quotes, config, today, dry, live)
                if newly_stopped:
                    apply_side_stop_cooldowns(
                        newly_stopped,
                        config=config,
                        now=now,
                        side_stop_cooldown_until=side_stop_cooldown_until,
                    )
                    for spread in newly_stopped:
                        log_event(today, {
                            "event": "side_stop_cooldown_start",
                            "side": spread.candidate.side,
                            "minutes": config.same_side_stop_cooldown_minutes,
                            "until": (
                                side_stop_cooldown_until[spread.candidate.side].isoformat()
                                if spread.candidate.side in side_stop_cooldown_until
                                else None
                            ),
                        })

                marked = _mark_book(open_spreads, quotes, config)
                last_marked_pnl = marked
                if not entries_halted and marked <= halt_limit:
                    entries_halted = True
                    print(f"[{now.isoformat()}] HALT new entries (marked ${marked:,.0f} <= ${halt_limit:,.0f}).")
                    log_event(today, {"event": "halt_entries", "marked_pnl": round(marked, 2)})
                if config.flatten_on_daily_loss and not flattened and marked <= flatten_limit:
                    flattened = True
                    entries_halted = True
                    print(f"[{now.isoformat()}] FLATTEN (marked ${marked:,.0f} <= ${flatten_limit:,.0f}).")
                    cancel_pending_entry(ib, pending_entry, today, reason="flatten", dry=dry)
                    pending_entry = None
                    flatten_all(ib, open_spreads, today, dry)
                    log_event(today, {"event": "flatten", "marked_pnl": round(marked, 2)})

                if at_tranche:
                    tranche_key = (now.hour, now.minute)
                    traded_tranches.add(tranche_key)
                    executed, credit_added, _, portfolio_margin_used, new_pending, _ = _process_tranche(
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
                        provider=ib_provider,
                        pending_entry=pending_entry,
                        side_stop_cooldown_until=side_stop_cooldown_until,
                        vix_sizing_multiplier=vix_sizing_mult,
                    )
                    gross_credit_sold += credit_added
                    pending_entry = new_pending

                sleep_for = adaptive_sleep_seconds(
                    live=live,
                    now=now,
                    open_spreads=open_spreads,
                    quotes=quotes,
                    config=config,
                )
                if pending_entry is not None:
                    sleep_for = min(sleep_for, live.entry_poll_seconds)
                if sleep_for > 0:
                    self_sleep = sleep_for
                    if HAS_IB and ib is not None:
                        ib.sleep(self_sleep)
                    else:
                        _time.sleep(self_sleep)
        except Exception as exc:
            print(f"[{datetime.now().isoformat()}] ERROR: {exc!r} -- flattening and exiting.")
            log_event(today, {"event": "error_flatten", "error": repr(exc)})
            cancel_pending_entry(ib, pending_entry, today, reason="error", dry=dry)
            if not dry:
                flatten_all(ib, [s for s in open_spreads if not s.closed], today, dry)
            raise

        # End of day: SPXW 0DTE is cash-settled on the close, so open defined-risk
        # spreads are left to settle (matches the backtest's settle-at-close). Only
        # the governor or an error flattens early.
        if last_quotes:
            last_marked_pnl = _mark_book(open_spreads, last_quotes, config)
        log_event(today, {
            "event": "session_end",
            "spreads": len(open_spreads),
            "stopped": sum(1 for s in open_spreads if s.stopped),
            "flattened": flattened,
            "gross_credit_sold": round(gross_credit_sold, 2),
            "marked_pnl": round(last_marked_pnl, 2),
        })
        print(f"[{datetime.now().isoformat()}] session end. spreads={len(open_spreads)} "
              f"stopped={sum(1 for s in open_spreads if s.stopped)} "
              f"gross_credit=${gross_credit_sold:,.0f} marked_pnl=${last_marked_pnl:,.0f}")
    finally:
        if isinstance(provider, IBSignalProvider):
            try:
                provider.shutdown()
            except Exception:
                pass
        if ib is not None:
            try:
                ib.disconnect()
            except Exception:
                pass
        release_executor_lock(lock_path)


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
            stop_px = spread.stop_fill_price if spread.stop_fill_price is not None else spread.stop_price
            per_contract = spread.short_entry_sell - stop_px - spread.long_entry_buy + lq.bid
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

    def fetch(self, now: datetime, at_tranche: bool = False):
        return [], SignalSnapshot(timestamp=now)


if __name__ == "__main__":
    run(ACTIVE)
