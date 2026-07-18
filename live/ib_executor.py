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
    select_condor_entries,
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
    fetch_ib_spxw_positions,
    load_fills_events,
    recover_governor_state,
    recover_session_book,
    release_executor_lock,
)
from kill_switch import check_kill_switch  # noqa: E402
from account_guards import (  # noqa: E402
    check_loop_account_guard,
    check_startup_account_guard,
    fetch_account_snapshot,
)
from ib_connection import (  # noqa: E402
    format_reconnect_banner,
    ib_is_connected,
    reconnect_ib,
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
        session_vix: Optional[float] = None,
    ):
        self.ib = ib
        self.live = live
        self.config = config
        self.baselines = baselines_core
        self.session_vix = session_vix
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
            return SignalSnapshot(timestamp=now, vix=self.session_vix)
        session = now.date().isoformat()
        zero_q, _ = split_session_quotes(quotes, session)
        next_q = self._stream.next_expiry_quotes() if at_tranche else None
        raw = compute_raw_features(zero_q, spot, now, self._feature_state, next_expiry_quotes=next_q)
        signal = raw_to_signal_snapshot(raw, self.baselines, now)
        if self.session_vix is not None:
            from dataclasses import replace as dc_replace

            signal = dc_replace(signal, vix=self.session_vix)
        return signal


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
    ib: Optional["IB"],
    spread: OpenSpread,
    today: str,
    *,
    dry: bool = False,
    reason: str = "",
) -> None:
    """Cancel the optional native STP backstop on the short leg."""
    if dry or not HAS_IB or ib is None or spread.stop_order_id is None:
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
    ib: Optional["IB"],
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

    if dry or not HAS_IB or ib is None:
        for spread in open_spreads:
            if not spread.closed and _same_short_leg(spread.candidate, candidate):
                spread.stop_order_id = None
        return

    cancelled = _cancel_open_orders_on_short_leg(ib, candidate, today)
    for spread in open_spreads:
        if not spread.closed and _same_short_leg(spread.candidate, candidate):
            spread.stop_order_id = None
    if cancelled:
        log_event(today, {
            "event": "short_leg_orders_cancelled",
            "short_strike": candidate.short_strike,
            "short_type": candidate.short_type,
            "count": cancelled,
            "reason": reason or "unspecified",
        })


def native_stops_enabled(live: LiveConfig) -> bool:
    return bool(live.use_native_stop_replace or live.use_native_stop_backstop)


def active_spreads_on_short(
    open_spreads: Sequence[OpenSpread],
    candidate: CandidateRecord,
) -> List[OpenSpread]:
    """Open, unstopped verticals sharing this short option."""
    return [
        s
        for s in open_spreads
        if (
            not s.closed
            and not s.stopped
            and s.contracts > 0
            and s.stop_price > 0
            and _same_short_leg(s.candidate, candidate)
        )
    ]


def aggregated_native_stop_plan(
    siblings: Sequence[OpenSpread],
    live: LiveConfig,
    config: StrategyConfig,
) -> Tuple[int, float]:
    """Total short qty and tightest BUY-stop trigger for an aggregated STP."""
    if not siblings:
        return 0, 0.0
    total_qty = int(sum(s.contracts for s in siblings))
    if live.use_native_stop_replace:
        multiple = (
            live.native_stop_multiple
            if live.native_stop_multiple is not None
            else config.stop_multiple
        )
        stop_px = min(s.short_entry_sell * multiple for s in siblings)
    else:
        # Legacy disaster backstop: wider than the synthetic 3×.
        stop_px = min(s.stop_price * 1.5 for s in siblings)
    return total_qty, _round_spx_premium(stop_px)


def _order_still_working(ib: "IB", order_id: Optional[int]) -> bool:
    if order_id is None or not HAS_IB:
        return False
    for trade in ib.openTrades():
        if trade.order.orderId != order_id:
            continue
        status = _open_order_status(trade)
        if status in {"filled", "cancelled", "inactive", "apicancelled"}:
            return False
        return True
    return False


