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
class IBSignalProvider:
    """Pulls the live SPXW 0DTE (+ next-expiry) chain from IB and builds an
    OptionQuote snapshot. Signal z-scores require historical baselines; load
    them from the backtest pipeline (``LiveConfig.baselines_path``) and combine
    with the live ATM straddle / skew / term readings.

    The chain fetch and quote mapping are implemented; the z-score assembly is
    intentionally a single clearly-marked seam so you can wire your preferred
    baseline source without touching order/risk logic.
    """

    def __init__(self, ib: "IB", baselines_path: Optional[Path] = None):
        self.ib = ib
        self.baselines = self._load_baselines(baselines_path) if baselines_path else None
        self._spx = Index("SPX", "CBOE", "USD")
        self.ib.qualifyContracts(self._spx)

    @staticmethod
    def _load_baselines(path: Path) -> dict:
        if not path.exists():
            raise FileNotFoundError(f"baselines file not found: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _spx_spot(self) -> float:
        [ticker] = self.ib.reqTickers(self._spx)
        return float(ticker.marketPrice() or ticker.close)

    def fetch(self, now: datetime) -> Tuple[List[OptionQuote], Optional[SignalSnapshot]]:
        spot = self._spx_spot()
        today = now.date().isoformat()
        chains = self.ib.reqSecDefOptParams("SPX", "", "IND", self._spx.conId)
        spxw = next((c for c in chains if c.tradingClass == "SPXW"), chains[0] if chains else None)
        if spxw is None:
            return [], None

        # Focus on strikes within a band around spot to limit data lines.
        band = 0.06
        strikes = sorted(s for s in spxw.strikes if abs(s - spot) <= spot * band)
        expiry = today.replace("-", "")
        contracts: List[Contract] = []
        for strike in strikes:
            for right in ("P", "C"):
                opt = Option("SPX", expiry, strike, right, "CBOE", tradingClass="SPXW")
                contracts.append(opt)
        self.ib.qualifyContracts(*contracts)
        tickers = self.ib.reqTickers(*contracts)

        quotes: List[OptionQuote] = []
        for opt, tk in zip(contracts, tickers):
            bid = float(tk.bid) if tk.bid and tk.bid > 0 else 0.0
            ask = float(tk.ask) if tk.ask and tk.ask > 0 else 0.0
            delta = None
            if tk.modelGreeks is not None:
                delta = tk.modelGreeks.delta
            quotes.append(
                OptionQuote(
                    timestamp=now,
                    expiry=today,
                    option_type="CALL" if opt.right == "C" else "PUT",
                    strike=float(opt.strike),
                    bid=bid,
                    ask=ask,
                    delta=delta,
                    underlying_price=spot,
                )
            )

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
                 today: str, dry: bool) -> OpenSpread:
    """Place the spread as a net-credit limit combo.

    The short-leg stop is managed synthetically in the run loop (mark + N-bar
    confirmation) so it matches the backtest; a wide native STP is attached only
    as a crash backstop. The stop level mirrors the simulator:
    ``short_entry_sell * stop_multiple`` on the short-leg ask.
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
        return spread

    bag, short_leg_opt = build_combo(ib, candidate, today)
    # SELL the combo to collect the net credit (limit at the candidate credit).
    combo_order = LimitOrder("SELL", contracts, round(candidate.credit, 2))
    trade = ib.placeOrder(bag, combo_order)
    spread.combo_order_id = trade.order.orderId

    # Native STP backstop only (well beyond the synthetic trigger) in case the
    # process dies; the loop's synthetic stop is the primary, confirmation-gated
    # exit that matches the backtest.
    backstop = round(spread.stop_price * 1.5, 2)
    if backstop > 0:
        stop_trade = ib.placeOrder(short_leg_opt, StopOrder("BUY", contracts, backstop))
        spread.stop_order_id = stop_trade.order.orderId
    return spread


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
            ib.placeOrder(bag, Order(action="BUY", totalQuantity=spread.contracts, orderType="MKT"))
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
        port = live.port or (7497 if live.mode == "paper" else 7496)
        ib.connect(live.host, port, clientId=live.client_id)
        provider = IBSignalProvider(ib, Path(live.baselines_path) if live.baselines_path else None)

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
            if (not entries_halted and signal is not None and is_entry_time(now, config)
                    and tranche_key not in traded_tranches):
                traded_tranches.add(tranche_key)
                base_contracts = _tranche_base_contracts(config, sizing_schedule, now)
                if base_contracts > 0:
                    selected, _records = select_candidate_entries(quotes, signal, base_contracts, config)
                    for cand in selected:
                        if cand.short_quote is None or cand.long_quote is None:
                            continue
                        contracts = _size_with_caps(cand, config, gross_credit_sold, daily_credit_cap,
                                                    sleeve_margin_used, portfolio_margin_used)
                        contracts = min(contracts, live.max_contracts_per_tranche)
                        if contracts <= 0:
                            continue
                        spread = place_spread(ib, cand, contracts, config, today, dry)
                        open_spreads.append(spread)
                        if cand.credit > 0:
                            gross_credit_sold += cand.credit * contracts * config.multiplier
                        margin = candidate_margin_per_contract(cand, config) * contracts
                        sleeve = cand.sleeve or "core"
                        sleeve_margin_used[sleeve] = sleeve_margin_used.get(sleeve, 0.0) + margin
                        portfolio_margin_used += margin
                        log_event(today, {"event": "entry", "side": cand.side, "sleeve": sleeve,
                                          "short_strike": cand.short_strike, "long_strike": cand.long_strike,
                                          "contracts": contracts, "credit": round(cand.credit, 2),
                                          "score": round(cand.score, 3), "dry": dry})
                        print(f"[{now.isoformat()}] ENTRY {cand.side} {cand.short_strike}/{cand.long_strike} "
                              f"x{contracts} credit={cand.credit:.2f} score={cand.score:.2f}"
                              f"{' (dry)' if dry else ''}")

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
        lq = lookup.get((cand.long_type, cand.long_strike))
        if lq is None:
            continue
        if spread.stopped:
            # Short already bought back; only the long wing marks from here.
            per_contract = spread.short_entry_sell - spread.stop_price - spread.long_entry_buy + lq.bid
        else:
            sq = lookup.get((cand.short_type, cand.short_strike))
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
