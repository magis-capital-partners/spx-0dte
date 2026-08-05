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
import math
import sys
import time as _time
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as dt_time, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))


class _ConsoleTee:
    """Mirror an interactive executor's console output into its session folder."""

    def __init__(self, console, log_path: Path):
        self._console = console
        self._log = log_path.open("a", encoding="utf-8", buffering=1)

    def write(self, text: str) -> int:
        self._console.write(text)
        self._log.write(text)
        return len(text)

    def flush(self) -> None:
        self._console.flush()
        self._log.flush()

    def isatty(self) -> bool:
        return bool(getattr(self._console, "isatty", lambda: False)())

    @property
    def encoding(self) -> str:
        return getattr(self._console, "encoding", "utf-8") or "utf-8"


def _mirror_console_to_session_log() -> None:
    """Capture direct/manual launches as well as supervised launches for the UI."""
    today = date.today().isoformat()
    log_path = ROOT / "data" / "live" / today / "executor-console.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    sys.stdout = _ConsoleTee(sys.stdout, log_path)
    sys.stderr = _ConsoleTee(sys.stderr, log_path)

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
from combo_pricing import ComboQuote, protect_credit_limit  # noqa: E402
from entry_execution import (  # noqa: E402
    ENTRY_DONE_STATUSES,
    PendingEntry,
    evaluate_entry_quality,
    entry_limit_credit,
    entry_quote_block_reason,
    natural_credit,
    pending_is_awaiting_cancel,
    pending_trade_is_active,
    poll_pending_entry,
    round_spx_premium,
    teardown_fill_event,
    work_deadline,
)
from live_features import (  # noqa: E402
    DeterministicMinuteSampler,
    SessionFeatureState,
    compute_raw_features_once_per_minute,
    extract_baselines_core,
    raw_to_signal_snapshot,
    signal_features_are_sane,
    split_session_quotes,
    validate_baselines_freshness,
)
from feature_state_io import load_feature_state, save_feature_state  # noqa: E402
from ib_market_data import IBStreamingMarketData  # noqa: E402
from loop_timing import (  # noqa: E402
    adaptive_sleep_seconds,
    interruptible_sleep,
    seconds_until_market_open,
    should_fire_tranche,
    stop_wake_thresholds,
)
from risk_gates import apply_side_stop_cooldowns, side_stop_cooldown_block_reason  # noqa: E402
from vix_session import (  # noqa: E402
    check_vix_session_allowed,
    format_vix_session_banner,
    resolve_session_vix_open,
    vix_elevated_sizing_multiplier,
)
from session_recovery import (  # noqa: E402
    LegKey,
    acquire_executor_lock,
    fetch_ib_spxw_positions,
    load_fills_events,
    recover_governor_state,
    recovered_halt_is_mark_only,
    recover_session_book,
    release_executor_lock,
)
from kill_switch import check_kill_switch  # noqa: E402
from clear_stale_halt import (  # noqa: E402
    consume_clear_stale_halt,
    filter_cleared_stale_reasons,
)
from clear_flatten_halt import (  # noqa: E402
    consume_clear_flatten_halt,
    filter_cleared_flatten_reasons,
)
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
from connection_health import ConnectionHealthMonitor  # noqa: E402
from run_metadata import build_run_metadata  # noqa: E402
from stale_quotes import StaleQuoteTracker, evaluate_stale_quotes  # noqa: E402
from slack_notify import (  # noqa: E402
    dropped_count as slack_dropped_count,
    flush as flush_slack,
    maybe_notify_safety_event,
)
from heartbeat import append_risk_snapshot, write_heartbeat  # noqa: E402
from execution_type import execution_type  # noqa: E402
from risk_ledger import build_risk_snapshot  # noqa: E402
from open_risk_caps import open_risk_block_reason  # noqa: E402
from live_entry_risk import (  # noqa: E402
    apply_live_risk_overlays,
    live_entry_risk_block,
    recover_side_stop_counts,
)
from expiry_calendar import DEFAULT_RULES, is_live_tradable_day, load_era_rules  # noqa: E402
from profiles import schedule_multiplier  # noqa: E402
from strategy_profiles import resolve_strategy_config  # noqa: E402


LIVE_DIR = ROOT / "data" / "live"

# Feature state (spot history, first straddle) must only advance on regular-
# session observations. Pre-open polls fed the live realized-vol history with
# points the backtest baselines never saw, producing degenerate z-scores at
# the first tranche (2026-08-04: realized_vs_implied_z = -1.29M at 09:32).
SESSION_OPEN = dt_time(9, 30)


def wait_for_market_open(live: LiveConfig, today: str, *, ib=None) -> None:
    """Idle until shortly before the open so a session can be launched early.

    Nothing useful happens pre-open: the first tranche is after 09:30, feature
    state deliberately ignores pre-open quotes, and the cash index publishes no
    prints for the SPX probe to read. Idling through ``ib.sleep`` keeps the
    ib_insync event loop servicing the socket and its error handlers.
    """
    if not getattr(live, "wait_for_market_open", True):
        return
    lead_seconds = float(getattr(live, "market_data_lead_seconds", 0.0))
    remaining = seconds_until_market_open(
        datetime.now(), session_open=SESSION_OPEN, lead_seconds=lead_seconds
    )
    if remaining <= 0:
        return
    resume_at = datetime.now() + timedelta(seconds=remaining)
    print(
        f"[{datetime.now().isoformat()}] pre-open: idling {remaining / 60:.1f} min until "
        f"{resume_at.strftime('%H:%M:%S')} before starting market data "
        f"(open {SESSION_OPEN.strftime('%H:%M')}, lead {lead_seconds:.0f}s)"
    )
    log_event(today, {
        "event": "pre_open_wait",
        "resume_at": resume_at.isoformat(),
        "wait_seconds": round(remaining, 1),
        "session_open": SESSION_OPEN.strftime("%H:%M"),
        "lead_seconds": lead_seconds,
    })
    while True:
        remaining = (resume_at - datetime.now()).total_seconds()
        if remaining <= 0:
            break
        chunk = min(remaining, 30.0)
        if ib is not None:
            ib.sleep(chunk)
        else:
            _time.sleep(chunk)
    print(
        f"[{datetime.now().isoformat()}] pre-open wait complete — starting market data"
    )


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
    stop_breach_since: Optional[datetime] = None
    # Confirmation time accrued on fresh quotes only (see manage_stops).
    stop_confirmed_seconds: float = 0.0
    stop_last_breach_eval: Optional[datetime] = None
    stop_confirm_paused: bool = False
    stopped: bool = False
    closed: bool = False
    stop_fill_price: Optional[float] = None
    fill_credit: Optional[float] = None
    # Both verticals opened by one four-leg BAG share this immutable parent ID.
    # Stops remain per vertical to preserve the backtest's exit semantics.
    condor_id: Optional[str] = None

    @property
    def entry_credit(self) -> float:
        return self.short_entry_sell - self.long_entry_buy


# --------------------------------------------------------------------------- #
# IB market data implementation (skeleton)
# --------------------------------------------------------------------------- #
# IB codes that are normal on paper (delayed data, farm OK messages) — file only.
_QUIET_IB_ERROR_CODES = frozenset({10090, 10167, 10197, 202, 2104, 2106, 2158})