def place_or_replace_native_stop_for_short(
    ib: Optional["IB"],
    candidate: CandidateRecord,
    open_spreads: Sequence[OpenSpread],
    today: str,
    *,
    dry: bool,
    live: LiveConfig,
    config: StrategyConfig,
    reason: str = "rearm",
) -> Optional[int]:
    """Cancel any short-leg orders, then place one aggregated BUY STP.

    Safeties:
    - One STP per short contract covering total open size (avoids duplicate STPs).
    - Trigger uses the tightest (lowest) 3× among siblings so earlier/richer
      credits are not under-protected.
    - On reject, leave ``stop_order_id`` cleared and log loudly — synthetic
      stops remain primary while the loop is alive.
    """
    if not native_stops_enabled(live):
        return None
    siblings = active_spreads_on_short(open_spreads, candidate)
    if not siblings:
        return None
    total_qty, stop_px = aggregated_native_stop_plan(siblings, live, config)
    if total_qty <= 0 or stop_px <= 0:
        return None

    clear_short_leg_backstops(
        ib, candidate, open_spreads, today, dry=dry, reason=f"pre_{reason}",
    )
    # Re-resolve after clear (same objects; stop_order_id wiped).
    siblings = active_spreads_on_short(open_spreads, candidate)
    if not siblings:
        return None

    if dry or not HAS_IB or ib is None:
        fake_id = -(
            abs(hash((candidate.short_type, float(candidate.short_strike), total_qty, stop_px)))
            % 10_000_000
            or 1
        )
        for spread in siblings:
            spread.stop_order_id = fake_id
        log_event(today, {
            "event": "native_stop_armed",
            "short_strike": candidate.short_strike,
            "short_type": candidate.short_type,
            "contracts": total_qty,
            "stop_price": stop_px,
            "order_id": fake_id,
            "reason": reason,
            "dry": True,
            "spread_count": len(siblings),
        })
        return fake_id

    short_opt = _short_option(ib, candidate, today)
    stop_order = StopOrder("BUY", total_qty, stop_px)
    stop_order.tif = "DAY"
    trade = ib.placeOrder(short_opt, stop_order)
    order_id = trade.order.orderId
    reject = _trade_rejection_reason(trade)
    if reject:
        try:
            ib.cancelOrder(trade.order)
        except Exception:
            pass
        for spread in siblings:
            spread.stop_order_id = None
        log_event(today, {
            "event": "native_stop_rejected",
            "short_strike": candidate.short_strike,
            "short_type": candidate.short_type,
            "contracts": total_qty,
            "stop_price": stop_px,
            "reason": reject,
            "arm_reason": reason,
        })
        print(
            f"[{datetime.now().isoformat()}] NATIVE STP REJECTED "
            f"{candidate.short_type} {candidate.short_strike} x{total_qty} "
            f"@{stop_px:.2f}: {reject}"
        )
        return None

    for spread in siblings:
        spread.stop_order_id = order_id
    log_event(today, {
        "event": "native_stop_armed",
        "short_strike": candidate.short_strike,
        "short_type": candidate.short_type,
        "contracts": total_qty,
        "stop_price": stop_px,
        "order_id": order_id,
        "reason": reason,
        "dry": False,
        "spread_count": len(siblings),
    })
    print(
        f"[{datetime.now().isoformat()}] NATIVE STP {candidate.short_type} "
        f"{candidate.short_strike} x{total_qty} @{stop_px:.2f} ({reason})"
    )
    return order_id


def rearm_all_native_stops(
    ib: Optional["IB"],
    open_spreads: Sequence[OpenSpread],
    today: str,
    *,
    dry: bool,
    live: LiveConfig,
    config: StrategyConfig,
    reason: str = "rearm_all",
) -> int:
    """Place/replace aggregated STPs for every distinct open short leg."""
    if not native_stops_enabled(live):
        return 0
    seen: set = set()
    armed = 0
    for spread in open_spreads:
        if spread.closed or spread.stopped:
            continue
        key = (spread.candidate.short_type, float(spread.candidate.short_strike))
        if key in seen:
            continue
        seen.add(key)
        if place_or_replace_native_stop_for_short(
            ib,
            spread.candidate,
            open_spreads,
            today,
            dry=dry,
            live=live,
            config=config,
            reason=reason,
        ) is not None:
            armed += 1
    return armed


def verify_native_stops(
    ib: Optional["IB"],
    open_spreads: Sequence[OpenSpread],
    today: str,
    *,
    dry: bool,
    live: LiveConfig,
    config: StrategyConfig,
) -> int:
    """Replace missing/cancelled native STPs (loop crash / TWS cancel safety)."""
    if not native_stops_enabled(live) or dry or not HAS_IB or ib is None:
        return 0
    repaired = 0
    seen: set = set()
    for spread in open_spreads:
        if spread.closed or spread.stopped:
            continue
        key = (spread.candidate.short_type, float(spread.candidate.short_strike))
        if key in seen:
            continue
        seen.add(key)
        siblings = active_spreads_on_short(open_spreads, spread.candidate)
        if not siblings:
            continue
        order_id = next((s.stop_order_id for s in siblings if s.stop_order_id is not None), None)
        if order_id is not None and _order_still_working(ib, order_id):
            continue
        log_event(today, {
            "event": "native_stop_missing",
            "short_strike": spread.candidate.short_strike,
            "short_type": spread.candidate.short_type,
            "prior_order_id": order_id,
        })
        if place_or_replace_native_stop_for_short(
            ib,
            spread.candidate,
            open_spreads,
            today,
            dry=dry,
            live=live,
            config=config,
            reason="verify_repair",
        ) is not None:
            repaired += 1
    return repaired


