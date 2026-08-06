"""Startup recovery, single-instance lock, and IB SPXW risk fail-loud checks.

Solves the paper/live hazard where a mid-session restart wiped in-memory
``open_spreads`` and left IB positions unmanaged.
"""
from __future__ import annotations

import atexit
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
LIVE_DIR = ROOT / "data" / "live"

# Fill events that mean "the book was force-closed"; each one also records its
# own name as a halt reason, so ``flattened`` is derivable from halt_reasons.
FLATTEN_HALT_REASONS = frozenset({
    "flatten",
    "error_flatten",
    "kill_switch",
    "flatten_incomplete",
})

# Halts caused purely by a data feed going quiet. These carry no risk
# implication once the feed recovers, so they are the only reasons an operator
# clear (CLEAR_STALE_HALT) or the in-loop auto-resume may remove.
STALE_HALT_REASONS = frozenset({
    "stale_quotes",
    "stale_underlying",
})


@dataclass(frozen=True)
class LegKey:
    right: str  # "C" or "P"
    strike: float
    expiry: str  # YYYYMMDD

    def as_tuple(self) -> Tuple[str, float, str]:
        return self.right, self.strike, self.expiry


@dataclass
class RecoveredBook:
    spreads: List[Any]  # List[OpenSpread] — typed loosely to avoid circular imports
    gross_credit_sold: float
    source_entries: int
    ib_matched_legs: int = 0
    warnings: List[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.warnings is None:
            self.warnings = []


@dataclass
class RecoveredGovernor:
    """Risk-governor flags restored from fills.jsonl after a mid-session restart."""

    entries_halted: bool = False
    flattened: bool = False
    halt_reasons: List[str] = None  # type: ignore[assignment]
    side_stop_cooldown_until: Dict[str, datetime] = None  # type: ignore[assignment]
    warnings: List[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.halt_reasons is None:
            self.halt_reasons = []
        if self.side_stop_cooldown_until is None:
            self.side_stop_cooldown_until = {}
        if self.warnings is None:
            self.warnings = []


def lock_path_for(today: str, live_dir: Path = LIVE_DIR) -> Path:
    return live_dir / today / "executor.lock"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
                return True
            return False
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def release_executor_lock(path: Path, *, pid: Optional[int] = None) -> None:
    """Remove lock file if it belongs to this process (or pid override)."""
    owner = pid if pid is not None else os.getpid()
    try:
        if not path.exists():
            return
        payload = json.loads(path.read_text(encoding="utf-8"))
        if int(payload.get("pid", -1)) != owner:
            return
        path.unlink(missing_ok=True)
    except Exception:
        pass


def acquire_executor_lock(
    today: str,
    *,
    live_dir: Path = LIVE_DIR,
    force: bool = False,
) -> Path:
    """Exclusive per-day lock so two executors cannot share a session folder."""
    day_dir = live_dir / today
    day_dir.mkdir(parents=True, exist_ok=True)
    path = lock_path_for(today, live_dir)

    if path.exists() and not force:
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            old_pid = int(existing.get("pid", -1))
        except Exception:
            old_pid = -1
            existing = {}
        if old_pid != os.getpid() and _pid_alive(old_pid):
            raise SystemExit(
                f"another executor is already running for {today} "
                f"(pid={old_pid}, lock={path}). Stop that process or delete the "
                "stale lock only if you are sure it is dead."
            )
        # Stale lock from a crashed process — take over.
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass

    payload = {
        "pid": os.getpid(),
        "started_at": datetime.now().isoformat(),
        "date": today,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _cleanup() -> None:
        release_executor_lock(path)

    atexit.register(_cleanup)
    return path


def _normalize_right(option_type: str) -> str:
    text = str(option_type or "").strip().upper()
    if text in {"C", "CALL"}:
        return "C"
    if text in {"P", "PUT"}:
        return "P"
    raise ValueError(f"unsupported option right {option_type!r}")


def _expiry_yyyymmdd(value: object, fallback: str) -> str:
    text = str(value or "").strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10].replace("-", "")
    if len(text) == 8 and text.isdigit():
        return text
    return fallback.replace("-", "")[:8]


def _side_short_type(side: str) -> str:
    if side == "bull_put":
        return "PUT"
    if side == "bear_call":
        return "CALL"
    raise ValueError(f"unsupported side {side!r}")


def load_fills_events(today: str, live_dir: Path = LIVE_DIR) -> List[dict]:
    path = live_dir / today / "fills.jsonl"
    if not path.exists():
        return []
    events: List[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _parse_event_ts(raw: object, fallback: datetime) -> datetime:
    if raw is None:
        return fallback
    try:
        return datetime.fromisoformat(str(raw).replace("Z", ""))
    except ValueError:
        return fallback


def recover_governor_state(
    events: Sequence[dict],
    *,
    now: Optional[datetime] = None,
    cooldown_minutes: int = 0,
) -> RecoveredGovernor:
    """Rebuild halt/flatten/cooldown flags from persisted fill events.

    Mark-data exception: a validated ``governor_clear`` can remove recovered
    mark-only halt reasons after every open leg has a fresh quote. It never
    clears P&L, account, stop-count, stale-data, flatten, or operator state.

    - Any ``halt_entries`` → entries halted for the rest of the session.
    - Any ``flatten`` / ``error_flatten`` / ``kill_switch`` → flattened + halted.
    - ``entry_fault`` / ``entry_poll_error`` / ``entry_ladder_failed`` do **not**
      sticky-halt (flat-book recoveries).
    - A validated ``operator_clear_flatten`` removes flatten-family reasons only,
      so an unintended flatten can be resumed once IB is confirmed flat.
    - ``side_stop_cooldown_start`` restores ``until`` when still in the future.
    - Bare ``stop`` events derive cooldown as ``ts + cooldown_minutes`` when no
      explicit cooldown event was logged (older sessions).
    """
    clock = now or datetime.now()
    flattened = False
    halt_reasons: set[str] = set()
    cooldowns: Dict[str, datetime] = {}
    warnings: List[str] = []

    for event in events:
        name = event.get("event")
        if name == "halt_entries":
            reason = str(event.get("reason") or "").strip()
            if not reason:
                reason = "daily_loss" if event.get("marked_pnl") is not None else "unspecified"
            halt_reasons.add(reason)
            continue
        if name in FLATTEN_HALT_REASONS:
            flattened = True
            halt_reasons.add(str(name))
            continue
        if name == "governor_clear" and event.get("reason") == "recovery_quotes_ready":
            for reason in event.get("cleared_reasons") or []:
                halt_reasons.discard(str(reason))
            continue
        if name == "governor_clear" and event.get("reason") == "operator_clear_stale_quotes":
            # Explicit operator resume after confirming the feed is healthy.
            # Only stale-data reasons may be removed this way — never flatten/PnL/etc.
            for reason in event.get("cleared_reasons") or []:
                if str(reason) in STALE_HALT_REASONS:
                    halt_reasons.discard(str(reason))
            continue
        if name == "governor_clear" and event.get("reason") == "stale_underlying_recovered":
            # In-loop auto-resume after the spot stream came back healthy.
            halt_reasons.discard("stale_underlying")
            continue
        if name == "governor_clear" and event.get("reason") == "operator_clear_flatten":
            # Explicit operator resume after confirming IB holds no same-day risk.
            # Only flatten-family reasons may be removed — P&L, account, stale and
            # stop-count halts survive and keep entries blocked.
            for reason in event.get("cleared_reasons") or []:
                if str(reason) in FLATTEN_HALT_REASONS:
                    halt_reasons.discard(str(reason))
            flattened = bool(halt_reasons & FLATTEN_HALT_REASONS)
            continue
        if name == "side_stop_cooldown_start":
            side = str(event.get("side") or "")
            until_raw = event.get("until")
            if side and until_raw:
                until = _parse_event_ts(until_raw, clock)
                if until > clock:
                    prev = cooldowns.get(side)
                    if prev is None or until > prev:
                        cooldowns[side] = until
            continue
        if name == "stop" and cooldown_minutes > 0:
            side = str(event.get("side") or "")
            if not side:
                continue
            ts = _parse_event_ts(event.get("ts"), clock)
            until = ts + timedelta(minutes=cooldown_minutes)
            if until > clock:
                prev = cooldowns.get(side)
                if prev is None or until > prev:
                    cooldowns[side] = until

    entries_halted = flattened or bool(halt_reasons)
    if flattened and not halt_reasons:
        warnings.append("flattened implies entries_halted")

    return RecoveredGovernor(
        entries_halted=entries_halted,
        flattened=flattened,
        halt_reasons=sorted(halt_reasons),
        side_stop_cooldown_until=cooldowns,
        warnings=warnings,
    )


def recovered_halt_is_mark_only(governor: RecoveredGovernor) -> bool:
    """True only when every recovered halt came from degraded mark data."""
    if governor.flattened or not governor.entries_halted or not governor.halt_reasons:
        return False
    return all(
        reason.startswith("mark_") or reason == "partial_mark"
        for reason in governor.halt_reasons
    )


def _spread_key(side: str, short_strike: float, long_strike: float) -> Tuple[str, float, float]:
    return side, float(short_strike), float(long_strike)


def open_entry_events_from_fills(events: Sequence[dict]) -> List[dict]:
    """Return still-active entry events after stops / flatten (chronological).

    A ``stop`` keeps the entry in the book marked ``stopped=True`` so the long
    wing remains expected inventory (manage-only). ``flatten`` clears everything.
    """
    open_entries: List[dict] = []
    for event in events:
        name = event.get("event")
        if name == "flatten":
            open_entries.clear()
            continue
        if name == "entry":
            row = dict(event)
            row["stopped"] = False
            open_entries.append(row)
            continue
        if name == "stop":
            key = _spread_key(
                str(event.get("side")),
                float(event.get("short_strike")),
                float(event.get("long_strike")),
            )
            for idx in range(len(open_entries) - 1, -1, -1):
                row = open_entries[idx]
                if row.get("stopped"):
                    continue
                row_key = _spread_key(
                    str(row.get("side")),
                    float(row.get("short_strike")),
                    float(row.get("long_strike")),
                )
                if row_key == key:
                    row["stopped"] = True
                    if event.get("stop_fill") is not None:
                        row["stop_fill"] = event.get("stop_fill")
                    if event.get("stop_price") is not None:
                        row["stop_fill_price"] = event.get("stop_fill")
                        row["logged_stop_price"] = event.get("stop_price")
                    break
    return open_entries


def _require_short_entry_sell(event: dict) -> float:
    """Stop distance must use short-leg premium, never net credit.

    Falling back to net credit made recovered stops ~3× too tight
    (e.g. credit 0.60 → stop 1.80 instead of short 2.50 → stop 7.50).
    """
    raw = event.get("short_entry_sell")
    if raw is None or raw == "":
        raise ValueError(
            "entry event missing short_entry_sell — refuse to rebuild stop from "
            "net credit (would set stop ≈ 3× credit instead of 3× short premium). "
            f"side={event.get('side')} short={event.get('short_strike')} "
            f"long={event.get('long_strike')}"
        )
    short_sell = float(raw)
    if short_sell <= 0:
        raise ValueError(
            f"entry event has non-positive short_entry_sell={short_sell!r} "
            f"side={event.get('side')} short={event.get('short_strike')}"
        )
    return short_sell


def rebuild_open_spreads_from_entries(
    entries: Sequence[dict],
    *,
    today: str,
    stop_multiple: float,
    OpenSpread,
    CandidateRecord,
) -> Tuple[List[Any], float]:
    """Materialize OpenSpread objects from persisted entry events.

    Stop price is always ``short_entry_sell × stop_multiple`` (production 3×
    short premium). Net credit is never used as a short-premium proxy.
    """
    spreads: List[Any] = []
    gross = 0.0
    expiry = today.replace("-", "")
    for event in entries:
        side = str(event["side"])
        short_type = _side_short_type(side)
        contracts = int(event.get("contracts") or 0)
        if contracts <= 0:
            continue
        credit = float(event.get("credit") or event.get("limit_credit") or 0.0)
        short_strike = float(event["short_strike"])
        long_strike = float(event["long_strike"])
        short_sell = _require_short_entry_sell(event)
        raw_long = event.get("long_entry_buy")
        if raw_long is None or raw_long == "":
            long_buy = max(short_sell - credit, 0.0)
        else:
            long_buy = float(raw_long)
        stop_price = short_sell * float(stop_multiple)
        ts_raw = event.get("ts") or event.get("timestamp")
        try:
            ts = datetime.fromisoformat(str(ts_raw).replace("Z", "")) if ts_raw else datetime.now()
        except ValueError:
            ts = datetime.now()
        candidate = CandidateRecord(
            timestamp=ts,
            side=side,
            status="recovered",
            reason="session_recovery",
            score=float(event.get("score") or 0.0),
            expiry=_expiry_yyyymmdd(event.get("expiry"), expiry),
            short_type=short_type,
            short_strike=short_strike,
            long_strike=long_strike,
            short_delta=None,
            long_delta=None,
            spot=float(event.get("spot") or 0.0),
            distance_pct=0.0,
            width=abs(long_strike - short_strike),
            credit=credit,
            credit_to_width=(credit / abs(long_strike - short_strike)) if long_strike != short_strike else 0.0,
            stop_loss_to_credit=float(stop_multiple),
            straddle_residual_z=0.0,
            skew_z=0.0,
            term_ratio_z=0.0,
            trend_score=0.0,
            realized_vs_implied_z=0.0,
            contracts=contracts,
            sleeve=str(event.get("sleeve") or "core"),
        )
        spread = OpenSpread(
            candidate=candidate,
            contracts=contracts,
            short_entry_sell=short_sell,
            long_entry_buy=long_buy,
            stop_price=stop_price,
            fill_credit=credit,
        )
        if event.get("stopped"):
            spread.stopped = True
            fill = event.get("stop_fill")
            if fill is not None:
                try:
                    spread.stop_fill_price = float(fill)
                except (TypeError, ValueError):
                    spread.stop_fill_price = float(stop_price)
            else:
                spread.stop_fill_price = float(stop_price)
        spreads.append(spread)
        if not event.get("stopped"):
            gross += credit * contracts * 100.0
    return spreads, gross


def expected_leg_net_from_spreads(spreads: Sequence[Any], today: str) -> Dict[LegKey, int]:
    """Net option lots implied by recovered verticals (short −contracts, long +contracts).

    Stopped (short covered, long wing kept) contribute only the long wing.
    Fully closed spreads contribute nothing.
    """
    expiry_fallback = today.replace("-", "")
    nets: Dict[LegKey, int] = {}
    for spread in spreads:
        if getattr(spread, "closed", False):
            continue
        cand = spread.candidate
        right = _normalize_right(cand.short_type)
        expiry = _expiry_yyyymmdd(cand.expiry, expiry_fallback)
        contracts = int(spread.contracts)
        long_key = LegKey(right=right, strike=float(cand.long_strike), expiry=expiry)
        if getattr(spread, "stopped", False):
            nets[long_key] = nets.get(long_key, 0) + contracts
            continue
        short_key = LegKey(right=right, strike=float(cand.short_strike), expiry=expiry)
        nets[short_key] = nets.get(short_key, 0) - contracts
        nets[long_key] = nets.get(long_key, 0) + contracts
    return {k: v for k, v in nets.items() if v != 0}


def mark_stopped_wings_from_ib(
    spreads: Sequence[Any],
    ib_nets: Dict[LegKey, int],
    today: str,
) -> List[str]:
    """If IB shows short flat but long wing still held, mark spread stopped.

    Happens when native STP fired while the executor was down. Returns warning
    strings for each wing marked. Leaves unexplained residuals to the caller.
    """
    expiry_fallback = today.replace("-", "")
    warnings: List[str] = []
    for spread in spreads:
        if getattr(spread, "closed", False) or getattr(spread, "stopped", False):
            continue
        cand = spread.candidate
        right = _normalize_right(cand.short_type)
        expiry = _expiry_yyyymmdd(cand.expiry, expiry_fallback)
        contracts = int(spread.contracts)
        short_key = LegKey(right=right, strike=float(cand.short_strike), expiry=expiry)
        long_key = LegKey(right=right, strike=float(cand.long_strike), expiry=expiry)
        ib_short = int(ib_nets.get(short_key, 0))
        ib_long = int(ib_nets.get(long_key, 0))
        # Intact vertical: short=-N, long=+N
        if ib_short == -contracts and ib_long == contracts:
            continue
        # Native STP covered short; long wing remains.
        if ib_short == 0 and ib_long == contracts:
            spread.stopped = True
            spread.stop_fill_price = float(getattr(spread, "stop_price", 0.0) or 0.0)
            warnings.append(
                f"marked stopped wing {cand.side} {cand.short_strike}/{cand.long_strike} "
                f"x{contracts} (IB short flat, long still held — likely native STP while down)"
            )
    return warnings


def fetch_ib_spxw_positions(
    ib: Any, today: str, *, account: Optional[str] = None
) -> Dict[LegKey, int]:
    """Net SPXW option positions from IB (signed: short < 0, long > 0)."""
    expiry_today = today.replace("-", "")
    try:
        ib.reqPositions()
        ib.sleep(1.0)
    except Exception:
        pass
    nets: Dict[LegKey, int] = {}
    positions = list(getattr(ib, "positions", lambda: [])())
    for item in positions:
        if account and str(getattr(item, "account", "") or "") != account:
            continue
        contract = getattr(item, "contract", None)
        if contract is None:
            continue
        sec_type = str(getattr(contract, "secType", "") or "").upper()
        if sec_type != "OPT":
            continue
        symbol = str(getattr(contract, "symbol", "") or "").upper()
        trading_class = str(getattr(contract, "tradingClass", "") or "").upper()
        local = str(getattr(contract, "localSymbol", "") or "").upper()
        if symbol not in {"SPX", "SPXW"} and "SPXW" not in local and trading_class not in {"SPXW", "SPX"}:
            continue
        # SAME-DAY ONLY (strategy isolation): this engine trades 0DTE, so only
        # today's expiry is session inventory. Long-dated SPX/XSP puts belong to
        # other strategies (e.g. ls-algo Bucket 5 Production B, orderRef
        # namespace "B5P|") and must never be treated as 0DTE risk, matched
        # against spreads, or flattened by this executor.
        expiry = _expiry_yyyymmdd(getattr(contract, "lastTradeDateOrContractMonth", ""), expiry_today)
        if expiry != expiry_today:
            continue
        try:
            right = _normalize_right(getattr(contract, "right", ""))
            strike = float(getattr(contract, "strike"))
            qty = int(round(float(getattr(item, "position", 0) or 0)))
        except Exception:
            continue
        if qty == 0:
            continue
        key = LegKey(right=right, strike=strike, expiry=expiry)
        nets[key] = nets.get(key, 0) + qty
    return {k: v for k, v in nets.items() if v != 0}


def unmatched_ib_risk(
    ib_nets: Dict[LegKey, int],
    expected_nets: Dict[LegKey, int],
) -> Dict[LegKey, int]:
    """Return residual IB lots not explained by recovered book."""
    keys = set(ib_nets) | set(expected_nets)
    residual: Dict[LegKey, int] = {}
    for key in keys:
        diff = int(ib_nets.get(key, 0)) - int(expected_nets.get(key, 0))
        if diff != 0:
            residual[key] = diff
    return residual


def format_leg_nets(nets: Dict[LegKey, int]) -> str:
    if not nets:
        return "(none)"
    parts = []
    for key in sorted(nets, key=lambda k: (k.expiry, k.right, k.strike)):
        parts.append(f"{key.expiry} {key.right}{key.strike:g}={nets[key]:+d}")
    return ", ".join(parts)


def cancel_orphan_open_orders(
    ib: Any, today: str, *, account: Optional[str] = None
) -> int:
    """Cancel working non-filled orders left from a prior crashed session.

    Cancel hygiene (strategy isolation): scope is strictly this engine's own
    inventory — SPX/SPXW option (or BAG) orders expiring TODAY. Orders from
    other strategies are never touched; in particular any order whose orderRef
    is in the ls-algo Bucket 5 namespace (``B5P|``) or on other underlyings
    (e.g. XSP long-dated puts) must survive a 0DTE session restart.
    """
    expiry_today = today.replace("-", "")
    cancelled = 0
    try:
        trades = list(ib.openTrades())
    except Exception:
        try:
            ib.reqAllOpenOrders()
            ib.sleep(0.5)
            trades = list(ib.openTrades())
        except Exception:
            return 0
    for trade in trades:
        contract = getattr(trade, "contract", None)
        order = getattr(trade, "order", None)
        if contract is None or order is None:
            continue
        if account and str(getattr(order, "account", "") or "") != account:
            continue
        status = str(getattr(getattr(trade, "orderStatus", None), "status", "") or "")
        if status.upper() in {"FILLED", "CANCELLED", "APICANCELLED", "INACTIVE"}:
            continue
        order_ref = str(getattr(order, "orderRef", "") or "")
        if order_ref.startswith("B5P|"):
            continue  # foreign strategy namespace — never cancel
        symbol = str(getattr(contract, "symbol", "") or "").upper()
        sec_type = str(getattr(contract, "secType", "") or "").upper()
        local = str(getattr(contract, "localSymbol", "") or "").upper()
        trading_class = str(getattr(contract, "tradingClass", "") or "").upper()
        is_spx_family = (
            symbol in {"SPX", "SPXW"}
            or "SPXW" in local
            or trading_class in {"SPXW", "SPX"}
        )
        if sec_type not in {"OPT", "BAG"} or not is_spx_family:
            continue
        if sec_type == "OPT":
            expiry = _expiry_yyyymmdd(
                getattr(contract, "lastTradeDateOrContractMonth", ""), expiry_today
            )
            if expiry != expiry_today:
                continue  # long-dated SPX puts are not 0DTE inventory
        try:
            ib.cancelOrder(order)
            cancelled += 1
        except Exception:
            continue
    if cancelled:
        try:
            ib.sleep(0.5)
        except Exception:
            pass
    return cancelled


def recover_session_book(
    *,
    today: str,
    stop_multiple: float,
    OpenSpread,
    CandidateRecord,
    ib: Any = None,
    account: Optional[str] = None,
    live_dir: Path = LIVE_DIR,
    cancel_orphans: bool = True,
    fail_on_unmatched: bool = True,
    manage_only_on_stopped_wings: bool = True,
) -> RecoveredBook:
    """Rebuild open book from fills.jsonl and verify against IB positions."""
    events = load_fills_events(today, live_dir)
    entries = open_entry_events_from_fills(events)
    try:
        spreads, gross = rebuild_open_spreads_from_entries(
            entries,
            today=today,
            stop_multiple=stop_multiple,
            OpenSpread=OpenSpread,
            CandidateRecord=CandidateRecord,
        )
    except ValueError as exc:
        raise SystemExit(
            f"session recovery refused — cannot rebuild stops safely: {exc}"
        ) from exc
    warnings: List[str] = []
    ib_matched = 0

    if ib is not None and cancel_orphans:
        n_cancelled = cancel_orphan_open_orders(ib, today, account=account)
        if n_cancelled:
            warnings.append(f"cancelled {n_cancelled} orphan open order(s) from prior run")

    if ib is not None:
        ib_nets = fetch_ib_spxw_positions(ib, today, account=account)
        if manage_only_on_stopped_wings and spreads:
            wing_warns = mark_stopped_wings_from_ib(spreads, ib_nets, today)
            warnings.extend(wing_warns)
        expected = expected_leg_net_from_spreads(spreads, today)
        # Count legs that match exactly.
        for key, qty in expected.items():
            if ib_nets.get(key) == qty:
                ib_matched += 1
        residual = unmatched_ib_risk(ib_nets, expected)
        if residual:
            detail = (
                f"IB SPXW risk not explained by recovered fills.\n"
                f"  expected: {format_leg_nets(expected)}\n"
                f"  ib:       {format_leg_nets(ib_nets)}\n"
                f"  residual: {format_leg_nets(residual)}\n"
                "Manually flatten or reconcile in TWS, then restart."
            )
            if fail_on_unmatched:
                raise SystemExit(detail)
            warnings.append(detail)
        elif ib_nets and not spreads:
            # Positions exist but fills missing — always fatal when fail_on_unmatched.
            detail = (
                f"IB has SPXW positions but fills.jsonl has no open entries.\n"
                f"  ib: {format_leg_nets(ib_nets)}"
            )
            if fail_on_unmatched:
                raise SystemExit(detail)
            warnings.append(detail)
        elif spreads and not ib_nets:
            warnings.append(
                "fills.jsonl has open entries but IB shows no SPXW positions "
                "(positions may have been flattened manually or settled)."
            )
            # Drop phantom local book so we don't manage risk that isn't there.
            spreads = []
            gross = 0.0

    return RecoveredBook(
        spreads=spreads,
        gross_credit_sold=gross,
        source_entries=len(entries),
        ib_matched_legs=ib_matched,
        warnings=warnings,
    )