def setup_ib_logging(today: str, live: LiveConfig) -> Path:
    """Write bounded IB diagnostics without recording every streaming tick.

    Fills, order status, and IB errors have dedicated structured logs. Normal
    operation retains INFO-level library diagnostics in a rotated ``ib.log``;
    full DEBUG wire capture is an explicit, still-bounded troubleshooting mode.
    """
    day_dir = LIVE_DIR / today
    day_dir.mkdir(parents=True, exist_ok=True)
    ib_log = day_dir / "ib.log"

    ib_logger = logging.getLogger("ib_insync")
    for existing_handler in ib_logger.handlers:
        existing_handler.close()
    ib_logger.handlers.clear()
    ib_logger.propagate = False
    configured_level = getattr(logging, live.ib_log_level.upper(), logging.INFO)
    level = logging.DEBUG if live.ib_wire_debug_capture else configured_level
    ib_logger.setLevel(level)

    handler = RotatingFileHandler(
        ib_log,
        maxBytes=live.ib_log_max_bytes,
        backupCount=live.ib_log_backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    ib_logger.addHandler(handler)
    ib_logger.info(
        "IB logging configured level=%s wire_debug=%s max_bytes=%d backups=%d",
        logging.getLevelName(level),
        live.ib_wire_debug_capture,
        live.ib_log_max_bytes,
        live.ib_log_backup_count,
    )

    # Belt-and-suspenders: ib_insync also hooks the root console at INFO by default.
    if HAS_IB:
        from ib_insync import util
        util.logToConsole(logging.ERROR)
        # ``logToConsole`` also changes ib_insync's level; restore the session
        # file policy after suppressing the library's console chatter.
        ib_logger.setLevel(level)

    return ib_log


def register_ib_error_handler(
    ib: "IB",
    today: str,
    *,
    health: Optional[ConnectionHealthMonitor] = None,
) -> None:
    """Structured IB error/warning log at data/live/<date>/ib_errors.jsonl.

    Feeds system connectivity events (1100/1101/1102) into the upstream health
    monitor and dedupes repeated per-contract console spam (e.g. error 200
    "no security definition") — every occurrence is still written to jsonl.
    """
    errors_path = LIVE_DIR / today / "ib_errors.jsonl"
    printed_contract_errors: set = set()

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
        if health is not None:
            health.on_ib_error(errorCode)
        if errorCode not in _QUIET_IB_ERROR_CODES:
            label = getattr(contract, "localSymbol", None) or getattr(contract, "symbol", "")
            if errorCode == 200 and label:
                dedupe_key = (errorCode, label)
                if dedupe_key in printed_contract_errors:
                    return
                printed_contract_errors.add(dedupe_key)
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
        *,
        today: Optional[str] = None,
    ):
        self.ib = ib
        self.live = live
        self.config = config
        self.baselines = baselines_core
        self.session_vix = session_vix
        self.today = today or datetime.now().date().isoformat()
        restored = load_feature_state(self.today)
        self._feature_state = restored if restored is not None else SessionFeatureState()
        if restored is not None:
            print(
                f"[{datetime.now().isoformat()}] restored SessionFeatureState "
                f"(first_straddle={restored.first_straddle}, "
                f"spot_history={len(restored.spot_history)})"
            )
        self._stream = IBStreamingMarketData(ib, live, config)
        self.last_signal_block_reason = ""
        self.last_signal_diagnostics: Dict[str, Any] = {}
        self._minute_sampler = DeterministicMinuteSampler(
            sample_offset_seconds=live.signal_sample_offset_seconds,
            sample_window_seconds=live.signal_sample_window_seconds,
            min_observations=live.signal_sample_min_observations,
            max_wait_seconds=live.signal_sample_max_wait_seconds,
        )

    @staticmethod
    def load_baselines(path: Path, max_age_days: int) -> tuple[dict, dict]:
        return load_baselines_file(path, max_age_days)

    def start(self) -> None:
        self._stream.start()

    def shutdown(self) -> None:
        self._stream.shutdown()

    def set_open_spread_legs(
        self,
        open_spreads: Sequence[OpenSpread],
    ) -> bool:
        """Pin every leg needed to monitor the current live/recovered book."""
        legs = set()
        for spread in open_spreads:
            if spread.closed:
                continue
            candidate = spread.candidate
            right = "P" if candidate.short_type.upper() in {"P", "PUT"} else "C"
            legs.add((right, float(candidate.short_strike)))
            legs.add((right, float(candidate.long_strike)))
        return self._stream.set_required_0dte_legs(sorted(legs))

    def wait_for_open_spread_quotes(
        self,
        timeout_seconds: float,
    ) -> List[Tuple[str, float]]:
        return self._stream.wait_for_required_quotes(timeout_seconds)

    def fetch(
        self, now: datetime, *, at_tranche: bool = False
    ) -> Tuple[List[OptionQuote], Optional[SignalSnapshot]]:
        self._stream.maybe_rebalance()
        if not self.live.use_streaming_quotes:
            self._stream._refresh_snapshot_cache()
        quotes = self._stream.build_option_quotes(now)
        spot = self._stream.spot()
        if spot <= 0:
            self.last_signal_block_reason = "missing_underlying"
            return [], None
        if self.live.use_streaming_quotes and self._stream.spot_is_stale(
            self.live.stale_spot_halt_seconds
        ):
            self.last_signal_block_reason = "stale_underlying"
            return quotes, None

        self.last_signal_block_reason = ""
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

    def leg_quote_update_times(self, candidate: CandidateRecord) -> List[Optional[float]]:
        return [
            self._stream.quote_update_time(candidate.short_type, candidate.short_strike),
            self._stream.quote_update_time(candidate.short_type, candidate.long_strike),
        ]

    # --- event-driven stop wake (delegates to the streaming feed) ----------- #
    def arm_stop_watch(self, thresholds) -> None:
        self._stream.arm_stop_watch(thresholds)

    def stop_wake_pending(self) -> bool:
        return self._stream.stop_wake_pending()

    def consume_stop_wake(self):
        return self._stream.consume_stop_wake()

    def evaluate_candidate_quality(
        self,
        candidate: CandidateRecord,
        now: datetime,
        *,
        reference_spot: float,
        reference_credit: float,
        reference_short_delta: float,
    ):
        is_condor = (candidate.sleeve or "").lower() == "condor"
        delta_min = self.config.condor_min_abs_delta if is_condor else self.config.min_abs_delta
        delta_max = self.config.condor_max_abs_delta if is_condor else self.config.max_abs_delta
        return evaluate_entry_quality(
            candidate,
            self.live,
            now=now,
            current_spot=self._stream.spot(),
            spot_age_seconds=self._stream.spot_age_seconds(),
            reference_spot=reference_spot,
            reference_credit=reference_credit,
            reference_short_delta=reference_short_delta,
            leg_ages=self.leg_quote_ages(candidate),
            leg_update_times=self.leg_quote_update_times(candidate),
            short_delta_min=delta_min,
            short_delta_max=delta_max,
        )

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
        if now.time() < SESSION_OPEN:
            # Never let pre-open quotes advance the sampler or feature state:
            # backtest baselines are built from 09:30+ observations only.
            self.last_signal_block_reason = "signal_warming"
            self.last_signal_diagnostics = {"sample_status": "pre_open"}
            return None
        session = now.date().isoformat()
        zero_q, _ = split_session_quotes(quotes, session)
        self._minute_sampler.observe(now, spot, zero_q)
        sample_status = self._minute_sampler.status(now)
        self.last_signal_diagnostics = {"sample_status": sample_status}
        if sample_status == "collecting":
            self.last_signal_block_reason = "signal_warming"
            return None
        if sample_status == "unavailable":
            self.last_signal_block_reason = "signal_inputs_unavailable"
            return None
        sample = self._minute_sampler.aggregate(now)
        if sample is None:
            self.last_signal_block_reason = "signal_inputs_unavailable"
            return None
        health = self._stream.feature_input_health(
            sample.spot,
            max_age_seconds=self.live.signal_max_feature_quote_age_seconds,
            max_dispersion_seconds=self.live.signal_max_feature_timestamp_dispersion_seconds,
        )
        self.last_signal_diagnostics.update({
            "sample_observations": sample.observation_count,
            "feature_quote_count": health.quote_count,
            "feature_max_age_seconds": round(health.max_age_seconds, 3),
            "feature_timestamp_dispersion_seconds": round(health.timestamp_dispersion_seconds, 3),
        })
        if not health.ok:
            self.last_signal_block_reason = health.reason
            return None
        if at_tranche:
            self._stream.refresh_next_expiry_at_tranche(now)
        next_q = self._stream.next_expiry_quotes() if at_tranche else None
        previous_sample_minute = self._feature_state.last_sample_minute
        raw = compute_raw_features_once_per_minute(
            sample.quotes,
            sample.spot,
            sample.timestamp,
            self._feature_state,
            next_expiry_quotes=next_q,
        )
        sample_ts = sample.timestamp
        signal = raw_to_signal_snapshot(raw, self.baselines, sample_ts)
        if not signal_features_are_sane(
            signal, max_abs_z=self.live.signal_sanity_abs_z,
        ):
            # Record the discarded values so cold-start/feed anomalies are
            # auditable without ever reaching candidate selection or tranches.
            self.last_signal_block_reason = "invalid_signal"
            self.last_signal_diagnostics.update({
                "discarded_straddle_residual_z": float(signal.straddle_residual_z),
                "discarded_skew_z": float(signal.skew_z),
                "discarded_term_ratio_z": float(signal.term_ratio_z),
                "discarded_trend_score": float(signal.trend_score),
                "discarded_realized_vs_implied_z": float(signal.realized_vs_implied_z),
                "sanity_abs_z_limit": float(self.live.signal_sanity_abs_z),
            })
            return None
        if self.session_vix is not None:
            from dataclasses import replace as dc_replace

            signal = dc_replace(signal, vix=self.session_vix)
        if self._feature_state.last_sample_minute != previous_sample_minute:
            try:
                save_feature_state(self.today, self._feature_state)
            except OSError:
                pass
        self.last_signal_block_reason = ""
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


def fetch_combo_execution_quote(
    ib: "IB", bag: "Contract", *, timeout_seconds: float = 0.75,
) -> ComboQuote:
    """Collect a bounded SMART BAG quote without blocking the executor loop.

    ``reqTickers`` may wait roughly eleven seconds for an IB snapshot.  A
    cancellable stream lets the entry path enforce its own sub-second budget.
    Credit BAG prices can legitimately be negative, so zero and negative
    finite prices are retained.
    """
    ticker = None
    try:
        ticker = ib.reqMktData(bag, "", False, False)
        deadline = _time.monotonic() + max(0.0, timeout_seconds)
        while True:
            bid_raw = getattr(ticker, "bid", None)
            ask_raw = getattr(ticker, "ask", None)
            bid = float(bid_raw) if bid_raw is not None else None
            ask = float(ask_raw) if ask_raw is not None else None
            if bid is not None and not math.isfinite(bid):
                bid = None
            if ask is not None and not math.isfinite(ask):
                ask = None
            if bid is not None or ask is not None:
                return ComboQuote(bid, ask)
            if _time.monotonic() >= deadline:
                return ComboQuote(None, None)
            ib.sleep(min(0.05, max(0.0, deadline - _time.monotonic())))
    except Exception:
        return ComboQuote(None, None)
    finally:
        if ticker is not None:
            try:
                ib.cancelMktData(bag)
            except Exception:
                pass