def enforce_native_stop_disarm_budget(
    ib: Optional["IB"],
    pending: Optional[PendingEntry],
    open_spreads: Sequence[OpenSpread],
    today: str,
    *,
    now: datetime,
    dry: bool,
    live: LiveConfig,
    config: StrategyConfig,
) -> Optional[PendingEntry]:
    """Cancel a same-strike add that has left existing shorts unprotected too long."""
    if (
        pending is None
        or not live.use_native_stop_replace
        or live.native_stop_disarm_max_seconds <= 0
    ):
        return pending
    siblings = active_spreads_on_short(open_spreads, pending.candidate)
    if not siblings:
        return pending
    disarmed = any(s.stop_order_id is None for s in siblings)
    age = (now - pending.submitted_at).total_seconds()
    if not disarmed or age < live.native_stop_disarm_max_seconds:
        return pending
    cancel_pending_entry(
        ib, pending, today, reason="native_stop_disarm_timeout", dry=dry,
    )
    place_or_replace_native_stop_for_short(
        ib,
        pending.candidate,
        open_spreads,
        today,
        dry=dry,
        live=live,
        config=config,
        reason="disarm_timeout_rearm",
    )
    log_event(today, {
        "event": "native_stop_disarm_timeout",
        "short_strike": pending.candidate.short_strike,
        "short_type": pending.candidate.short_type,
        "age_seconds": round(age, 1),
        "max_seconds": live.native_stop_disarm_max_seconds,
    })
    print(
        f"[{now.isoformat()}] NATIVE STP disarm timeout — cancelled pending "
        f"{pending.candidate.side} {pending.candidate.short_strike}/"
        f"{pending.candidate.long_strike} after {age:.0f}s"
    )
    return None


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
    if reason in {"new_tranche", "flatten", "error", "native_stop_disarm_timeout"}:
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

    # Cancel→add→replace: disarm native STP on this short before the combo SELL.
    clear_short_leg_backstops(
        ib, candidate, open_spreads, today, dry=dry, reason="pre_entry",
    )
    bag, _short_leg_opt = build_combo(ib, candidate, today)
    combo_order = LimitOrder("BUY", contracts, -limit)
    combo_order.tif = "DAY"
    trade = ib.placeOrder(bag, combo_order)
    spread.combo_order_id = trade.order.orderId

    submitted = now
    work_until = work_deadline(submitted, live, config.entry_interval_minutes)
    # Same-strike scale-in leaves existing shorts unprotected until fill/reject —
    # cap work time so STPs are re-armed quickly.
    if (
        live.use_native_stop_replace
        and live.native_stop_disarm_max_seconds > 0
        and active_spreads_on_short(open_spreads, candidate)
    ):
        disarm_deadline = submitted + timedelta(seconds=live.native_stop_disarm_max_seconds)
        if disarm_deadline < work_until:
            work_until = disarm_deadline
    pending = PendingEntry(
        spread=spread,
        trade=trade,
        candidate=candidate,
        contracts=contracts,
        natural_credit=nat,
        limit_credit=limit,
        submitted_at=submitted,
        work_until=work_until,
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
    ib: Optional["IB"] = None,
    today: str = "",
    dry: bool = True,
    live: Optional[LiveConfig] = None,
) -> Tuple[int, float, float, float]:
    """Apply a filled or rejected pending entry; re-arm native STP afterward."""
    filled = 0
    credit_added = 0.0
    margin = 0.0
    if event.get("event") == "entry":
        spread = pending.spread
        fill_credit = float(event.get("credit", pending.limit_credit))
        spread.fill_credit = fill_credit
        # Keep stop_price aligned with strategy multiple on the short bid at submit.
        if live is not None and live.use_native_stop_replace:
            multiple = (
                live.native_stop_multiple
                if live.native_stop_multiple is not None
                else config.stop_multiple
            )
            if spread.short_entry_sell > 0 and multiple > 0:
                spread.stop_price = _round_spx_premium(spread.short_entry_sell * multiple)
        open_spreads.append(spread)
        contracts = pending.contracts
        credit_added = fill_credit * contracts * config.multiplier
        margin = candidate_margin_per_contract(pending.candidate, config) * contracts
        sleeve = pending.sleeve or "core"
        sleeve_margin_used[sleeve] = sleeve_margin_used.get(sleeve, 0.0) + margin
        filled = 1
        print(
            f"[{datetime.now().isoformat()}] ENTRY filled {pending.candidate.side} "
            f"{pending.candidate.short_strike}/{pending.candidate.long_strike} "
            f"x{contracts} fill={fill_credit:.2f} "
            f"(natural={event.get('natural_credit')} slippage={event.get('fill_slippage')})"
        )
    else:
        print(
            f"[{datetime.now().isoformat()}] ENTRY failed {pending.candidate.side} "
            f"{pending.candidate.short_strike}/{pending.candidate.long_strike} "
            f"reason={event.get('reason')}"
        )

    if live is not None and today and native_stops_enabled(live):
        place_or_replace_native_stop_for_short(
            ib,
            pending.candidate,
            open_spreads,
            today,
            dry=dry,
            live=live,
            config=config,
            reason="post_fill" if filled else "post_reject",
        )

    return filled, credit_added, margin, portfolio_margin_used + margin


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
    open_spreads: Sequence[OpenSpread],
) -> Tuple[bool, float]:
    """Phase 4: limit at ask + buffer, escalate to MKT if unfilled."""
    # Cancel aggregated native STP on this short before the protective BUY.
    clear_short_leg_backstops(
        ib, candidate, open_spreads, today, dry=False, reason="synthetic_stop",
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
                ib, spread, spread.candidate, today, sq.ask, live, open_spreads,
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

    # Re-arm aggregated STP for any remaining size on shorts that just stopped.
    if newly_stopped and native_stops_enabled(live):
        seen: set = set()
        for spread in newly_stopped:
            key = (spread.candidate.short_type, float(spread.candidate.short_strike))
            if key in seen:
                continue
            seen.add(key)
            place_or_replace_native_stop_for_short(
                ib,
                spread.candidate,
                open_spreads,
                today,
                dry=dry,
                live=live,
                config=config,
                reason="post_synthetic_stop",
            )
    return newly_stopped


@dataclass
class FlattenResult:
    """Outcome of a confirmed flatten attempt."""

    closed: int = 0
    failed: int = 0
    residual_ib_lots: int = 0
    complete: bool = True


def flatten_all(
    ib: "IB",
    open_spreads: Sequence[OpenSpread],
    today: str,
    dry: bool,
    *,
    live: Optional[LiveConfig] = None,
    timeout_sec: Optional[float] = None,
) -> FlattenResult:
    """Flatten governor: MKT-close open spreads and confirm fills / IB residual."""
    cfg = live or ACTIVE
    wait_sec = (
        timeout_sec
        if timeout_sec is not None
        else float(getattr(cfg, "flatten_fill_timeout_seconds", 12.0))
    )
    retry_mkt = bool(getattr(cfg, "flatten_retry_mkt", True))
    result = FlattenResult()

    for spread in open_spreads:
        if spread.closed:
            continue
        if dry or not HAS_IB or ib is None:
            spread.closed = True
            result.closed += 1
            continue

        clear_short_leg_backstops(
            ib, spread.candidate, [spread], today, dry=dry, reason="flatten",
        )
        bag, _short_leg_opt = build_combo(ib, spread.candidate, today)
        trade = ib.placeOrder(
            bag,
            Order(action="SELL", totalQuantity=spread.contracts, orderType="MKT"),
        )
        state, reason = _wait_for_order(ib, trade, timeout_sec=wait_sec)
        if state != "filled" and retry_mkt:
            if state == "pending":
                try:
                    ib.cancelOrder(trade.order)
                    ib.sleep(0.25)
                except Exception:
                    pass
            trade = ib.placeOrder(
                bag,
                Order(action="SELL", totalQuantity=spread.contracts, orderType="MKT"),
            )
            state, reason = _wait_for_order(ib, trade, timeout_sec=wait_sec)

        if state == "filled":
            fill_px = float(trade.orderStatus.avgFillPrice or 0.0)
            spread.closed = True
            result.closed += 1
            log_event(today, {
                "event": "flatten_fill",
                "side": spread.candidate.side,
                "short_strike": spread.candidate.short_strike,
                "long_strike": spread.candidate.long_strike,
                "contracts": spread.contracts,
                "fill_price": round(fill_px, 4),
            })
        else:
            result.failed += 1
            result.complete = False
            log_event(today, {
                "event": "flatten_unfilled",
                "side": spread.candidate.side,
                "short_strike": spread.candidate.short_strike,
                "long_strike": spread.candidate.long_strike,
                "contracts": spread.contracts,
                "state": state,
                "reason": reason,
            })
            print(
                f"[{datetime.now().isoformat()}] FLATTEN UNFILLED "
                f"{spread.candidate.side} {spread.candidate.short_strike}/"
                f"{spread.candidate.long_strike} x{spread.contracts} "
                f"state={state} reason={reason or 'timeout'}"
            )

    # Confirm no residual SPXW risk remains in IB for still-open local book.
    still_open = [s for s in open_spreads if not s.closed]
    if not dry and HAS_IB and ib is not None:
        try:
            ib_nets = fetch_ib_spxw_positions(ib, today)
        except Exception:
            ib_nets = {}
        residual_lots = sum(abs(v) for v in ib_nets.values())
        result.residual_ib_lots = residual_lots
        if residual_lots > 0 and still_open:
            result.complete = False
            log_event(today, {
                "event": "flatten_incomplete",
                "residual_ib_lots": residual_lots,
                "still_open_local": len(still_open),
                "closed": result.closed,
                "failed": result.failed,
            })
            print(
                f"[{datetime.now().isoformat()}] FLATTEN INCOMPLETE — "
                f"IB still shows {residual_lots} SPXW lot(s); "
                f"local open={len(still_open)}"
            )
        elif residual_lots > 0 and not still_open:
            # Local book closed but IB residual — treat as incomplete.
            result.complete = False
            result.residual_ib_lots = residual_lots
            log_event(today, {
                "event": "flatten_incomplete",
                "residual_ib_lots": residual_lots,
                "still_open_local": 0,
                "closed": result.closed,
                "failed": result.failed,
                "note": "local_closed_ib_residual",
            })

    if result.failed == 0 and result.residual_ib_lots == 0:
        result.complete = True
    return result


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
        cancelled_cand = pending_entry.candidate
        cancel_pending_entry(ib, pending_entry, today, reason="new_tranche", dry=dry)
        # Re-arm STPs that were disarmed for the abandoned working entry.
        place_or_replace_native_stop_for_short(
            ib,
            cancelled_cand,
            open_spreads,
            today,
            dry=dry,
            live=live,
            config=config,
            reason="new_tranche_rearm",
        )
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
        # Mirror simulate_day: optional iron-condor overlay (once/day when configured).
        if config.use_condor_sleeve:
            # Synthesize Trade-like markers from open spreads for max-entries check.
            class _CondorMark:
                def __init__(self, side: str):
                    self.model = "candidate_condor"
                    self.side = side

            condor_marks = [
                _CondorMark(s.candidate.side)
                for s in open_spreads
                if (s.candidate.sleeve or "") == "condor"
            ]
            condor_selected, condor_records = select_condor_entries(
                quotes, signal, base_contracts, config, trades=condor_marks
            )
            selected.extend(condor_selected)
            records.extend(condor_records)

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
            place_or_replace_native_stop_for_short(
                ib,
                cand,
                open_spreads,
                today,
                dry=True,
                live=live,
                config=config,
                reason="dry_entry",
            )
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

    kill_hit = check_kill_switch(today, enabled=live.kill_switch_enabled)
    if kill_hit is not None:
        release_executor_lock(lock_path)
        raise SystemExit(
            f"KILL switch present ({kill_hit.scope}: {kill_hit.path}). "
            "Remove the file before starting the executor."
        )

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
          f"vix_put_widen="
          f"{'off' if config.vix_widen_put_wing_above <= 0 else f'>={config.vix_widen_put_wing_above:.0f}+{config.vix_widen_put_wing_extra:.0f}'} "
          f"fomc_cutoff={'on@'+config.fomc_entry_end.strftime('%H:%M') if config.use_fomc_entry_cutoff else 'off'} "
          f"gates=trend<={config.candidate_max_adverse_trend}/skew<={config.candidate_max_adverse_skew} "
          f"stop={config.stop_multiple}x/{config.stop_confirmation_count}bar "
          f"native_stp={'replace@' + (f'{live.native_stop_multiple}x' if live.native_stop_multiple is not None else f'{config.stop_multiple}x') if live.use_native_stop_replace else ('legacy_1.5x' if live.use_native_stop_backstop else 'off')} "
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
            if live.use_account_guards and not dry:
                acct = fetch_account_snapshot(ib)
                guard = check_startup_account_guard(
                    acct,
                    account_equity=live.account_equity,
                    netliq_min_ratio=live.netliq_min_ratio,
                    buying_power_min_ratio=live.buying_power_min_ratio,
                )
                log_event(today, {
                    "event": "account_guard_startup",
                    "ok": guard.ok,
                    "reason": guard.reason,
                    "net_liquidation": acct.net_liquidation,
                    "buying_power": acct.buying_power,
                    "account_equity": live.account_equity,
                })
                if not guard.ok:
                    release_executor_lock(lock_path)
                    raise SystemExit(
                        f"account guard failed at startup: {guard.reason} "
                        f"(NetLiq={acct.net_liquidation}, BP={acct.buying_power}, "
                        f"configured equity={live.account_equity:,.0f})"
                    )
                print(
                    f"Account guard OK — NetLiq=${acct.net_liquidation:,.0f} "
                    f"BP=${acct.buying_power:,.0f} (equity=${live.account_equity:,.0f})"
                )
            provider = IBSignalProvider(
                ib, live, config, baselines_core=baselines_core, session_vix=vix_open
            )
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
            if open_spreads and native_stops_enabled(live):
                armed = rearm_all_native_stops(
                    ib,
                    open_spreads,
                    today,
                    dry=dry,
                    live=live,
                    config=config,
                    reason="session_recovery",
                )
                print(
                    f"[{datetime.now().isoformat()}] native STP armed on "
                    f"{armed} short leg(s) after recovery"
                )

        # Restore halt/flatten/cooldown so a restart cannot re-arm selling after a halt.
        fills_events = load_fills_events(today)
        governor = recover_governor_state(
            fills_events,
            now=datetime.now(),
            cooldown_minutes=config.same_side_stop_cooldown_minutes,
        )
        entries_halted = governor.entries_halted
        flattened = governor.flattened
        side_stop_cooldown_until: Dict[str, datetime] = dict(governor.side_stop_cooldown_until)
        for warn in governor.warnings:
            print(f"[{datetime.now().isoformat()}] GOVERNOR WARN: {warn}")
        if entries_halted or flattened or side_stop_cooldown_until:
            print(
                f"[{datetime.now().isoformat()}] governor recovered — "
                f"halted={entries_halted} flattened={flattened} "
                f"cooldowns={list(side_stop_cooldown_until)}"
            )
            log_event(today, {
                "event": "governor_recovered",
                "entries_halted": entries_halted,
                "flattened": flattened,
                "cooldowns": {k: v.isoformat() for k, v in side_stop_cooldown_until.items()},
            })
        if flattened and open_spreads and ib is not None and not dry:
            raise SystemExit(
                "fills.jsonl shows a prior flatten but open SPXW risk remains. "
                "Manually flatten/reconcile in TWS, then restart."
            )

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
        traded_tranches: set = set()
        pending_entry = None
        last_quotes = []
        last_marked_pnl = 0.0
        last_native_stop_verify_at = datetime.now()
        last_account_guard_at = datetime.now() - timedelta(seconds=live.account_guard_poll_seconds)
        mark_bad_since: Optional[datetime] = None
        disconnect_halt = False
        ib_port = live.port or (7497 if live.mode == "paper" else 7496)
        ib_provider: Optional[IBSignalProvider] = None
        if isinstance(provider, IBSignalProvider):
            ib_provider = provider

        def _trigger_flatten(reason: str, marked_pnl: float) -> FlattenResult:
            nonlocal pending_entry, flattened, entries_halted
            flattened = True
            entries_halted = True
            cancel_pending_entry(ib, pending_entry, today, reason=reason, dry=dry)
            pending_entry = None
            fres = flatten_all(
                ib, [s for s in open_spreads if not s.closed], today, dry, live=live,
            )
            log_event(today, {
                "event": "flatten",
                "reason": reason,
                "marked_pnl": round(marked_pnl, 2),
                "complete": fres.complete,
                "closed": fres.closed,
                "failed": fres.failed,
                "residual_ib_lots": fres.residual_ib_lots,
            })
            return fres

        try:
            while datetime.now().time() <= config.force_flat_time:
                now = datetime.now()

                # --- Phase F: external KILL switch ---------------------------------
                kill_hit = check_kill_switch(today, enabled=live.kill_switch_enabled)
                if kill_hit is not None:
                    print(
                        f"[{now.isoformat()}] KILL switch ({kill_hit.scope}: {kill_hit.path}) "
                        "— flattening and exiting."
                    )
                    log_event(today, {
                        "event": "kill_switch",
                        "scope": kill_hit.scope,
                        "path": str(kill_hit.path),
                    })
                    _trigger_flatten("kill_switch", last_marked_pnl)
                    raise SystemExit(f"KILL switch activated ({kill_hit.path})")

                # --- Phase C: disconnect / reconnect breaker ----------------------
                if (
                    live.use_disconnect_breaker
                    and connect_ib
                    and ib is not None
                    and not dry
                    and not ib_is_connected(ib)
                ):
                    halted_before_disconnect = entries_halted
                    disconnect_halt = True
                    entries_halted = True
                    print(f"[{now.isoformat()}] IB DISCONNECTED — halting entries, reconnecting…")
                    log_event(today, {"event": "ib_disconnected"})
                    cancel_pending_entry(ib, pending_entry, today, reason="disconnect", dry=dry)
                    pending_entry = None
                    if ib_provider is not None:
                        try:
                            ib_provider.shutdown()
                        except Exception:
                            pass
                    outcome = reconnect_ib(
                        ib,
                        host=live.host,
                        port=ib_port,
                        client_id=live.client_id,
                        max_seconds=live.reconnect_max_seconds,
                        initial_backoff=live.reconnect_initial_backoff,
                        max_backoff=live.reconnect_max_backoff,
                        on_attempt=lambda n, b: print(
                            f"[{datetime.now().isoformat()}] reconnect attempt {n} (backoff {b:.0f}s)"
                        ),
                        sleep_fn=(ib.sleep if HAS_IB else _time.sleep),
                    )
                    print(format_reconnect_banner(outcome))
                    log_event(today, {
                        "event": "ib_reconnect",
                        "connected": outcome.connected,
                        "attempts": outcome.attempts,
                        "elapsed_seconds": round(outcome.elapsed_seconds, 2),
                        "reason": outcome.reason,
                    })
                    if not outcome.connected:
                        open_risk = [s for s in open_spreads if not s.closed]
                        if open_risk:
                            _trigger_flatten("reconnect_failed", last_marked_pnl)
                        raise SystemExit("IB reconnect failed — exiting")
                    register_ib_error_handler(ib, today)
                    if ib_provider is not None:
                        ib_provider.ib = ib
                        ib_provider.start()
                    # Re-check book vs IB after reconnect (fail loud on residual).
                    recovered_chk = recover_session_book(
                        today=today,
                        stop_multiple=config.stop_multiple,
                        OpenSpread=OpenSpread,
                        CandidateRecord=CandidateRecord,
                        ib=ib,
                        fail_on_unmatched=True,
                        cancel_orphans=False,
                    )
                    open_spreads[:] = list(recovered_chk.spreads)
                    if open_spreads and native_stops_enabled(live):
                        rearm_all_native_stops(
                            ib, open_spreads, today, dry=dry, live=live, config=config,
                            reason="post_reconnect",
                        )
                    # Clear only the disconnect-induced halt; keep PnL/account/mark halts.
                    disconnect_halt = False
                    entries_halted = halted_before_disconnect or flattened
                    continue

                at_tranche = should_fire_tranche(now, config, traded_tranches)
                quotes, signal = provider.fetch(now, at_tranche=at_tranche)
                last_quotes = list(quotes)

                pending_entry = enforce_native_stop_disarm_budget(
                    ib,
                    pending_entry,
                    open_spreads,
                    today,
                    now=now,
                    dry=dry,
                    live=live,
                    config=config,
                )

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
                            ib=ib,
                            today=today,
                            dry=dry,
                            live=live,
                        )
                        gross_credit_sold += credit_added

                if (
                    native_stops_enabled(live)
                    and live.native_stop_verify_seconds > 0
                    and (now - last_native_stop_verify_at).total_seconds()
                    >= live.native_stop_verify_seconds
                ):
                    # Skip verify while a same-strike entry is working (STP must stay off).
                    pending_blocks_verify = (
                        pending_entry is not None
                        and bool(active_spreads_on_short(open_spreads, pending_entry.candidate))
                    )
                    if not pending_blocks_verify:
                        verify_native_stops(
                            ib,
                            open_spreads,
                            today,
                            dry=dry,
                            live=live,
                            config=config,
                        )
                    last_native_stop_verify_at = now

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

                # --- Phase B: periodic NetLiq overlay -----------------------------
                if (
                    live.use_account_guards
                    and ib is not None
                    and not dry
                    and (now - last_account_guard_at).total_seconds()
                    >= live.account_guard_poll_seconds
                ):
                    last_account_guard_at = now
                    acct = fetch_account_snapshot(ib)
                    loop_guard = check_loop_account_guard(
                        acct,
                        account_equity=live.account_equity,
                        netliq_halt_ratio=live.netliq_halt_ratio,
                        netliq_flatten_ratio=live.netliq_flatten_ratio,
                        flatten_on_netliq_breach=live.flatten_on_netliq_breach,
                    )
                    if loop_guard.halt_entries and not entries_halted:
                        entries_halted = True
                        print(
                            f"[{now.isoformat()}] HALT (account guard): {loop_guard.reason}"
                        )
                        log_event(today, {
                            "event": "halt_entries",
                            "reason": "account_guard",
                            "detail": loop_guard.reason,
                            "net_liquidation": acct.net_liquidation,
                            "buying_power": acct.buying_power,
                        })
                    if loop_guard.flatten and not flattened:
                        print(f"[{now.isoformat()}] FLATTEN (account guard): {loop_guard.reason}")
                        _trigger_flatten("account_guard", last_marked_pnl)

                # --- Phase D: mark integrity + PnL governor -----------------------
                mark = _mark_book(open_spreads, quotes, config)
                if mark.quality == "ok":
                    mark_bad_since = None
                    last_marked_pnl = mark.pnl
                    if not entries_halted and mark.pnl <= halt_limit:
                        entries_halted = True
                        print(
                            f"[{now.isoformat()}] HALT new entries "
                            f"(marked ${mark.pnl:,.0f} <= ${halt_limit:,.0f})."
                        )
                        log_event(today, {
                            "event": "halt_entries",
                            "marked_pnl": round(mark.pnl, 2),
                        })
                    if (
                        config.flatten_on_daily_loss
                        and not flattened
                        and mark.pnl <= flatten_limit
                    ):
                        print(
                            f"[{now.isoformat()}] FLATTEN "
                            f"(marked ${mark.pnl:,.0f} <= ${flatten_limit:,.0f})."
                        )
                        _trigger_flatten("daily_loss", mark.pnl)
                else:
                    if mark_bad_since is None:
                        mark_bad_since = now
                        log_event(today, {
                            "event": "mark_degraded",
                            "quality": mark.quality,
                            "open_count": mark.open_count,
                            "marked_count": mark.marked_count,
                            "missing_count": mark.missing_count,
                        })
                    if live.mark_degraded_halt and mark.open_count > 0:
                        if not entries_halted:
                            entries_halted = True
                            print(
                                f"[{now.isoformat()}] HALT (mark {mark.quality}) — "
                                f"missing quotes on {mark.missing_count}/{mark.open_count} spread(s)."
                            )
                            log_event(today, {
                                "event": "halt_entries",
                                "reason": f"mark_{mark.quality}",
                                "missing_count": mark.missing_count,
                                "open_count": mark.open_count,
                            })
                    bad_age = (now - mark_bad_since).total_seconds() if mark_bad_since else 0.0
                    if (
                        mark.quality == "unavailable"
                        and mark.open_count > 0
                        and not flattened
                        and bad_age >= live.mark_unavailable_flatten_seconds
                    ):
                        print(
                            f"[{now.isoformat()}] FLATTEN (mark unavailable "
                            f"for {bad_age:.0f}s with open risk)."
                        )
                        _trigger_flatten("mark_unavailable", last_marked_pnl)
                    elif mark.quality == "partial" and mark.marked_count > 0:
                        # Use partial mark only to tighten halt, never to clear it.
                        last_marked_pnl = mark.pnl
                        if not entries_halted and mark.pnl <= halt_limit:
                            entries_halted = True
                            log_event(today, {
                                "event": "halt_entries",
                                "marked_pnl": round(mark.pnl, 2),
                                "reason": "partial_mark",
                            })

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
                        entries_halted=entries_halted or disconnect_halt,
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
                    if HAS_IB and ib is not None and ib_is_connected(ib):
                        ib.sleep(self_sleep)
                    else:
                        _time.sleep(self_sleep)
        except SystemExit:
            raise
        except Exception as exc:
            print(f"[{datetime.now().isoformat()}] ERROR: {exc!r} -- flattening and exiting.")
            log_event(today, {"event": "error_flatten", "error": repr(exc)})
            cancel_pending_entry(ib, pending_entry, today, reason="error", dry=dry)
            pending_entry = None
            if not dry:
                flatten_all(
                    ib, [s for s in open_spreads if not s.closed], today, dry, live=live,
                )
            raise

        # End of day: SPXW 0DTE is cash-settled on the close, so open defined-risk
        # spreads are left to settle (matches the backtest's settle-at-close). Only
        # the governor or an error flattens early.
        if last_quotes:
            last_marked_pnl = _mark_book(open_spreads, last_quotes, config).pnl
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


