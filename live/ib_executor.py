"""Interactive Brokers live/paper executor for the SPX 0DTE vertical-spread strategy.

This reuses the backtest engine's selection and risk logic (``StrategyConfig``,
``select_candidate_entries``, the short-leg stop, and the daily-loss flatten
governor) so live decisions match the validated backtest. Market data plugs in
through ``SignalProvider`` -- an IB implementation skeleton is included; you can
also feed it from ThetaData for signals while routing execution through IB.

Modes:
  dry   -- compute and log intended trades, place nothing (safe default).
  paper -- route orders to IB paper (default port 7497).
  live  -- route orders to IB live (default port 7496). Start with --contract-scale small.

Run:
  python live/ib_executor.py --profile best --mode dry --account-equity 13000000
"""
from __future__ import annotations

import argparse
import json
import sys
import time as _time
from dataclasses import dataclass
from datetime import datetime, time
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

from strategy_profiles import PROFILES, scale_profile  # noqa: E402


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
    them from the backtest pipeline (``--baselines-path``) and combine with the
    live ATM straddle / skew / term readings.

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


def place_spread(ib: "IB", candidate: CandidateRecord, contracts: int, today: str, dry: bool) -> OpenSpread:
    """Place the spread as a net-credit limit combo and attach a short-leg stop."""
    bag, short_leg_opt = build_combo(ib, candidate, today)
    net_credit = round(candidate.credit, 2)
    spread = OpenSpread(
        candidate=candidate,
        contracts=contracts,
        short_entry_sell=candidate.short_quote.bid if candidate.short_quote else 0.0,
        long_entry_buy=candidate.long_quote.ask if candidate.long_quote else 0.0,
        stop_price=(candidate.short_quote.bid if candidate.short_quote else 0.0) * 2.0,
    )
    if dry:
        return spread

    # SELL the combo to collect the net credit (limit at the candidate credit).
    combo_order = LimitOrder("SELL", contracts, net_credit)
    combo_order.orderType = "LMT"
    trade = ib.placeOrder(bag, combo_order)
    spread.combo_order_id = trade.order.orderId

    # Stop on the SHORT leg only: buy it back if its price hits stop_multiple x entry.
    stop = StopOrder("BUY", contracts, round(spread.stop_price, 2))
    stop_trade = ib.placeOrder(short_leg_opt, stop)
    spread.stop_order_id = stop_trade.order.orderId
    return spread


def flatten_all(ib: "IB", open_spreads: Sequence[OpenSpread], today: str, dry: bool) -> None:
    """Daily-loss flatten governor: close every open spread immediately."""
    for spread in open_spreads:
        if spread.closed:
            continue
        if not dry and HAS_IB:
            bag, short_leg_opt = build_combo(ib, spread.candidate, today)
            ib.placeOrder(bag, Order(action="BUY", totalQuantity=spread.contracts, orderType="MKT"))
        spread.closed = True


# --------------------------------------------------------------------------- #
# Fill logging
# --------------------------------------------------------------------------- #
def log_event(today: str, event: dict) -> None:
    day_dir = LIVE_DIR / today
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / "fills.jsonl"
    event = {"ts": datetime.now().isoformat(), **event}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event) + "\n")


# --------------------------------------------------------------------------- #
# Main run loop
# --------------------------------------------------------------------------- #
def build_config(profile_name: str, account_equity: float, contract_scale: float) -> StrategyConfig:
    if profile_name not in PROFILES:
        raise SystemExit(f"unknown profile {profile_name}; choose from {sorted(PROFILES)}")
    kwargs = scale_profile(PROFILES[profile_name], account_equity, contract_scale)
    return StrategyConfig(account_equity=account_equity, **kwargs)