def build_paired_condor_combo(
    ib: "IB", put_candidate: CandidateRecord, call_candidate: CandidateRecord, today: str,
) -> "Contract":
    """Build one atomic four-leg SPXW iron-condor BAG.

    The order is deliberately a single combo: submitting its two verticals
    independently can leave an unintended naked/one-sided position.
    """
    if put_candidate.short_type != "PUT" or call_candidate.short_type != "CALL":
        raise ValueError("paired condor requires a put vertical and a call vertical")
    expiry = today.replace("-", "")
    contracts = []
    for candidate, right in ((put_candidate, "P"), (call_candidate, "C")):
        short_opt = Option("SPX", expiry, candidate.short_strike, right, "CBOE", tradingClass="SPXW")
        long_opt = Option("SPX", expiry, candidate.long_strike, right, "CBOE", tradingClass="SPXW")
        contracts.extend((short_opt, long_opt))
    ib.qualifyContracts(*contracts)
    bag = Contract(symbol="SPX", secType="BAG", currency="USD", exchange="SMART")
    bag.comboLegs = [
        ComboLeg(conId=contracts[0].conId, ratio=1, action="SELL", exchange="SMART"),
        ComboLeg(conId=contracts[1].conId, ratio=1, action="BUY", exchange="SMART"),
        ComboLeg(conId=contracts[2].conId, ratio=1, action="SELL", exchange="SMART"),
        ComboLeg(conId=contracts[3].conId, ratio=1, action="BUY", exchange="SMART"),
    ]
    return bag


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
    stop_order.account = live.ib_account
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
        }, live=live)
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
    sleeve_margin_used: Optional[dict] = None,
) -> Tuple[Optional[PendingEntry], CancelBooking]:
    """Cancel a same-strike add that has left existing shorts unprotected too long.

    Returns (remaining pending, booking). A non-empty booking means the entry
    filled before the cancel landed and is now in ``open_spreads``; the caller
    must fold its credit/margin into the session totals.
    """
    if (
        pending is None
        or not live.use_native_stop_replace
        or live.native_stop_disarm_max_seconds <= 0
    ):
        return pending, CancelBooking()
    siblings = active_spreads_on_short(open_spreads, pending.candidate)
    if not siblings:
        return pending, CancelBooking()
    disarmed = any(s.stop_order_id is None for s in siblings)
    age = (now - pending.submitted_at).total_seconds()
    if not disarmed or age < live.native_stop_disarm_max_seconds:
        return pending, CancelBooking()
    booking = cancel_pending_entry(
        ib,
        pending,
        today,
        reason="native_stop_disarm_timeout",
        dry=dry,
        # open_spreads is a list in every live caller; Sequence here is only a
        # read-only annotation for the scanning logic above.
        open_spreads=open_spreads if isinstance(open_spreads, list) else None,
        config=config,
        sleeve_margin_used=sleeve_margin_used,
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
    return None, booking


def _await_cancel_ack(ib: "IB", trade, *, live: LiveConfig) -> float:
    """Wait for IB to acknowledge a cancel. Returns seconds waited.

    Returns as soon as the trade reaches a terminal status, capped by
    ``entry_cancel_grace_seconds``. Sleeps in small slices so the ib_insync
    event loop keeps pumping (market data stays fresh throughout).
    """
    grace = float(getattr(live, "entry_cancel_grace_seconds", 1.0) or 0.0)
    if grace <= 0:
        return 0.0
    slice_seconds = max(
        float(getattr(live, "stop_wake_slice_seconds", 0.05) or 0.05), 0.01,
    )
    start = _time.monotonic()
    deadline = start + grace
    while _time.monotonic() < deadline:
        try:
            if (trade.orderStatus.status or "").lower() in ENTRY_DONE_STATUSES:
                break
        except Exception:
            break
        ib.sleep(min(slice_seconds, max(deadline - _time.monotonic(), 0.0)))
    return max(_time.monotonic() - start, 0.0)


@dataclass(frozen=True)
class CancelBooking:
    """What a teardown cancel actually left on the book.

    ``contracts`` > 0 means IB filled (fully or partially) before the cancel
    landed and the spread has been appended to ``open_spreads``; the caller must
    fold ``credit_added`` / ``margin`` into its running totals.
    """

    contracts: int = 0
    credit_added: float = 0.0
    margin: float = 0.0
    event: Optional[dict] = None


def cancel_pending_entry(
    ib: "IB",
    pending: Optional[PendingEntry],
    today: str,
    *,
    reason: str,
    dry: bool,
    open_spreads: Optional[List[OpenSpread]] = None,
    config: Optional[StrategyConfig] = None,
    sleeve_margin_used: Optional[dict] = None,
) -> CancelBooking:
    """Cancel a working entry during teardown, booking anything that filled.

    Pass ``open_spreads``/``config``/``sleeve_margin_used`` so a fill that beat
    the cancel is added to the local book — otherwise the loop would manage a
    short leg it does not know about. Without them the fill is logged loudly but
    left to ``run_flatten_audit`` / ``session_recovery`` to reconcile.
    """
    if pending is None or dry or not HAS_IB:
        return CancelBooking()
    try:
        ib.cancelOrder(pending.trade.order)
        # Terminal teardown: the caller discards this pending, so we still wait
        # for the cancel before a same-strike order can be placed. Wait on the
        # acknowledgement rather than a flat 0.25s — usually far shorter, and
        # bounded by entry_cancel_grace_seconds when IB is slow.
        _await_cancel_ack(ib, pending.trade, live=ACTIVE)
    except Exception:
        pass
    # The poll resolver never runs on this pending, so anything IB filled before
    # the cancel has to be booked here or it becomes an unmanaged short leg.
    fill_event = teardown_fill_event(pending)
    filled_lots = int(fill_event.get("contracts") or 0) if fill_event else 0
    booking = CancelBooking()
    if fill_event is not None:
        if open_spreads is not None and config is not None:
            contracts, credit_added, margin = _book_filled_spread(
                fill_event,
                pending,
                open_spreads=open_spreads,
                config=config,
                sleeve_margin_used=(
                    sleeve_margin_used if sleeve_margin_used is not None else {}
                ),
            )
            booking = CancelBooking(
                contracts=contracts,
                credit_added=credit_added,
                margin=margin,
                event=fill_event,
            )
            log_event(today, {**fill_event, "booked_at_cancel": reason})
            print(
                f"[{datetime.now().isoformat()}] ENTRY filled during "
                f"{reason} cancel: {pending.candidate.side} "
                f"{pending.candidate.short_strike}/{pending.candidate.long_strike} "
                f"x{contracts} fill={pending.spread.fill_credit:.2f} "
                f"stop={pending.spread.stop_price:.2f} — booked and managed."
            )
        else:
            # No book to append to (caller could not supply one). Log loudly:
            # the flatten audit is the backstop for the resulting residual.
            log_event(today, {**fill_event, "booked_at_cancel": None})
            print(
                f"[{datetime.now().isoformat()}] WARN: entry filled "
                f"{filled_lots}/{pending.contracts} during {reason} cancel "
                f"({pending.candidate.side} {pending.candidate.short_strike}) "
                f"but was NOT booked locally — audit will reconcile."
            )
    log_event(today, {
        "event": "entry_cancelled",
        "tranche_time": (
            pending.tranche_time.replace(second=0, microsecond=0).isoformat()
            if pending.tranche_time is not None
            else None
        ),
        "side": pending.candidate.side,
        "short_strike": pending.candidate.short_strike,
        "long_strike": pending.candidate.long_strike,
        "reason": reason,
        "limit_credit": round(pending.limit_credit, 2),
        "filled_lots": filled_lots,
        "requested_contracts": pending.contracts,
    })
    # An order that filled is not a rejection: emitting order_rejected here too
    # would double-count the tranche against the credit/margin ledger.
    if filled_lots == 0 and reason in {
        "new_tranche",
        "flatten",
        "error",
        "entry_fault",
        "native_stop_disarm_timeout",
        "poll_error",
        "stale_underlying",
        "disconnect",
    }:
        log_event(today, {
            "event": "order_rejected",
            "tranche_time": (
                pending.tranche_time.replace(second=0, microsecond=0).isoformat()
                if pending.tranche_time is not None
                else None
            ),
            "side": pending.candidate.side,
            "short_strike": pending.candidate.short_strike,
            "long_strike": pending.candidate.long_strike,
            "contracts": pending.contracts,
            "natural_credit": round(pending.natural_credit, 2),
            "limit_credit": round(pending.limit_credit, 2),
            "credit": round(pending.limit_credit, 2),
            "status": "Cancelled",
            "reason": f"entry_cancelled_{reason}",
            "filled_lots": filled_lots,
        })
    return booking


def repair_session_after_entry_fault(
    ib: Optional["IB"],
    pending: Optional[PendingEntry],
    open_spreads: Sequence[OpenSpread],
    today: str,
    *,
    dry: bool,
    live: LiveConfig,
    config: StrategyConfig,
    error: str,
    sleeve_margin_used: Optional[dict] = None,
) -> CancelBooking:
    """Clear a dangling pending entry and re-arm native STPs so the loop can continue.

    Returns the teardown booking: non-empty when the faulted entry had actually
    filled, in which case the spread is now in ``open_spreads`` and the caller
    must fold its credit/margin into the session totals.
    """
    if pending is None:
        return CancelBooking()
    log_event(today, {
        "event": "entry_poll_error",
        "error": error,
        "side": pending.candidate.side,
        "short_strike": pending.candidate.short_strike,
        "long_strike": pending.candidate.long_strike,
        "ladder_step": pending.ladder_step,
        "status": getattr(
            getattr(pending.trade, "orderStatus", None), "status", None,
        ),
    }, live=live)
    booking = cancel_pending_entry(
        ib,
        pending,
        today,
        reason="poll_error",
        dry=dry,
        open_spreads=open_spreads if isinstance(open_spreads, list) else None,
        config=config,
        sleeve_margin_used=sleeve_margin_used,
    )
    if native_stops_enabled(live):
        place_or_replace_native_stop_for_short(
            ib,
            pending.candidate,
            open_spreads,
            today,
            dry=dry,
            live=live,
            config=config,
            reason="post_poll_error",
        )
    return booking


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
    reference_spot = float(candidate.spot)
    reference_credit = max(float(candidate.credit or 0.0), natural_credit(candidate))
    reference_short_delta = abs(float(candidate.short_delta or 0.0))
    if provider is not None:
        provider.refresh_candidate_legs(candidate, now)
    leg_ages = provider.leg_quote_ages(candidate) if provider is not None else None
    block = entry_quote_block_reason(candidate, live, leg_ages=leg_ages)
    if block:
        return None, None, block
    quality = None
    if provider is not None:
        quality = provider.evaluate_candidate_quality(
            candidate,
            now,
            reference_spot=reference_spot,
            reference_credit=reference_credit,
            reference_short_delta=reference_short_delta,
        )
        if not quality.ok:
            return None, None, f"entry_quality_{quality.reason}"

    nat = natural_credit(candidate)
    limit = entry_limit_credit(nat, live)
    short_sell = candidate.short_quote.bid if candidate.short_quote else 0.0
    long_buy = candidate.long_quote.ask if candidate.long_quote else 0.0
    entry_diagnostics = {
        "decision_spot": round(float(candidate.spot), 4),
        "candidate_timestamp": candidate.timestamp.isoformat(),
        "short_delta": (
            round(float(candidate.short_delta), 6)
            if candidate.short_delta is not None
            else None
        ),
        "long_delta": (
            round(float(candidate.long_delta), 6)
            if candidate.long_delta is not None
            else None
        ),
        "width": round(float(candidate.width), 4),
        "trend_score": round(float(candidate.trend_score), 6),
        "skew_z": round(float(candidate.skew_z), 6),
        "straddle_residual_z": round(float(candidate.straddle_residual_z), 6),
        "short_bid": round(float(short_sell), 4),
        "short_ask": round(float(candidate.short_quote.ask), 4) if candidate.short_quote else None,
        "long_bid": round(float(candidate.long_quote.bid), 4) if candidate.long_quote else None,
        "long_ask": round(float(long_buy), 4),
    }
    if quality is not None and quality.diagnostics:
        entry_diagnostics.update(quality.diagnostics)
    if provider is not None:
        entry_diagnostics.update({
            f"signal_{key}": value
            for key, value in provider.last_signal_diagnostics.items()
        })
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

    # Build and validate the BAG before touching protection on an existing short.
    bag, _short_leg_opt = build_combo(ib, candidate, today)
    if live.combo_quote_guard_enabled:
        combo_quote = fetch_combo_execution_quote(
            ib, bag, timeout_seconds=live.combo_quote_timeout_seconds,
        )
        combo_decision = protect_credit_limit(limit, combo_quote)
        entry_diagnostics.update({
            "combo_bid": combo_quote.bid,
            "combo_ask": combo_quote.ask,
            "combo_requested_credit": round(limit, 2),
            "combo_collar_credit": combo_decision.collar_credit,
            "combo_quote_reason": combo_decision.reason,
        })
        if not combo_decision.ok or combo_decision.allowed_credit is None:
            return None, None, combo_decision.reason or "combo_quote_blocked"
        if combo_decision.allowed_credit != limit:
            limit = combo_decision.allowed_credit
            entry_diagnostics["combo_guard_repriced"] = True
            entry_diagnostics["combo_allowed_credit"] = round(limit, 2)
        else:
            entry_diagnostics["combo_guard_repriced"] = False
    else:
        entry_diagnostics["combo_quote_reason"] = "guard_disabled"

    # Disarm a same-strike native STP only after validation and immediately
    # before routing the additional short position.
    clear_short_leg_backstops(
        ib, candidate, open_spreads, today, dry=dry, reason="pre_entry",
    )
    combo_order = LimitOrder("BUY", contracts, -limit)
    combo_order.tif = "DAY"
    combo_order.account = live.ib_account
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
        entry_diagnostics=entry_diagnostics,
        reference_spot=reference_spot,
        reference_natural_credit=reference_credit,
        reference_short_delta=reference_short_delta,
        signal_timestamp=candidate.timestamp,
    )
    log_event(today, {
        "event": "entry_submitted",
        "tranche_time": now.replace(second=0, microsecond=0).isoformat(),
        "decision_latency_seconds": round(
            max(
                0.0,
                (
                    datetime.now()
                    - now.replace(second=0, microsecond=0, tzinfo=None)
                ).total_seconds(),
            ),
            3,
        ),
        "side": candidate.side,
        "short_strike": candidate.short_strike,
        "long_strike": candidate.long_strike,
        "contracts": contracts,
        "natural_credit": round(nat, 2),
        "limit_credit": round(limit, 2),
        "score": round(candidate.score, 3),
        **entry_diagnostics,
    })
    print(
        f"[{now.isoformat()}] ENTRY working {candidate.side} "
        f"{candidate.short_strike}/{candidate.long_strike} x{contracts} "
        f"natural={nat:.2f} limit={limit:.2f}"
    )
    return None, pending, ""