@dataclass(frozen=True)
class MarkBookResult:
    """Marked PnL plus quality so the governor never treats missing quotes as $0."""

    pnl: float
    quality: str  # ok | partial | unavailable
    open_count: int
    marked_count: int
    missing_count: int


def _mark_book(
    open_spreads: Sequence[OpenSpread],
    quotes: Sequence[OptionQuote],
    config: StrategyConfig,
) -> MarkBookResult:
    active = [s for s in open_spreads if not s.closed]
    if not active:
        return MarkBookResult(pnl=0.0, quality="ok", open_count=0, marked_count=0, missing_count=0)
    if not quotes:
        return MarkBookResult(
            pnl=0.0,
            quality="unavailable",
            open_count=len(active),
            marked_count=0,
            missing_count=len(active),
        )
    lookup = {(q.option_type, q.strike): q for q in quotes}
    total = 0.0
    marked = 0
    missing = 0
    for spread in active:
        cand = spread.candidate
        opt_type = _candidate_option_type(cand)
        lq = lookup.get((opt_type, cand.long_strike))
        if lq is None or lq.bid is None:
            missing += 1
            continue
        if spread.stopped:
            stop_px = spread.stop_fill_price if spread.stop_fill_price is not None else spread.stop_price
            per_contract = spread.short_entry_sell - stop_px - spread.long_entry_buy + float(lq.bid)
        else:
            sq = lookup.get((opt_type, cand.short_strike))
            if sq is None or sq.ask is None or sq.ask <= 0:
                missing += 1
                continue
            per_contract = spread.short_entry_sell - sq.ask - spread.long_entry_buy + float(lq.bid)
        total += per_contract * spread.contracts * config.multiplier
        marked += 1
    if missing == 0:
        quality = "ok"
    elif marked == 0:
        quality = "unavailable"
    else:
        quality = "partial"
    return MarkBookResult(
        pnl=total,
        quality=quality,
        open_count=len(active),
        marked_count=marked,
        missing_count=missing,
    )


class _NeutralProvider:
    """Dry-run provider with no IB: emits neutral signals so the loop exercises
    end-to-end wiring without market data."""

    def fetch(self, now: datetime, at_tranche: bool = False):
        return [], SignalSnapshot(timestamp=now)


if __name__ == "__main__":
    run(ACTIVE)