def run(args: argparse.Namespace) -> None:
    config = build_config(args.profile, args.account_equity, args.contract_scale)
    today = datetime.now().date().isoformat()
    dry = args.mode == "dry"

    print(f"[{datetime.now().isoformat()}] mode={args.mode} profile={args.profile} "
          f"equity=${args.account_equity:,.0f} baseline_contracts={config.baseline_contracts} "
          f"flatten_on_loss={config.flatten_on_daily_loss}")
    log_event(today, {"event": "session_start", "mode": args.mode, "profile": args.profile,
                      "baseline_contracts": config.baseline_contracts})

    ib = None
    provider: SignalProvider
    connect_ib = (args.mode in ("paper", "live")) or (dry and args.dry_with_ib)
    if not connect_ib:
        if dry:
            print("Dry mode without IB: logging intended trades with neutral signals.")
        provider = _NeutralProvider()
    else:
        if not HAS_IB:
            raise SystemExit("ib_insync required for IB connection: pip install ib_insync")
        ib = IB()
        port = args.port or (7497 if args.mode == "paper" else 7496)
        ib.connect(args.host, port, clientId=args.client_id)
        provider = IBSignalProvider(ib, Path(args.baselines_path) if args.baselines_path else None)

    open_spreads: List[OpenSpread] = []
    gross_credit_sold = 0.0
    daily_credit_cap = config.account_equity * config.daily_credit_cap_pct
    daily_loss_limit = -config.account_equity * config.daily_loss_limit_pct
    portfolio_margin_used = 0.0
    sleeve_margin_used = {"core": 0.0, "exploratory": 0.0, "condor": 0.0,
                          "one_dte": 0.0, "trend_debit": 0.0, "long_put_hedge": 0.0}
    halted = False

    end_time = time(16, 0)
    while datetime.now().time() <= end_time:
        now = datetime.now()
        quotes, signal = provider.fetch(now)

        marked = _mark_book(open_spreads, quotes, config)
        if not halted and marked <= daily_loss_limit:
            halted = True
            print(f"[{now.isoformat()}] DAILY LOSS LIMIT hit (marked ${marked:,.0f}); flattening.")
            flatten_all(ib, open_spreads, today, dry)
            log_event(today, {"event": "daily_loss_flatten", "marked_pnl": marked})

        if not halted and signal is not None and is_entry_time(now, config):
            base_contracts = config.baseline_contracts
            selected, _records = select_candidate_entries(quotes, signal, base_contracts, config)
            for cand in selected:
                if cand.short_quote is None or cand.long_quote is None:
                    continue
                contracts = _size_with_caps(cand, config, gross_credit_sold, daily_credit_cap,
                                            sleeve_margin_used, portfolio_margin_used)
                if contracts <= 0:
                    continue
                spread = place_spread(ib, cand, contracts, today, dry)
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
                      f"x{contracts} credit={cand.credit:.2f} score={cand.score:.2f}{' (dry)' if dry else ''}")

        _time.sleep(args.poll_seconds)

    # End of day: stops + settlement handled by IB; just flatten any stragglers.
    if not dry:
        flatten_all(ib, [s for s in open_spreads if not s.closed], today, dry)
        if ib is not None:
            ib.disconnect()
    log_event(today, {"event": "session_end", "spreads": len(open_spreads),
                      "gross_credit_sold": round(gross_credit_sold, 2)})
    print(f"[{datetime.now().isoformat()}] session end. spreads={len(open_spreads)} "
          f"gross_credit=${gross_credit_sold:,.0f}")


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
    lookup = {(q.expiry, q.option_type, q.strike): q for q in quotes}
    total = 0.0
    for spread in open_spreads:
        if spread.closed:
            continue
        cand = spread.candidate
        sq = lookup.get((quotes[0].expiry if quotes else "", cand.short_type, cand.short_strike)) if quotes else None
        lq = lookup.get((quotes[0].expiry if quotes else "", cand.long_type, cand.long_strike)) if quotes else None
        if sq is None or lq is None:
            continue
        per_contract = spread.short_entry_sell - sq.ask - spread.long_entry_buy + lq.bid
        total += per_contract * spread.contracts * config.multiplier
    return total


class _NeutralProvider:
    """Dry-run provider with no IB: emits neutral signals so the loop exercises
    end-to-end wiring without market data."""

    def fetch(self, now: datetime):
        return [], SignalSnapshot(timestamp=now)


def main() -> None:
    parser = argparse.ArgumentParser(description="IBKR live/paper executor for SPX 0DTE vertical spreads.")
    parser.add_argument("--profile", default="best", choices=sorted(PROFILES))
    parser.add_argument("--mode", default="dry", choices=["dry", "paper", "live"])
    parser.add_argument("--account-equity", type=float, default=13_000_000)
    parser.add_argument("--contract-scale", type=float, default=1.0,
                        help="Fractional multiplier on validated size for pilot ramp (e.g. 0.05).")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="0 = auto (7497 paper / 7496 live).")
    parser.add_argument("--client-id", type=int, default=17)
    parser.add_argument("--baselines-path", default="")
    parser.add_argument("--dry-with-ib", action="store_true",
                        help="In dry mode, still connect to IB to read the live chain (places nothing).")
    parser.add_argument("--poll-seconds", type=float, default=30.0)
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