def submit_paired_condor_entry(
    ib: "IB",
    put_candidate: CandidateRecord,
    call_candidate: CandidateRecord,
    contracts: int,
    config: StrategyConfig,
    today: str,
    live: LiveConfig,
    open_spreads: Sequence[OpenSpread],
    *,
    now: datetime,
    provider: Optional["IBSignalProvider"] = None,
) -> Tuple[List[OpenSpread], str]:
    """Submit an all-or-none four-leg condor entry and return its two children.

    This intentionally has no ladder: repricing a four-leg structure after a
    partial/uncertain fill is not safe.  At the one-lot pilot size, IB's BAG is
    either fully filled or cancelled; a timeout cancels it and records no
    structure.  The parent order's combined credit is persisted on both child
    events through ``condor_id`` for audit and restart reconciliation.
    """
    candidates = (put_candidate, call_candidate)
    for candidate in candidates:
        reference_spot = float(candidate.spot)
        reference_credit = max(float(candidate.credit or 0.0), natural_credit(candidate))
        reference_delta = abs(float(candidate.short_delta or 0.0))
        if provider is not None:
            provider.refresh_candidate_legs(candidate, now)
        ages = provider.leg_quote_ages(candidate) if provider is not None else None
        block = entry_quote_block_reason(candidate, live, leg_ages=ages)
        if block:
            return [], f"paired_condor_{candidate.side}_{block}"
        if provider is not None:
            quality = provider.evaluate_candidate_quality(
                candidate,
                now,
                reference_spot=reference_spot,
                reference_credit=reference_credit,
                reference_short_delta=reference_delta,
            )
            if not quality.ok:
                return [], f"paired_condor_{candidate.side}_entry_quality_{quality.reason}"
    naturals = [natural_credit(candidate) for candidate in candidates]
    total_natural = sum(naturals)
    total_limit = entry_limit_credit(total_natural, live)
    if total_limit <= 0:
        return [], "paired_condor_insufficient_credit"

    # Disarm only the two shorts the BAG will add to, then re-arm each side
    # immediately after a fill or cancellation.
    for candidate in candidates:
        clear_short_leg_backstops(ib, candidate, open_spreads, today, dry=False, reason="paired_condor_pre_entry")
    bag = build_paired_condor_combo(ib, put_candidate, call_candidate, today)
    order = LimitOrder("BUY", contracts, -total_limit)
    order.tif = "DAY"
    order.account = live.ib_account
    trade = ib.placeOrder(bag, order)
    state, reason = _wait_for_combo_order(ib, trade, timeout_sec=8.0)
    if state != "filled":
        if state == "pending":
            ib.cancelOrder(trade.order)
            ib.sleep(0.25)
            reason = "paired_condor_unfilled"
        for candidate in candidates:
            place_or_replace_native_stop_for_short(
                ib, candidate, open_spreads, today, dry=False, live=live,
                config=config, reason="paired_condor_cancelled",
            )
        return [], reason or "paired_condor_rejected"

    condor_id = f"ic-{today}-{uuid.uuid4().hex[:10]}"
    total_fill = abs(float(trade.orderStatus.avgFillPrice or total_limit))
    # Preserve the actual combined fill while assigning a deterministic,
    # non-negative share to each child for the existing per-spread accounting.
    put_fill = round(total_fill * naturals[0] / total_natural, 2) if total_natural else 0.0
    call_fill = round(total_fill - put_fill, 2)
    fills = (put_fill, call_fill)
    spreads: List[OpenSpread] = []
    for candidate, fill in zip(candidates, fills):
        short_sell = candidate.short_quote.bid if candidate.short_quote else 0.0
        long_buy = candidate.long_quote.ask if candidate.long_quote else 0.0
        spread = OpenSpread(
            candidate=candidate, contracts=contracts,
            short_entry_sell=short_sell, long_entry_buy=long_buy,
            stop_price=_round_spx_premium(short_sell * config.stop_multiple),
            combo_order_id=trade.order.orderId, fill_credit=fill, condor_id=condor_id,
        )
        spreads.append(spread)
        log_event(today, {
            "event": "entry", "side": candidate.side, "sleeve": "condor",
            "condor_id": condor_id, "paired_condor": True,
            "short_strike": candidate.short_strike, "long_strike": candidate.long_strike,
            "contracts": contracts, "natural_credit": round(natural_credit(candidate), 2),
            "combined_natural_credit": round(total_natural, 2),
            "combined_limit_credit": round(total_limit, 2),
            "combined_fill_credit": round(total_fill, 2), "credit": fill,
            "short_entry_sell": round(short_sell, 4), "long_entry_buy": round(long_buy, 4),
            "score": round(candidate.score, 3),
        }, live=live)
        place_or_replace_native_stop_for_short(
            ib, candidate, [*open_spreads, *spreads], today, dry=False,
            live=live, config=config, reason="paired_condor_fill",
        )
    print(f"[{now.isoformat()}] PAIRED CONDOR filled {put_candidate.short_strike}/{put_candidate.long_strike} + "
          f"{call_candidate.short_strike}/{call_candidate.long_strike} x{contracts} credit={total_fill:.2f} id={condor_id}")
    return spreads, ""


def _book_filled_spread(
    event: dict,
    pending: PendingEntry,
    *,
    open_spreads: List[OpenSpread],
    config: StrategyConfig,
    sleeve_margin_used: dict,
) -> Tuple[int, float, float]:
    """Append a filled (or partially filled) spread to the local book.

    Shared by the poll resolver and by teardown cancels so the two paths cannot
    drift on stop pricing, partial sizing, or margin accounting.
    Returns (contracts, credit_added, margin).
    """
    spread = pending.spread
    fill_credit = float(event.get("credit", pending.limit_credit))
    spread.fill_credit = fill_credit
    # Partial fills: shrink local book to filled qty.
    filled_contracts = int(event.get("contracts") or pending.contracts)
    if filled_contracts > 0:
        spread.contracts = filled_contracts
    # Synthetic stop always uses strategy stop_multiple × short premium.
    # Native STP uses live.native_stop_multiple separately (wider backstop).
    if spread.short_entry_sell > 0 and config.stop_multiple > 0:
        spread.stop_price = _round_spx_premium(
            spread.short_entry_sell * config.stop_multiple
        )
    open_spreads.append(spread)
    contracts = spread.contracts
    credit_added = fill_credit * contracts * config.multiplier
    margin = candidate_margin_per_contract(pending.candidate, config) * contracts
    sleeve = pending.sleeve or "core"
    sleeve_margin_used[sleeve] = sleeve_margin_used.get(sleeve, 0.0) + margin
    return contracts, credit_added, margin


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
        contracts, credit_added, margin = _book_filled_spread(
            event,
            pending,
            open_spreads=open_spreads,
            config=config,
            sleeve_margin_used=sleeve_margin_used,
        )
        filled = 1
        partial_note = " partial" if event.get("partial") else ""
        print(
            f"[{datetime.now().isoformat()}] ENTRY filled{partial_note} {pending.candidate.side} "
            f"{pending.candidate.short_strike}/{pending.candidate.long_strike} "
            f"x{contracts} fill={pending.spread.fill_credit:.2f} "
            f"stop={pending.spread.stop_price:.2f} "
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
    *,
    decision_at: Optional[datetime] = None,
) -> Tuple[bool, float, Dict[str, float]]:
    """Phase 4: limit at ask + buffer, escalate to MKT if unfilled.

    Returns ``(ok, fill_price, timings)`` where timings carry the
    decision→submission and submission→fill latencies for the trade record.
    """
    decision_ts = decision_at or datetime.now()
    # Cancel aggregated native STP on this short before the protective BUY.
    clear_short_leg_backstops(
        ib, candidate, open_spreads, today, dry=False, reason="synthetic_stop",
    )
    short_opt = _short_option(ib, candidate, today)
    limit_px = _stop_limit_price(short_ask, live)
    limit_order = LimitOrder("BUY", spread.contracts, limit_px)
    limit_order.tif = "DAY"
    limit_order.account = live.ib_account
    submitted_ts = datetime.now()
    timings: Dict[str, float] = {
        "decision_to_submission_seconds": round(
            max(0.0, (submitted_ts - decision_ts).total_seconds()), 3,
        ),
    }
    trade = ib.placeOrder(short_opt, limit_order)
    state, reason = _wait_for_order(ib, trade, timeout_sec=live.stop_limit_timeout_seconds)
    if state == "filled":
        fill = float(trade.orderStatus.avgFillPrice or limit_px)
        timings["submission_to_fill_seconds"] = round(
            max(0.0, (datetime.now() - submitted_ts).total_seconds()), 3,
        )
        return True, fill, timings
    if state == "pending":
        ib.cancelOrder(trade.order)
        ib.sleep(0.25)
    print(f"[{datetime.now().isoformat()}] STOP limit unfilled @ {limit_px:.2f} ({reason}) — MKT fallback")
    timings["escalated_to_market"] = 1.0
    stop_market_order = Order(action="BUY", totalQuantity=spread.contracts, orderType="MKT")
    stop_market_order.account = live.ib_account
    mkt = ib.placeOrder(short_opt, stop_market_order)
    mkt_state, _ = _wait_for_order(ib, mkt, timeout_sec=5.0)
    if mkt_state == "filled":
        fill = float(mkt.orderStatus.avgFillPrice or short_ask)
        timings["submission_to_fill_seconds"] = round(
            max(0.0, (datetime.now() - submitted_ts).total_seconds()), 3,
        )
        return True, fill, timings
    return False, short_ask, timings


def _reset_stop_confirmation(spread: OpenSpread) -> None:
    spread.stop_confirm_count = 0
    spread.stop_breach_since = None
    spread.stop_confirmed_seconds = 0.0
    spread.stop_last_breach_eval = None
    spread.stop_confirm_paused = False


def effective_stop_confirm_seconds(
    *,
    ask: float,
    stop_price: float,
    spot: float,
    short_type: str,
    short_strike: float,
    live: LiveConfig,
) -> Tuple[float, str]:
    """Dynamic confirmation window for a breached short-leg stop.

    Minor noise keeps the full ``stop_confirm_seconds`` (backtest parity);
    a severe premium breach or the underlying crossing the short strike is a
    decisive move where waiting only buys additional overrun.
    """
    base = float(live.stop_confirm_seconds)
    immediate_ratio = float(getattr(live, "stop_immediate_ask_ratio", 0.0) or 0.0)
    if immediate_ratio > 0 and stop_price > 0 and ask >= stop_price * immediate_ratio:
        return 0.0, "severe_breach"
    if bool(getattr(live, "stop_immediate_on_underlying_cross", False)) and spot > 0:
        is_call = short_type.upper() in {"C", "CALL"}
        crossed = spot >= short_strike if is_call else spot <= short_strike
        if crossed:
            return 0.0, "underlying_cross"
    fast_ratio = float(getattr(live, "stop_fast_confirm_ask_ratio", 0.0) or 0.0)
    if fast_ratio > 0 and stop_price > 0 and ask >= stop_price * fast_ratio:
        fast = float(getattr(live, "stop_fast_confirm_seconds", base) or base)
        return min(base, fast), "fast_breach"
    return base, "standard"


def manage_stops(
    ib: "IB",
    open_spreads: Sequence[OpenSpread],
    quotes: Sequence[OptionQuote],
    config: StrategyConfig,
    today: str,
    dry: bool,
    live: LiveConfig,
    *,
    now: Optional[datetime] = None,
    quote_age_fn=None,
    spot: float = 0.0,
    connection_ok: bool = True,
) -> List[OpenSpread]:
    """Short-leg stop with freshness-gated, dynamic confirmation; limit→MKT.

    Confirmation time accrues on ``stop_confirmed_seconds`` only while the
    short-leg quote is fresh (``quote_age_fn`` within
    ``live.stop_quote_max_age_seconds``) and the upstream connection is
    healthy; stale marks or a TWS outage PAUSE the clock instead of letting it
    run against frozen prices (2026-08-04: timers advanced through seven
    silent 1100 disconnections). Each loop step credits at most
    ``live.stop_confirm_max_step_seconds`` so a stalled loop cannot complete a
    confirmation in one jump.

    The confirmation window itself is dynamic (``effective_stop_confirm_seconds``):
    120s for minor noise, a short window for a fast premium breach, and
    immediate execution for severe breaches or an underlying strike crossing.
    When stop_confirm_seconds ≤ 0, falls back to config.stop_confirmation_count.
    """
    if not config.use_short_leg_stops or not quotes:
        return []
    clock = now or datetime.now()
    lookup = {(q.option_type, q.strike): q for q in quotes}
    max_quote_age = float(getattr(live, "stop_quote_max_age_seconds", 0.0) or 0.0)
    max_step = float(getattr(live, "stop_confirm_max_step_seconds", 0.0) or 0.0)
    newly_stopped: List[OpenSpread] = []
    for spread in open_spreads:
        if spread.stopped or spread.closed:
            continue
        sq = lookup.get((spread.candidate.short_type, spread.candidate.short_strike))
        if sq is None or sq.ask <= 0:
            _reset_stop_confirmation(spread)
            continue
        if sq.ask < spread.stop_price:
            _reset_stop_confirmation(spread)
            continue

        # --- breached: decide whether this evaluation may advance the clock --
        quote_age: Optional[float] = None
        if quote_age_fn is not None:
            try:
                quote_age = quote_age_fn(
                    spread.candidate.short_type, spread.candidate.short_strike,
                )
            except Exception:
                quote_age = None
        quote_fresh = (
            max_quote_age <= 0
            or quote_age_fn is None
            or (quote_age is not None and quote_age <= max_quote_age)
        )
        evaluable = connection_ok and quote_fresh

        if spread.stop_breach_since is None:
            spread.stop_breach_since = clock
            spread.stop_confirmed_seconds = 0.0
            spread.stop_last_breach_eval = None
        spread.stop_confirm_count += 1

        if not evaluable:
            # Pause: keep accrued confirmation, stop the clock, never fire
            # against a stale mark or during an upstream outage.
            spread.stop_last_breach_eval = None
            if not spread.stop_confirm_paused:
                spread.stop_confirm_paused = True
                log_event(today, {
                    "event": "stop_confirm_paused",
                    "side": spread.candidate.side,
                    "short_strike": spread.candidate.short_strike,
                    "reason": "connection_unhealthy" if not connection_ok else "stale_quote",
                    "quote_age_seconds": (
                        round(quote_age, 3) if quote_age is not None else None
                    ),
                    "confirmed_seconds": round(spread.stop_confirmed_seconds, 3),
                }, live=live)
            continue
        if spread.stop_confirm_paused:
            spread.stop_confirm_paused = False
            log_event(today, {
                "event": "stop_confirm_resumed",
                "side": spread.candidate.side,
                "short_strike": spread.candidate.short_strike,
                "confirmed_seconds": round(spread.stop_confirmed_seconds, 3),
            }, live=live)

        if spread.stop_last_breach_eval is not None:
            step = (clock - spread.stop_last_breach_eval).total_seconds()
            if max_step > 0:
                step = min(step, max_step)
            spread.stop_confirmed_seconds += max(step, 0.0)
        spread.stop_last_breach_eval = clock

        effective_spot = spot if spot > 0 else float(sq.underlying_price or 0.0)
        confirm_needed, confirm_mode = effective_stop_confirm_seconds(
            ask=sq.ask,
            stop_price=spread.stop_price,
            spot=effective_spot,
            short_type=spread.candidate.short_type,
            short_strike=float(spread.candidate.short_strike),
            live=live,
        )
        if confirm_needed > 0 or live.stop_confirm_seconds > 0:
            if spread.stop_confirmed_seconds < confirm_needed:
                continue
        elif (
            confirm_mode in ("standard", "fast_breach")
            and spread.stop_confirm_count < config.stop_confirmation_count
        ):
            # Poll-count fallback (stop_confirm_seconds <= 0); immediate
            # modes (severe breach / underlying cross) still fire at once.
            continue

        breach_started = spread.stop_breach_since
        actual_confirm_seconds = spread.stop_confirmed_seconds
        spread.stopped = True
        fill_px = sq.ask
        stop_timings: Dict[str, float] = {}
        if not dry and HAS_IB and ib is not None:
            net_before = (
                _short_leg_ib_net(ib, spread.candidate, today, live)
                if live.confirm_stop_against_ib
                else None
            )
            ok, fill_px, stop_timings = _buy_short_leg_stop(
                ib, spread, spread.candidate, today, sq.ask, live, open_spreads,
                decision_at=clock,
            )
            if not ok:
                spread.stopped = False
                _reset_stop_confirmation(spread)
                continue
            if live.confirm_stop_against_ib and net_before is not None:
                net_after = _short_leg_ib_net(ib, spread.candidate, today, live)
                # Short nets are negative; covering should raise net by ~contracts.
                if net_after is None or net_after < net_before + int(spread.contracts):
                    spread.stopped = False
                    _reset_stop_confirmation(spread)
                    log_event(today, {
                        "event": "stop_unconfirmed",
                        "side": spread.candidate.side,
                        "short_strike": spread.candidate.short_strike,
                        "contracts": spread.contracts,
                        "net_before": net_before,
                        "net_after": net_after,
                        "fill": round(fill_px, 2),
                        "condor_id": spread.condor_id,
                    }, live=live)
                    print(
                        f"[{datetime.now().isoformat()}] STOP UNCONFIRMED "
                        f"{spread.candidate.short_strike} net {net_before}→{net_after} "
                        f"(expected +{spread.contracts})"
                    )
                    continue
        spread.stop_fill_price = fill_px
        spread.stop_breach_since = None
        spread.stop_last_breach_eval = None
        newly_stopped.append(spread)
        log_event(
            today,
            {
                "event": "stop",
                "side": spread.candidate.side,
                "short_strike": spread.candidate.short_strike,
                "long_strike": spread.candidate.long_strike,
                "stop_price": round(spread.stop_price, 2),
                "short_ask": round(sq.ask, 2),
                "stop_fill": round(fill_px, 2),
                "contracts": spread.contracts,
                "confirm_seconds": live.stop_confirm_seconds,
                "confirm_mode": confirm_mode,
                "confirm_needed_seconds": round(confirm_needed, 3),
                "breach_started": (
                    breach_started.isoformat() if breach_started is not None else None
                ),
                "actual_confirm_seconds": round(actual_confirm_seconds, 3),
                "trigger_excess": round(max(0.0, sq.ask - spread.stop_price), 4),
                "trigger_excess_dollars": round(
                    max(0.0, sq.ask - spread.stop_price)
                    * spread.contracts
                    * config.multiplier,
                    2,
                ),
                "fill_excess": round(max(0.0, fill_px - spread.stop_price), 4),
                "fill_excess_dollars": round(
                    max(0.0, fill_px - spread.stop_price)
                    * spread.contracts
                    * config.multiplier,
                    2,
                ),
                **stop_timings,
                "condor_id": spread.condor_id,
                "dry": dry,
            },
            live=live,
        )
        print(
            f"[{datetime.now().isoformat()}] STOP short {spread.candidate.short_strike} "
            f"ask={sq.ask:.2f}>={spread.stop_price:.2f} fill={fill_px:.2f} "
            f"mode={confirm_mode} (keep long wing)"
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
        flatten_order = Order(action="SELL", totalQuantity=spread.contracts, orderType="MKT")
        flatten_order.account = cfg.ib_account
        trade = ib.placeOrder(bag, flatten_order)
        state, reason = _wait_for_order(ib, trade, timeout_sec=wait_sec)
        if state != "filled" and retry_mkt:
            if state == "pending":
                try:
                    ib.cancelOrder(trade.order)
                    ib.sleep(0.25)
                except Exception:
                    pass
            retry_order = Order(action="SELL", totalQuantity=spread.contracts, orderType="MKT")
            retry_order.account = cfg.ib_account
            trade = ib.placeOrder(bag, retry_order)
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
                "condor_id": spread.condor_id,
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
                "condor_id": spread.condor_id,
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
            ib_nets = fetch_ib_spxw_positions(ib, today, account=cfg.ib_account)
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
# Per-process run identity (git commit, config hash, run id, pid) injected
# into every structured record so intraday restarts are auditable.
_RUN_METADATA: dict = {}


def set_run_metadata(metadata: dict) -> None:
    _RUN_METADATA.clear()
    _RUN_METADATA.update(metadata)


def log_event(today: str, event: dict, *, live: Optional[LiveConfig] = None) -> None:
    day_dir = LIVE_DIR / today
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / "fills.jsonl"
    event = {"ts": datetime.now().isoformat(), **event, **_RUN_METADATA}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event) + "\n")
    cfg = live or ACTIVE
    if bool(getattr(cfg, "slack_notify_enabled", True)):
        maybe_notify_safety_event(
            str(event.get("event") or ""),
            event,
            enabled=True,
        )


def _short_leg_ib_net(
    ib: "IB", candidate: CandidateRecord, today: str, live: LiveConfig
) -> Optional[int]:
    """Signed IB net for the short leg (short < 0). None if IB unavailable."""
    try:
        nets = fetch_ib_spxw_positions(ib, today, account=live.ib_account)
    except Exception:
        return None
    right = "P" if candidate.short_type == "PUT" else "C"
    expiry = today.replace("-", "")
    key = LegKey(right=right, strike=float(candidate.short_strike), expiry=expiry)
    return int(nets.get(key, 0))


def run_flatten_audit(
    ib: Optional["IB"],
    today: str,
    *,
    dry: bool,
    live: LiveConfig,
) -> dict:
    """Post-flatten / session audit: IB should show no SPXW residual."""
    if dry or ib is None or not HAS_IB:
        payload = {"event": "flatten_audit", "ib_flat": True, "residual_ib_lots": 0, "dry": True}
        log_event(today, payload, live=live)
        return payload
    try:
        nets = fetch_ib_spxw_positions(ib, today, account=live.ib_account)
    except Exception as exc:
        payload = {
            "event": "flatten_audit",
            "ib_flat": False,
            "residual_ib_lots": -1,
            "error": repr(exc),
        }
        log_event(today, payload, live=live)
        return payload
    residual = sum(abs(v) for v in nets.values())
    payload = {
        "event": "flatten_audit",
        "ib_flat": residual == 0,
        "residual_ib_lots": residual,
    }
    log_event(today, payload, live=live)
    if residual > 0:
        print(
            f"[{datetime.now().isoformat()}] FLATTEN AUDIT FAIL — "
            f"IB still shows {residual} SPXW lot(s)"
        )
    return payload


def log_tranche(today: str, record: dict) -> None:
    day_dir = LIVE_DIR / today
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / "tranches.jsonl"
    payload = {"ts": datetime.now().isoformat(), **record, **_RUN_METADATA}
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
        **_RUN_METADATA,
        "live_config": asdict(live),
        "sizing_scheme": sizing_scheme,
        "strategy_config": asdict(config),
    }
    (day_dir / "config.json").write_text(
        json.dumps(snapshot, indent=2, default=str), encoding="utf-8"
    )
    # ``config.json`` is the latest resolved state; the append-only history
    # preserves restarts and intraday size/config changes for as-run replay.
    with (day_dir / "config_history.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(snapshot, default=str) + "\n")


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
    """Flat baseline, optionally reshaped by time-of-day and VIX elevated band.

    Scale factors are combined and rounded once, and any positive product is
    floored at one contract: a small baseline must never be silently rounded
    to zero by a late-day decay multiplier (2026-08-04: ``round(1 × 0.45) = 0``
    suppressed all afternoon entries). Only an explicit 0.0 schedule
    multiplier (or a zero baseline) halts entries; per-entry hard caps such as
    ``max_contracts_per_tranche`` still bound the upside.
    """
    base = int(config.baseline_contracts)
    if base <= 0:
        return 0
    multiplier = 1.0
    if sizing_schedule:
        multiplier *= schedule_multiplier(now.time(), sizing_schedule)
    multiplier *= vix_sizing_multiplier
    if multiplier <= 0:
        return 0
    return max(1, round(base * multiplier))


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
    side_stop_counts: Optional[Dict[str, int]] = None,
    vix_sizing_multiplier: float = 1.0,
) -> Tuple[int, float, float, float, Optional[PendingEntry], str]:
    """Evaluate one entry tranche; log diagnostics; submit any selected spreads."""
    stop_counts = side_stop_counts if side_stop_counts is not None else {}
    cooldown_map = side_stop_cooldown_until if side_stop_cooldown_until is not None else {}
    # Credit/margin booked by the abandoned-entry teardown below, folded into
    # this tranche's totals so the caller's caps stay accurate.
    carried_credit = 0.0
    carried_margin = 0.0
    if pending_entry is not None and not dry:
        cancelled_cand = pending_entry.candidate
        carried = cancel_pending_entry(
            ib,
            pending_entry,
            today,
            reason="new_tranche",
            dry=dry,
            open_spreads=open_spreads,
            config=config,
            sleeve_margin_used=sleeve_margin_used,
        )
        carried_credit = carried.credit_added
        carried_margin = carried.margin
        # Re-arm STPs that were disarmed for the abandoned working entry. When
        # the entry filled instead, the spread is now in open_spreads and this
        # arms the STP for the real position.
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
    paired_condor_candidates: List[CandidateRecord] = []
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
            # Live condors are a single four-leg structure.  Keep the two
            # simulator records together until paired routing below.
            paired_condor_candidates = condor_selected
            records.extend(condor_records)

    executed = 0
    credit_added = carried_credit
    margin_added = carried_margin
    order_rejected = False
    entry_working = False
    new_pending: Optional[PendingEntry] = None

    for cand in selected:
        if cand.short_quote is None or cand.long_quote is None:
            continue
        cooldown_reason = side_stop_cooldown_block_reason(
            cand.side, now, config, cooldown_map
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
            }, live=live)
            continue
        risk_reason = live_entry_risk_block(
            cand,
            open_spreads,
            now=now,
            config=config,
            side_stop_cooldown_until=cooldown_map,
            side_stop_counts=stop_counts,
        )
        if risk_reason:
            cand.status = "blocked"
            cand.reason = risk_reason
            log_event(today, {
                "event": "entry_blocked",
                "side": cand.side,
                "short_strike": cand.short_strike,
                "long_strike": cand.long_strike,
                "reason": risk_reason,
            }, live=live)
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
        cap_reason = open_risk_block_reason(
            cand,
            open_spreads,
            contracts=contracts,
            max_open_contracts=live.max_open_contracts,
            max_open_per_side=live.max_open_per_side,
            max_open_same_strike=live.max_open_same_strike,
            max_open_same_strike_multiple=live.max_open_same_strike_multiple,
            max_open_side_cluster=live.max_open_side_cluster,
            side_cluster_points=live.side_cluster_points,
        )
        if cap_reason:
            cand.status = "blocked"
            cand.reason = cap_reason
            log_event(today, {
                "event": "entry_blocked",
                "side": cand.side,
                "short_strike": cand.short_strike,
                "long_strike": cand.long_strike,
                "reason": cap_reason,
                "contracts": contracts,
            }, live=live)
            continue
        if live.use_pre_entry_buying_power and ib is not None and not dry and HAS_IB:
            acct = fetch_account_snapshot(ib, account=live.ib_account)
            need = candidate_margin_per_contract(cand, config) * contracts
            bp = acct.buying_power
            if bp is None or bp < need:
                cand.status = "blocked"
                cand.reason = "buying_power"
                log_event(today, {
                    "event": "entry_blocked",
                    "side": cand.side,
                    "short_strike": cand.short_strike,
                    "long_strike": cand.long_strike,
                    "reason": "buying_power",
                    "buying_power": bp,
                    "needed": round(need, 2),
                }, live=live)
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
                "short_entry_sell": round(spread.short_entry_sell, 4),
                "long_entry_buy": round(spread.long_entry_buy, 4),
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

    # The simulator represents an IC as two candidate verticals.  Never send
    # those through the serial vertical loop: route a complete pair as one BAG.
    # This branch is unreachable unless the explicit live feature gate is on.
    if (
        bool(getattr(live, "enable_paired_condor_live", False))
        and not dry
        and not entry_working
        and len(paired_condor_candidates) == 2
    ):
        put_cand = next((c for c in paired_condor_candidates if c.short_type == "PUT"), None)
        call_cand = next((c for c in paired_condor_candidates if c.short_type == "CALL"), None)
        if put_cand is not None and call_cand is not None:
            pair_contracts = min(
                _size_with_caps(put_cand, config, gross_credit_sold + credit_added, daily_credit_cap,
                                sleeve_margin_used, portfolio_margin_used + margin_added),
                _size_with_caps(call_cand, config, gross_credit_sold + credit_added, daily_credit_cap,
                                sleeve_margin_used, portfolio_margin_used + margin_added),
                live.max_contracts_per_tranche,
            )
            pair_reason = ""
            for candidate in (put_cand, call_cand):
                if pair_reason:
                    break
                pair_reason = side_stop_cooldown_block_reason(candidate.side, now, config, cooldown_map)
                if not pair_reason:
                    pair_reason = live_entry_risk_block(
                        candidate, open_spreads, now=now, config=config,
                        side_stop_cooldown_until=cooldown_map, side_stop_counts=stop_counts,
                    )
                if not pair_reason:
                    pair_reason = open_risk_block_reason(
                        candidate, open_spreads, contracts=pair_contracts,
                        max_open_contracts=live.max_open_contracts,
                        max_open_per_side=live.max_open_per_side,
                        max_open_same_strike=live.max_open_same_strike,
                        max_open_same_strike_multiple=live.max_open_same_strike_multiple,
                        max_open_side_cluster=live.max_open_side_cluster,
                        side_cluster_points=live.side_cluster_points,
                    )
            if pair_contracts <= 0:
                pair_reason = pair_reason or "risk_blocked_size_cap"
            if not pair_reason and live.use_pre_entry_buying_power and ib is not None and HAS_IB:
                required = sum(candidate_margin_per_contract(c, config) * pair_contracts
                               for c in (put_cand, call_cand))
                acct = fetch_account_snapshot(ib, account=live.ib_account)
                if acct.buying_power is None or acct.buying_power < required:
                    pair_reason = "buying_power"
            if pair_reason:
                for candidate in (put_cand, call_cand):
                    candidate.status = "blocked"
                    candidate.reason = f"paired_condor_{pair_reason}"
                log_event(today, {"event": "entry_blocked", "sleeve": "condor",
                                  "reason": f"paired_condor_{pair_reason}"}, live=live)
            else:
                pair_spreads, pair_reason = submit_paired_condor_entry(
                    ib, put_cand, call_cand, pair_contracts, config, today, live,
                    open_spreads, now=now, provider=provider,
                )
                if pair_spreads:
                    open_spreads.extend(pair_spreads)
                    executed += 2
                    credit_added += sum((s.fill_credit or 0.0) * s.contracts * config.multiplier
                                        for s in pair_spreads)
                    for spread in pair_spreads:
                        margin = candidate_margin_per_contract(spread.candidate, config) * spread.contracts
                        sleeve_margin_used["condor"] = sleeve_margin_used.get("condor", 0.0) + margin
                        margin_added += margin
                else:
                    order_rejected = True
                    for candidate in (put_cand, call_cand):
                        candidate.status = "blocked"
                        candidate.reason = f"paired_condor_{pair_reason}"
                    log_event(today, {"event": "order_rejected", "sleeve": "condor",
                                      "reason": f"paired_condor_{pair_reason}"}, live=live)

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
    if live.mode == "live" and not str(live.ib_account or "").strip():
        raise SystemExit("live mode requires an explicit ib_account in LiveConfig.")

    config, sizing_schedule = resolve_strategy_config(live)
    config = apply_live_risk_overlays(config, live)
    set_run_metadata(build_run_metadata(asdict(live), asdict(config)))
    today_date = datetime.now().date()
    today = today_date.isoformat()
    dry = live.mode == "dry"
    needs_signals = gates_require_signals(config)
    if live.mode == "live":
        # Fail loud if OPRA/index missing — never silently weaken quote guards.
        live.auto_fallback_delayed = False

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
          f"stop={config.stop_multiple}x/{live.stop_confirm_seconds:.0f}s "
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
    session_execution_type = execution_type(live.mode)
    log_event(today, {"event": "session_start", "mode": live.mode,
                      "execution_type": session_execution_type, "profile": live.profile,
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
    connection_health = ConnectionHealthMonitor()

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
            ib_log = setup_ib_logging(today, live)
            port = live.port or (7497 if live.mode == "paper" else 7496)
            ib.connect(live.host, port, clientId=live.client_id)
            register_ib_error_handler(ib, today, health=connection_health)
            print(f"IB connected (port {port}, market_data_type={live.market_data_type} requested)")
            print(f"IB messages -> {ib_log} and data/live/{today}/ib_errors.jsonl")
            print(f"Tranche diagnostics -> data/live/{today}/tranches.jsonl")
            if baselines_core is not None:
                print(f"Signal baselines -> {live.baselines_path}")
            if live.use_account_guards and not dry:
                acct = fetch_account_snapshot(ib, account=live.ib_account)
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
                ib, live, config, baselines_core=baselines_core, session_vix=vix_open, today=today
            )

        # Rebuild open book from today's fills and verify against IB positions.
        recovered = recover_session_book(
            today=today,
            stop_multiple=config.stop_multiple,
            OpenSpread=OpenSpread,
            CandidateRecord=CandidateRecord,
            ib=ib if (ib is not None and not dry) else None,
            account=live.ib_account,
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

        # Build the initial stream only after recovery so every open-position
        # leg is reserved inside the market-data budget before the first mark.
        missing_recovery_quotes: Optional[List[Tuple[str, float]]] = None
        if isinstance(provider, IBSignalProvider):
            wait_for_market_open(live, today, ib=ib)
            provider.set_open_spread_legs(open_spreads)
            provider.start()
            missing_recovery_quotes = []
            if open_spreads:
                missing_recovery_quotes = provider.wait_for_open_spread_quotes(
                    live.recovery_quote_warmup_seconds
                )
                if missing_recovery_quotes:
                    print(
                        f"[{datetime.now().isoformat()}] RECOVERY QUOTE WARN: "
                        "no fresh markable quote after warmup for "
                        + ", ".join(
                            f"{right}{strike:g}"
                            for right, strike in missing_recovery_quotes
                        )
                    )
                else:
                    print(
                        f"[{datetime.now().isoformat()}] recovered-position "
                        "quotes ready for all open legs"
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
        if (
            recovered_halt_is_mark_only(governor)
            and missing_recovery_quotes == []
        ):
            cleared_reasons = list(governor.halt_reasons)
            entries_halted = False
            print(
                f"[{datetime.now().isoformat()}] governor clear — "
                "recovered mark-only halt after all open legs warmed"
            )
            log_event(today, {
                "event": "governor_clear",
                "reason": "recovery_quotes_ready",
                "cleared_reasons": cleared_reasons,
            }, live=live)
        clear_flatten_path = consume_clear_flatten_halt(today)
        if clear_flatten_path is not None:
            flatten_to_clear = filter_cleared_flatten_reasons(governor.halt_reasons)
            kept_reasons = [
                r for r in governor.halt_reasons if r not in set(flatten_to_clear)
            ]
            residual_ib_lots = 0
            ib_check_error: Optional[str] = None
            if ib is not None and HAS_IB and not dry:
                try:
                    nets = fetch_ib_spxw_positions(ib, today, account=live.ib_account)
                except Exception as exc:
                    ib_check_error = repr(exc)
                else:
                    residual_ib_lots = sum(abs(v) for v in nets.values())
            if not flatten_to_clear:
                print(
                    f"[{datetime.now().isoformat()}] CLEAR_FLATTEN_HALT ignored — "
                    f"no flatten halt to clear (reasons={governor.halt_reasons})"
                )
            elif open_spreads:
                print(
                    f"[{datetime.now().isoformat()}] CLEAR_FLATTEN_HALT ignored — "
                    f"recovered book still holds {len(open_spreads)} open spread(s)"
                )
            elif ib_check_error is not None:
                print(
                    f"[{datetime.now().isoformat()}] CLEAR_FLATTEN_HALT ignored — "
                    f"could not verify IB is flat: {ib_check_error}"
                )
            elif residual_ib_lots:
                print(
                    f"[{datetime.now().isoformat()}] CLEAR_FLATTEN_HALT ignored — "
                    f"IB still shows {residual_ib_lots} same-day SPXW lot(s)"
                )
            else:
                print(
                    f"[{datetime.now().isoformat()}] governor clear — "
                    f"operator CLEAR_FLATTEN_HALT removed {flatten_to_clear} "
                    "after confirming a flat book"
                    + (f" (kept {kept_reasons})" if kept_reasons else "")
                )
                log_event(today, {
                    "event": "governor_clear",
                    "reason": "operator_clear_flatten",
                    "cleared_reasons": flatten_to_clear,
                    "kept_reasons": kept_reasons,
                }, live=live)
                flattened = False
                entries_halted = bool(kept_reasons)
            try:
                clear_flatten_path.unlink()
            except OSError as exc:
                print(
                    f"[{datetime.now().isoformat()}] WARN could not remove "
                    f"{clear_flatten_path}: {exc!r}"
                )
        clear_stale_path = consume_clear_stale_halt(today)
        if clear_stale_path is not None:
            stale_to_clear = filter_cleared_stale_reasons(governor.halt_reasons)
            other_reasons = [
                r for r in governor.halt_reasons if r not in set(stale_to_clear)
            ]
            if flattened:
                print(
                    f"[{datetime.now().isoformat()}] CLEAR_STALE_HALT ignored — "
                    f"session is flattened ({clear_stale_path})"
                )
            elif not stale_to_clear:
                print(
                    f"[{datetime.now().isoformat()}] CLEAR_STALE_HALT ignored — "
                    f"no stale_quotes halt to clear (reasons={governor.halt_reasons})"
                )
            else:
                print(
                    f"[{datetime.now().isoformat()}] governor clear — "
                    f"operator CLEAR_STALE_HALT removed {stale_to_clear}"
                    + (f" (kept {other_reasons})" if other_reasons else "")
                )
                log_event(today, {
                    "event": "governor_clear",
                    "reason": "operator_clear_stale_quotes",
                    "cleared_reasons": stale_to_clear,
                    "kept_reasons": other_reasons,
                }, live=live)
                entries_halted = flattened or bool(other_reasons)
            try:
                clear_stale_path.unlink()
            except OSError as exc:
                print(
                    f"[{datetime.now().isoformat()}] WARN could not remove "
                    f"{clear_stale_path}: {exc!r}"
                )
        side_stop_cooldown_until: Dict[str, datetime] = dict(governor.side_stop_cooldown_until)
        side_stop_counts: Dict[str, int] = recover_side_stop_counts(fills_events)
        for warn in governor.warnings:
            print(f"[{datetime.now().isoformat()}] GOVERNOR WARN: {warn}")
        if entries_halted or flattened or side_stop_cooldown_until or side_stop_counts:
            print(
                f"[{datetime.now().isoformat()}] governor recovered — "
                f"halted={entries_halted} flattened={flattened} "
                f"cooldowns={list(side_stop_cooldown_until)} "
                f"stop_counts={side_stop_counts}"
            )
            log_event(today, {
                "event": "governor_recovered",
                "entries_halted": entries_halted,
                "flattened": flattened,
                "halt_reasons": governor.halt_reasons,
                "cooldowns": {k: v.isoformat() for k, v in side_stop_cooldown_until.items()},
                "side_stop_counts": side_stop_counts,
            }, live=live)
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
        # Stop-wake observability: session count of idles cut short by a short
        # leg ticking into its stop band. Surfaced in the risk snapshot so the
        # event-driven path can be verified without per-wake log spam.
        stop_wake_count = 0
        last_stop_wake: Optional[Tuple[str, float, float]] = None
        last_quotes = []
        last_marked_pnl = 0.0
        last_native_stop_verify_at = datetime.now()
        last_account_guard_at = datetime.now() - timedelta(seconds=live.account_guard_poll_seconds)
        mark_bad_since: Optional[datetime] = None
        disconnect_halt = False
        upstream_halt = False
        stale_tracker = StaleQuoteTracker()
        last_heartbeat_at = datetime.now() - timedelta(seconds=live.heartbeat_seconds)
        last_risk_snapshot_at = datetime.now() - timedelta(seconds=live.risk_snapshot_seconds)
        ib_port = live.port or (7497 if live.mode == "paper" else 7496)
        ib_provider: Optional[IBSignalProvider] = None
        if isinstance(provider, IBSignalProvider):
            ib_provider = provider

        def _cancel_pending(pending_obj, reason: str) -> CancelBooking:
            """Tear down a working entry, booking anything that filled first.

            A fill can beat the cancel; booking it here puts the spread under
            stop management (and inside the flatten set) instead of leaving an
            unmanaged short leg for the audit to discover.
            """
            nonlocal gross_credit_sold, portfolio_margin_used
            booking = cancel_pending_entry(
                ib,
                pending_obj,
                today,
                reason=reason,
                dry=dry,
                open_spreads=open_spreads,
                config=config,
                sleeve_margin_used=sleeve_margin_used,
            )
            if booking.contracts:
                gross_credit_sold += booking.credit_added
                portfolio_margin_used += booking.margin
            return booking

        def _trigger_flatten(reason: str, marked_pnl: float) -> FlattenResult:
            nonlocal pending_entry, flattened, entries_halted
            flattened = True
            entries_halted = True
            # Book before flatten_all so a fill that beat the cancel is included
            # in the flatten set rather than surviving the flatten.
            _cancel_pending(pending_entry, reason)
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
            }, live=live)
            if not fres.complete:
                log_event(today, {
                    "event": "flatten_incomplete",
                    "reason": reason,
                    "residual_ib_lots": fres.residual_ib_lots,
                    "failed": fres.failed,
                }, live=live)
            run_flatten_audit(ib, today, dry=dry, live=live)
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
                    }, live=live)
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
                    log_event(today, {"event": "ib_disconnected"}, live=live)
                    _cancel_pending(pending_entry, "disconnect")
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
                    }, live=live)
                    if not outcome.connected:
                        open_risk = [s for s in open_spreads if not s.closed]
                        if open_risk:
                            _trigger_flatten("reconnect_failed", last_marked_pnl)
                        raise SystemExit("IB reconnect failed — exiting")
                    register_ib_error_handler(ib, today, health=connection_health)
                    # Re-check book vs IB after reconnect (fail loud on residual).
                    recovered_chk = recover_session_book(
                        today=today,
                        stop_multiple=config.stop_multiple,
                        OpenSpread=OpenSpread,
                        CandidateRecord=CandidateRecord,
                        ib=ib,
                        account=live.ib_account,
                        fail_on_unmatched=True,
                        cancel_orphans=False,
                    )
                    open_spreads[:] = list(recovered_chk.spreads)
                    if open_spreads and native_stops_enabled(live):
                        rearm_all_native_stops(
                            ib, open_spreads, today, dry=dry, live=live, config=config,
                            reason="post_reconnect",
                        )
                    if ib_provider is not None:
                        ib_provider.ib = ib
                        ib_provider.set_open_spread_legs(open_spreads)
                        ib_provider.start()
                        reconnect_quote_gaps = (
                            ib_provider.wait_for_open_spread_quotes(
                                live.recovery_quote_warmup_seconds
                            )
                        )
                        if reconnect_quote_gaps:
                            print(
                                f"[{datetime.now().isoformat()}] RECONNECT QUOTE WARN: "
                                "no fresh markable quote for "
                                + ", ".join(
                                    f"{right}{strike:g}"
                                    for right, strike in reconnect_quote_gaps
                                )
                            )
                    # Clear only the disconnect-induced halt; keep PnL/account/mark halts.
                    disconnect_halt = False
                    entries_halted = halted_before_disconnect or flattened
                    continue

                # --- Phase C2: upstream connectivity breaker (IB 1100/1101/1102) --
                # TWS can lose its link to IB servers while the local API socket
                # stays connected: quotes freeze but ib.isConnected() is True.
                # Halt entries immediately; stop confirmations pause via the
                # connection_ok flag passed to manage_stops below.
                if (
                    getattr(live, "use_upstream_health_breaker", True)
                    and connect_ib
                    and ib is not None
                    and not dry
                ):
                    for transition in connection_health.consume_transitions():
                        if transition.kind == "upstream_lost":
                            # Gate entries via upstream_halt only; never mutate
                            # entries_halted so PnL/account/mark halts raised
                            # during the outage survive the restore.
                            upstream_halt = True
                            print(
                                f"[{now.isoformat()}] IB UPSTREAM LOST (error "
                                f"{transition.code}) — halting entries, pausing "
                                "stop confirmations until connectivity restores."
                            )
                            log_event(today, {
                                "event": "halt_entries",
                                "reason": "ib_upstream_lost",
                                "ib_error_code": transition.code,
                            }, live=live)
                            lost_pending = pending_entry
                            _cancel_pending(lost_pending, "ib_upstream_lost")
                            pending_entry = None
                            if lost_pending is not None and native_stops_enabled(live):
                                # Best-effort re-arm; verify_native_stops
                                # replaces it post-restore if this fails.
                                place_or_replace_native_stop_for_short(
                                    ib,
                                    lost_pending.candidate,
                                    open_spreads,
                                    today,
                                    dry=dry,
                                    live=live,
                                    config=config,
                                    reason="ib_upstream_lost",
                                )
                        elif transition.kind == "upstream_restored":
                            print(
                                f"[{now.isoformat()}] IB upstream restored (error "
                                f"{transition.code}"
                                + (", market data lost — resubscribing)"
                                   if transition.resubscribe_required else ")")
                            )
                            log_event(today, {
                                "event": "ib_upstream_restored",
                                "ib_error_code": transition.code,
                                "resubscribe_required": transition.resubscribe_required,
                                "outage_was_halting": upstream_halt,
                            }, live=live)
                            if (
                                transition.resubscribe_required
                                and ib_provider is not None
                            ):
                                # 1101: subscriptions are gone; rebuild streams
                                # and require fresh quotes on every open leg.
                                try:
                                    ib_provider.shutdown()
                                except Exception:
                                    pass
                                ib_provider.start()
                                ib_provider.set_open_spread_legs(open_spreads)
                                resub_gaps = ib_provider.wait_for_open_spread_quotes(
                                    live.recovery_quote_warmup_seconds
                                )
                                if resub_gaps:
                                    print(
                                        f"[{datetime.now().isoformat()}] RESUBSCRIBE "
                                        "QUOTE WARN: no fresh markable quote for "
                                        + ", ".join(
                                            f"{right}{strike:g}"
                                            for right, strike in resub_gaps
                                        )
                                    )
                                connection_health.mark_resubscribed()
                            if upstream_halt:
                                # Clear only the upstream-induced entry gate;
                                # PnL/account/mark/stale halts are untouched.
                                upstream_halt = False
                                log_event(today, {
                                    "event": "governor_clear",
                                    "reason": "ib_upstream_restored",
                                }, live=live)

                at_tranche = should_fire_tranche(now, config, traded_tranches)
                if ib_provider is not None:
                    required_changed = ib_provider.set_open_spread_legs(open_spreads)
                    if required_changed:
                        new_leg_gaps = ib_provider.wait_for_open_spread_quotes(
                            live.recovery_quote_warmup_seconds
                        )
                        if new_leg_gaps:
                            print(
                                f"[{now.isoformat()}] OPEN-LEG QUOTE WARN: "
                                "no fresh markable quote for "
                                + ", ".join(
                                    f"{right}{strike:g}"
                                    for right, strike in new_leg_gaps
                                )
                            )
                quotes, signal = provider.fetch(now, at_tranche=at_tranche)
                last_quotes = list(quotes)
                if (
                    at_tranche
                    and ib_provider is not None
                    and ib_provider.last_signal_block_reason == "signal_warming"
                ):
                    # Keep the tranche armed while the deterministic boundary
                    # window collects multiple synchronized observations.
                    at_tranche = False

                if (
                    ib_provider is not None
                    and ib_provider.last_signal_block_reason == "stale_underlying"
                    and not entries_halted
                ):
                    entries_halted = True
                    active_pending = pending_entry
                    _cancel_pending(active_pending, "stale_underlying")
                    pending_entry = None
                    if active_pending is not None and native_stops_enabled(live):
                        place_or_replace_native_stop_for_short(
                            ib,
                            active_pending.candidate,
                            open_spreads,
                            today,
                            dry=dry,
                            live=live,
                            config=config,
                            reason="stale_underlying",
                        )
                    spot_age = ib_provider._stream.spot_age_seconds()
                    print(
                        f"[{now.isoformat()}] HALT new entries "
                        f"(SPX stream stale for {spot_age:.1f}s)."
                    )
                    log_event(today, {
                        "event": "halt_entries",
                        "reason": "stale_underlying",
                        "spot_age_seconds": round(spot_age, 3),
                        "threshold_seconds": live.stale_spot_halt_seconds,
                    }, live=live)
                elif (
                    ib_provider is not None
                    and at_tranche
                    and ib_provider.last_signal_block_reason
                ):
                    reason = ib_provider.last_signal_block_reason
                    print(f"[{now.isoformat()}] SKIP tranche ({reason}).")
                    log_event(today, {
                        "event": "signal_blocked",
                        "reason": reason,
                        "tranche_time": now.replace(second=0, microsecond=0).isoformat(),
                        **ib_provider.last_signal_diagnostics,
                    }, live=live)

                pending_entry, disarm_booking = enforce_native_stop_disarm_budget(
                    ib,
                    pending_entry,
                    open_spreads,
                    today,
                    now=now,
                    dry=dry,
                    live=live,
                    config=config,
                    sleeve_margin_used=sleeve_margin_used,
                )
                if disarm_booking.contracts:
                    gross_credit_sold += disarm_booking.credit_added
                    portfolio_margin_used += disarm_booking.margin

                if pending_entry is not None and ib is not None and not dry:
                    active_pending = pending_entry
                    resolution = None
                    try:
                        quality_block = ""
                        if (
                            ib_provider is not None
                            and active_pending.reference_spot is not None
                            and active_pending.reference_natural_credit is not None
                            and active_pending.reference_short_delta is not None
                        ):
                            check_now = datetime.now()
                            ib_provider.refresh_candidate_legs(active_pending.candidate, check_now)
                            quality = ib_provider.evaluate_candidate_quality(
                                active_pending.candidate,
                                check_now,
                                reference_spot=active_pending.reference_spot,
                                reference_credit=active_pending.reference_natural_credit,
                                reference_short_delta=active_pending.reference_short_delta,
                            )
                            if quality.diagnostics:
                                active_pending.entry_diagnostics = {
                                    **(active_pending.entry_diagnostics or {}),
                                    **quality.diagnostics,
                                }
                            if not quality.ok:
                                quality_block = f"entry_quality_{quality.reason}"
                        pending_entry, resolution = poll_pending_entry(
                            ib,
                            active_pending,
                            live,
                            today,
                            now,
                            log_event=log_event,
                            quality_block_reason=quality_block,
                        )
                        # Belt-and-suspenders: never keep a non-active trade as
                        # pending. A cancel awaiting IB acknowledgement is the
                        # one legitimate inactive-but-pending state.
                        if (
                            pending_entry is not None
                            and resolution is None
                            and not pending_trade_is_active(pending_entry)
                            and not pending_is_awaiting_cancel(pending_entry)
                        ):
                            raise RuntimeError(
                                "pending_entry_inactive "
                                f"status={pending_entry.trade.orderStatus.status!r}"
                            )
                    except Exception as poll_exc:
                        print(
                            f"[{now.isoformat()}] ENTRY poll fault recovered: "
                            f"{poll_exc!r} — clearing pending, re-arming STP"
                        )
                        fault_booking = repair_session_after_entry_fault(
                            ib,
                            active_pending,
                            open_spreads,
                            today,
                            dry=dry,
                            live=live,
                            config=config,
                            error=repr(poll_exc),
                            sleeve_margin_used=sleeve_margin_used,
                        )
                        if fault_booking.contracts:
                            gross_credit_sold += fault_booking.credit_added
                            portfolio_margin_used += fault_booking.margin
                        pending_entry = None
                        resolution = None
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

                stop_quote_age_fn = None
                stop_spot = 0.0
                if ib_provider is not None:
                    stop_quote_age_fn = ib_provider._stream.quote_age_seconds
                    stop_spot = ib_provider._stream.spot()
                newly_stopped = manage_stops(
                    ib, open_spreads, quotes, config, today, dry, live, now=now,
                    quote_age_fn=stop_quote_age_fn,
                    spot=stop_spot,
                    connection_ok=not connection_health.upstream_down,
                )
                if newly_stopped:
                    apply_side_stop_cooldowns(
                        newly_stopped,
                        config=config,
                        now=now,
                        side_stop_cooldown_until=side_stop_cooldown_until,
                    )
                    for spread in newly_stopped:
                        side = spread.candidate.side
                        side_stop_counts[side] = side_stop_counts.get(side, 0) + 1
                        log_event(today, {
                            "event": "side_stop_cooldown_start",
                            "side": side,
                            "minutes": config.same_side_stop_cooldown_minutes,
                            "until": (
                                side_stop_cooldown_until[side].isoformat()
                                if side in side_stop_cooldown_until
                                else None
                            ),
                        }, live=live)

                # --- Stale-quote halt (entries only; never flatten on stale) -----
                quote_age_fn = None
                if ib_provider is not None:
                    quote_age_fn = ib_provider._stream.quote_age_seconds
                stale = evaluate_stale_quotes(
                    stale_tracker,
                    open_spreads,
                    quotes,
                    live=live,
                    quote_age_fn=quote_age_fn,
                )
                if stale.confirmed and not entries_halted:
                    entries_halted = True
                    print(
                        f"[{now.isoformat()}] HALT (stale quotes ×{stale.consecutive}) — "
                        f"{', '.join(stale.stale_legs[:4])}"
                    )
                    log_event(today, {
                        "event": "halt_entries",
                        "reason": "stale_quotes",
                        "consecutive": stale.consecutive,
                        "stale_legs": stale.stale_legs,
                        "threshold": stale.threshold_used,
                    }, live=live)

                # --- Heartbeat for local watchdog --------------------------------
                if (now - last_heartbeat_at).total_seconds() >= live.heartbeat_seconds:
                    last_heartbeat_at = now
                    open_n = sum(1 for s in open_spreads if not s.closed)
                    risk = build_risk_snapshot(
                        open_spreads, quotes, multiplier=config.multiplier,
                    )
                    # Cheap stop-wake telemetry: a session counter plus the last
                    # breaching quote. Zero while flat; rising only when a short
                    # leg trades into its stop band.
                    risk = {
                        **risk,
                        "stop_wake_count": stop_wake_count,
                        "last_stop_wake": (
                            {
                                "option_type": last_stop_wake[0],
                                "strike": last_stop_wake[1],
                                "ask": last_stop_wake[2],
                            }
                            if last_stop_wake is not None
                            else None
                        ),
                    }
                    write_heartbeat(
                        today,
                        open_count=open_n,
                        marked_pnl=last_marked_pnl,
                        entries_halted=entries_halted or upstream_halt,
                        flattened=flattened,
                        extra={
                            "risk": risk,
                            "execution_type": session_execution_type,
                            "connection_health": connection_health.snapshot(),
                            **_RUN_METADATA,
                        },
                    )
                    if (now - last_risk_snapshot_at).total_seconds() >= live.risk_snapshot_seconds:
                        last_risk_snapshot_at = now
                        append_risk_snapshot(
                            today, {**risk, "execution_type": session_execution_type},
                        )

                # --- Phase B: periodic NetLiq overlay -----------------------------
                if (
                    live.use_account_guards
                    and ib is not None
                    and not dry
                    and (now - last_account_guard_at).total_seconds()
                    >= live.account_guard_poll_seconds
                ):
                    last_account_guard_at = now
                    acct = fetch_account_snapshot(ib, account=live.ib_account)
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
                        }, live=live)
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
                        }, live=live)
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
                            }, live=live)
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
                            }, live=live)

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
                        entries_halted=entries_halted or disconnect_halt or upstream_halt,
                        open_spreads=open_spreads,
                        gross_credit_sold=gross_credit_sold,
                        daily_credit_cap=daily_credit_cap,
                        sleeve_margin_used=sleeve_margin_used,
                        portfolio_margin_used=portfolio_margin_used,
                        provider=ib_provider,
                        pending_entry=pending_entry,
                        side_stop_cooldown_until=side_stop_cooldown_until,
                        side_stop_counts=side_stop_counts,
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
                    live_sleep = HAS_IB and ib is not None and ib_is_connected(ib)
                    sleep_fn = ib.sleep if live_sleep else _time.sleep
                    should_wake = None
                    if (
                        getattr(live, "use_stop_wake", True)
                        and ib_provider is not None
                        and live_sleep
                    ):
                        # Arm the open short legs so a tick at/near a stop price
                        # cuts the idle short: immediate-mode stops then fire on
                        # the tick rather than a poll interval later.
                        try:
                            ib_provider.arm_stop_watch(
                                stop_wake_thresholds(open_spreads, live)
                            )
                            # Deliberately not consumed here: a tick that
                            # breached while the loop body was running has not
                            # been evaluated yet, so it must cut this sleep too.
                            should_wake = ib_provider.stop_wake_pending
                        except Exception:
                            should_wake = None
                    interruptible_sleep(
                        self_sleep,
                        sleep_fn=sleep_fn,
                        should_wake=should_wake,
                        slice_seconds=float(
                            getattr(live, "stop_wake_slice_seconds", 0.05) or 0.05
                        ),
                    )
                    if should_wake is not None:
                        # Latch consumed only after the sleep it shortened, so
                        # the next iteration starts clean instead of spinning.
                        try:
                            woken = ib_provider.consume_stop_wake()
                        except Exception:
                            woken = None
                        if woken is not None:
                            stop_wake_count += 1
                            last_stop_wake = woken
        except SystemExit:
            raise
        except Exception as exc:
            open_risk = [s for s in open_spreads if not s.closed]
            # A working entry that filled before the cancel IS open risk, so it
            # must steer this decision — otherwise a partial gets treated as a
            # flat book and is left unmanaged to settle.
            try:
                pending_fill = (
                    teardown_fill_event(pending_entry)
                    if pending_entry is not None
                    else None
                )
            except Exception:
                # Never raise a second exception out of the handler; assume the
                # worst (risk present) so we take the flatten path.
                pending_fill = {"event": "entry", "contracts": 0}
            if not open_risk and pending_fill is None:
                # Pending-entry / loop faults with a flat book must not sticky-halt
                # the session via error_flatten (today's AssertionError path).
                print(
                    f"[{datetime.now().isoformat()}] ENTRY FAULT (book flat): "
                    f"{exc!r} — clearing pending, continuing session end."
                )
                log_event(today, {
                    "event": "entry_fault",
                    "error": repr(exc),
                    "had_pending": pending_entry is not None,
                }, live=live)
                _cancel_pending(pending_entry, "entry_fault")
                pending_entry = None
                # Fall through to session_end / finally — do not re-raise.
            else:
                print(
                    f"[{datetime.now().isoformat()}] ERROR: {exc!r} "
                    f"-- flattening and exiting."
                )
                log_event(
                    today, {"event": "error_flatten", "error": repr(exc)}, live=live,
                )
                _cancel_pending(pending_entry, "error")
                pending_entry = None
                if not dry:
                    # Re-read after booking so a fill that beat the cancel is
                    # included in the flatten set.
                    flatten_all(
                        ib,
                        [s for s in open_spreads if not s.closed],
                        today,
                        dry,
                        live=live,
                    )
                    run_flatten_audit(ib, today, dry=dry, live=live)
                raise

        # End of day: SPXW 0DTE is cash-settled on the close, so open defined-risk
        # spreads are left to settle (matches the backtest's settle-at-close). Only
        # the governor or an error flattens early.
        if last_quotes:
            last_marked_pnl = _mark_book(open_spreads, last_quotes, config).pnl
        if flattened:
            run_flatten_audit(ib, today, dry=dry, live=live)
        log_event(today, {
            "event": "session_end",
            "spreads": len(open_spreads),
            "stopped": sum(1 for s in open_spreads if s.stopped),
            "flattened": flattened,
            "gross_credit_sold": round(gross_credit_sold, 2),
            "marked_pnl": round(last_marked_pnl, 2),
        }, live=live)
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
        # Slack delivery is off-loop and its worker is a daemon thread: drain
        # the backlog here so the final flatten / kill_switch page is not lost
        # at process exit.
        try:
            if not flush_slack(timeout_sec=10.0):
                print(
                    f"[{datetime.now().isoformat()}] WARN: Slack backlog did not "
                    f"drain; dropped={slack_dropped_count()}"
                )
            elif slack_dropped_count():
                print(
                    f"[{datetime.now().isoformat()}] WARN: Slack alerts dropped "
                    f"(queue full): {slack_dropped_count()}"
                )
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
    _mirror_console_to_session_log()
    run(ACTIVE)
