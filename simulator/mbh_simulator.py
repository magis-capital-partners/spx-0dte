from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


def parse_timestamp(value: str) -> datetime:
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def _float_or_none(value: str | None) -> Optional[float]:
    if value is None or value == "":
        return None
    return float(value)


def normalize_option_type(value: str) -> str:
    value = value.strip().upper()
    if value in {"C", "CALL"}:
        return "CALL"
    if value in {"P", "PUT"}:
        return "PUT"
    raise ValueError(f"Unsupported option type: {value}")


def compact_option_type(value: str) -> str:
    normalized = normalize_option_type(value)
    return "C" if normalized == "CALL" else "P"


@dataclass(frozen=True)
class OptionQuote:
    timestamp: datetime
    expiry: str
    option_type: str
    strike: float
    bid: float
    ask: float
    delta: Optional[float] = None
    iv: Optional[float] = None
    underlying_price: Optional[float] = None

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    def intrinsic(self, spot: float) -> float:
        option_type = normalize_option_type(self.option_type)
        if option_type == "CALL":
            return max(spot - self.strike, 0.0)
        if option_type == "PUT":
            return max(self.strike - spot, 0.0)
        raise ValueError(f"Unsupported option type: {self.option_type}")


@dataclass(frozen=True)
class SignalSnapshot:
    timestamp: datetime
    straddle_residual_z: float = 0.0
    skew_z: float = 0.0
    term_ratio_z: float = 0.0
    trend_score: float = 0.0
    realized_vs_implied_z: float = 0.0
    vix: Optional[float] = None


@dataclass(frozen=True)
class TradeInstruction:
    side: str
    contracts: int
    model: str = "model"


@dataclass
class CandidateRecord:
    timestamp: datetime
    side: str
    status: str
    reason: str
    score: float
    expiry: str
    short_type: str
    short_strike: float
    long_strike: float
    short_delta: Optional[float]
    long_delta: Optional[float]
    spot: float
    distance_pct: float
    width: float
    credit: float
    credit_to_width: float
    stop_loss_to_credit: float
    straddle_residual_z: float
    skew_z: float
    term_ratio_z: float
    trend_score: float
    realized_vs_implied_z: float
    short_quote: Optional[OptionQuote] = None
    long_quote: Optional[OptionQuote] = None
    contracts: int = 0
    sleeve: str = ""


@dataclass(frozen=True)
class StrategyConfig:
    target_expiry: Optional[str] = None
    account_equity: float = 1_000_000.0
    baseline_contracts: int = 16
    target_abs_delta: float = 0.20
    min_abs_delta: float = 0.15
    max_abs_delta: float = 0.25
    wing_width: float = 25.0
    wing_selection_mode: str = "target_delta"
    target_long_abs_delta: float = 0.05
    min_wing_width: float = 25.0
    max_wing_width: float = 400.0
    stop_multiple: float = 2.0
    daily_credit_cap_pct: float = 0.015
    daily_loss_limit_pct: float = 0.0225
    flatten_on_daily_loss: bool = False
    # Optional deeper trigger for the flatten governor. When > 0, new entries
    # still halt at daily_loss_limit_pct, but open positions are only
    # force-flattened once the deeper flatten_loss_limit_pct is breached. This
    # avoids whipsawing out of volatile-but-recovering days while still capping
    # genuine tail losses. When 0, flatten triggers at daily_loss_limit_pct.
    flatten_loss_limit_pct: float = 0.0
    fee_per_contract: float = 0.79
    multiplier: int = 100
    entry_interval_minutes: int = 15
    entry_start: time = time(9, 32)
    entry_end: time = time(15, 30)
    force_flat_time: time = time(16, 0)
    straddle_cheap_threshold: float = -1.0
    skew_extreme_threshold: float = 1.0
    term_extreme_threshold: float = 1.0
    realized_extreme_threshold: float = 1.5
    danger_skip_threshold: float = 2.5
    danger_quarter_size_threshold: float = 1.5
    danger_half_size_threshold: float = 0.75
    delta_neutral_trend_threshold: float = 0.25
    delta_neutral_min_straddle_residual: float = -0.5
    trend_direction_threshold: float = 0.25
    use_four_model_ensemble: bool = True
    atm_surface_min_residual: float = 0.25
    skew_model_extreme_threshold: float = 1.25
    duration_model_term_threshold: float = 1.25
    model_sleeve_fraction: float = 0.50
    confluence_sleeve_fraction: float = 0.50
    require_positive_premium_richness: bool = True
    hard_term_ratio_skip_threshold: float = 1.50
    hard_realized_skip_threshold: float = 1.75
    hard_trend_skip_threshold: float = 2.00
    use_candidate_engine: bool = True
    candidate_min_score: float = 2.50
    candidate_half_score: float = 2.25
    candidate_full_score: float = 2.50
    candidate_max_sides: int = 1
    use_two_tier_engine: bool = False
    exploratory_min_score: float = 2.25
    exploratory_max_score: float = 2.40
    exploratory_size_fraction: float = 0.15
    exploratory_max_sides: int = 1
    exploratory_entry_end: time = time(14, 30)
    exploratory_same_side_cluster_points: float = 25.0
    use_exploratory_bear_call_guard: bool = True
    exploratory_bear_call_guard_end: time = time(12, 0)
    exploratory_bear_call_min_score: float = 2.40
    exploratory_bear_call_min_distance_pct: float = 0.0065
    use_time_of_day_controls: bool = False
    early_entry_cutoff: time = time(9, 45)
    early_entry_min_score: float = 2.75
    late_entry_cutoff: time = time(14, 30)
    late_core_min_score: float = 2.75
    final_hour_cutoff: time = time(15, 0)
    final_hour_min_distance_pct: float = 0.006
    same_side_stop_late_reentry_cutoff: time = time(14, 0)
    use_event_controls: bool = False
    event_bucket: str = "unlabeled"
    event_shock_buckets: str = "tariff_shock,tariff_reversal"
    scheduled_macro_buckets: str = "cpi_event,fomc_event,nfp_event"
    scheduled_macro_early_min_score: float = 2.75
    scheduled_macro_exploratory_min_score: float = 2.35
    block_exploratory_rich_term_days: bool = True
    use_condor_sleeve: bool = False
    condor_size_fraction: float = 0.15
    condor_min_score: float = 2.30
    condor_target_abs_delta: float = 0.12
    condor_min_abs_delta: float = 0.08
    condor_max_abs_delta: float = 0.16
    condor_min_straddle_residual_z: float = 0.50
    condor_max_abs_trend_score: float = 1.00
    condor_max_abs_skew_z: float = 1.25
    condor_max_abs_term_ratio_z: float = 1.25
    condor_max_abs_realized_z: float = 1.50
    condor_entry_start: time = time(9, 45)
    condor_entry_end: time = time(14, 30)
    condor_allowed_event_buckets: str = ""
    condor_block_event_buckets: str = "tariff_shock,tariff_reversal,fomc_event"
    use_one_dte_sleeve: bool = False
    one_dte_size_fraction: float = 0.10
    one_dte_min_score: float = 2.50
    one_dte_target_abs_delta: float = 0.12
    one_dte_min_abs_delta: float = 0.08
    one_dte_max_abs_delta: float = 0.16
    one_dte_min_straddle_residual_z: float = 0.50
    one_dte_max_abs_trend_score: float = 0.75
    one_dte_max_abs_skew_z: float = 1.25
    one_dte_max_abs_term_ratio_z: float = 1.25
    one_dte_max_abs_realized_z: float = 1.50
    one_dte_entry_start: time = time(10, 0)
    one_dte_entry_end: time = time(14, 30)
    one_dte_allowed_event_buckets: str = ""
    one_dte_block_event_buckets: str = "tariff_shock,tariff_reversal,fomc_event"
    use_portfolio_allocator: bool = False
    portfolio_margin_budget_pct: float = 0.40
    core_margin_budget_pct: float = 0.35
    exploratory_margin_budget_pct: float = 0.02
    condor_margin_budget_pct: float = 0.03
    one_dte_margin_budget_pct: float = 0.0
    trend_debit_margin_budget_pct: float = 0.03
    long_put_hedge_margin_budget_pct: float = 0.02
    use_trend_debit_sleeve: bool = False
    trend_debit_size_fraction: float = 0.10
    trend_debit_min_abs_trend_score: float = 1.75
    trend_debit_min_entry_time: time = time(10, 0)
    trend_debit_max_entry_time: time = time(14, 30)
    trend_debit_target_abs_delta: float = 0.40
    trend_debit_min_abs_delta: float = 0.30
    trend_debit_max_abs_delta: float = 0.55
    trend_debit_width: float = 25.0
    trend_debit_max_debit_to_width: float = 0.55
    trend_debit_min_score: float = 2.20
    use_long_put_hedge_sleeve: bool = False
    long_put_hedge_size_fraction: float = 0.08
    long_put_hedge_min_downtrend_score: float = 1.25
    long_put_hedge_min_realized_z: float = 1.25
    long_put_hedge_entry_start: time = time(9, 45)
    long_put_hedge_entry_end: time = time(14, 30)
    long_put_hedge_target_abs_delta: float = 0.30
    long_put_hedge_min_abs_delta: float = 0.20
    long_put_hedge_max_abs_delta: float = 0.45
    long_put_hedge_width: float = 25.0
    long_put_hedge_max_debit_to_width: float = 0.45
    long_put_hedge_min_score: float = 2.00
    candidate_min_credit: float = 0.20
    candidate_min_credit_to_width: float = 0.0125
    candidate_max_stop_loss_to_credit: float = 4.50
    candidate_max_adverse_trend: float = 0.65
    candidate_max_chase_trend: float = 1.50
    candidate_max_adverse_skew: float = 0.75
    candidate_max_abs_term_ratio_z: float = 1.25
    candidate_max_abs_realized_z: float = 1.50
    candidate_distance_weight: float = 12.0
    max_open_trades_per_side: int = 2
    max_open_trades_same_side_strike: int = 1
    stop_cooldown_minutes: int = 30
    same_side_stop_cooldown_minutes: int = 120
    max_stops_per_side: int = 2
    use_intraday_memory_gate: bool = True
    memory_term_ratio_skip_threshold: float = 1.50
    memory_skew_skip_threshold: float = 99.0
    memory_trend_skip_threshold: float = 99.0


@dataclass
class Trade:
    trade_id: int
    entry_time: datetime
    expiry: str
    side: str
    model: str
    contracts: int
    short_type: str
    short_strike: float
    long_type: str
    long_strike: float
    short_entry_sell: float
    long_entry_buy: float
    entry_credit: float
    stop_price: float
    entry_spot: Optional[float] = None
    short_delta: Optional[float] = None
    long_delta: Optional[float] = None
    spread_width: float = 0.0
    credit_to_width: float = 0.0
    distance_pct: float = 0.0
    candidate_score: float = 0.0
    candidate_reason: str = ""
    entry_straddle_residual_z: float = 0.0
    entry_skew_z: float = 0.0
    entry_term_ratio_z: float = 0.0
    entry_trend_score: float = 0.0
    entry_realized_vs_implied_z: float = 0.0
    stop_spot: Optional[float] = None
    stopped: bool = False
    stop_time: Optional[datetime] = None
    stop_fill: Optional[float] = None
    exit_reason: str = "open"
    closed_early: bool = False
    close_fee_legs: int = 0
    gross_pnl: float = 0.0
    fees: float = 0.0
    net_pnl: float = 0.0

    @property
    def open_fee_legs(self) -> int:
        return 2

    @property
    def stop_fee_legs(self) -> int:
        return 1 if self.stopped else 0


@dataclass
class SimulationResult:
    trades: List[Trade]
    account_equity: float
    gross_pnl: float
    fees: float
    net_pnl: float
    gross_credit_sold: float
    halted: bool
    halt_time: Optional[datetime] = None
    messages: List[str] = field(default_factory=list)
    candidate_records: List[CandidateRecord] = field(default_factory=list)

    @property
    def return_on_equity(self) -> float:
        if self.account_equity == 0:
            return 0.0
        return self.net_pnl / self.account_equity


class DefaultSignalPolicy:
    """Placeholder policy for disclosed signal slots.

    This is a harness, not the proprietary edge. It maps candidate signal values
    to 16/8/4/0 contract sizes so we can test alternative policies later.
    """

    def contracts(self, signal: Optional[SignalSnapshot], config: StrategyConfig) -> int:
        if signal is None:
            return config.baseline_contracts

        vix_scale = 1.0
        if signal.vix is not None:
            if signal.vix < 12.0:
                vix_scale = 0.0
            elif signal.vix < 15.0:
                vix_scale = 0.8
            elif signal.vix < 16.0:
                vix_scale = 0.9
            elif signal.vix > 35.0:
                vix_scale = 0.5

        danger = 0.0
        if signal.straddle_residual_z < config.straddle_cheap_threshold:
            danger += abs(signal.straddle_residual_z)
        if abs(signal.skew_z) > config.skew_extreme_threshold:
            danger += abs(signal.skew_z) - 0.5
        if abs(signal.term_ratio_z) > config.term_extreme_threshold:
            danger += abs(signal.term_ratio_z) - 0.5
        if abs(signal.realized_vs_implied_z) > config.realized_extreme_threshold:
            danger += 0.5

        if danger >= config.danger_skip_threshold:
            return 0
        if danger >= config.danger_quarter_size_threshold:
            model_scale = 0.25
        elif danger >= config.danger_half_size_threshold:
            model_scale = 0.5
        else:
            model_scale = 1.0

        contracts = round(config.baseline_contracts * vix_scale * model_scale)
        return max(0, contracts)

    def instructions(self, signal: Optional[SignalSnapshot], config: StrategyConfig) -> List[TradeInstruction]:
        contracts = self.contracts(signal, config)
        if contracts <= 0:
            return []

        if signal is None:
            return [TradeInstruction("bull_put", contracts, "default")]

        if self.skip_entry(signal, config):
            return []

        if config.use_four_model_ensemble:
            return self.ensemble_instructions(signal, config, contracts)

        if (
            abs(signal.trend_score) <= config.delta_neutral_trend_threshold
            and signal.straddle_residual_z >= config.delta_neutral_min_straddle_residual
        ):
            put_contracts = max(1, math.ceil(contracts / 2))
            call_contracts = contracts - put_contracts
            instructions = [TradeInstruction("bull_put", put_contracts, "atm_surface_delta_neutral")]
            if call_contracts > 0:
                instructions.append(TradeInstruction("bear_call", call_contracts, "atm_surface_delta_neutral"))
            return instructions

        if signal.trend_score > config.trend_direction_threshold:
            return [TradeInstruction("bull_put", contracts, "trend_breakout")]
        if signal.trend_score < -config.trend_direction_threshold:
            return [TradeInstruction("bear_call", contracts, "trend_breakout")]

        if signal.skew_z >= 0:
            return [TradeInstruction("bull_put", contracts, "skew_filtered")]
        return [TradeInstruction("bear_call", contracts, "skew_filtered")]

    @staticmethod
    def skip_entry(signal: SignalSnapshot, config: StrategyConfig) -> bool:
        if config.require_positive_premium_richness and signal.straddle_residual_z < config.atm_surface_min_residual:
            return True
        if abs(signal.term_ratio_z) > config.hard_term_ratio_skip_threshold:
            return True
        if abs(signal.realized_vs_implied_z) > config.hard_realized_skip_threshold:
            return True
        if abs(signal.trend_score) > config.hard_trend_skip_threshold:
            return True
        return False

    def ensemble_instructions(self, signal: SignalSnapshot, config: StrategyConfig, contracts: int) -> List[TradeInstruction]:
        sleeve_contracts = max(1, round(contracts * config.model_sleeve_fraction))
        confluence_contracts = max(1, round(contracts * config.confluence_sleeve_fraction))
        instructions: List[TradeInstruction] = []

        atm_active = signal.straddle_residual_z >= config.atm_surface_min_residual
        skew_active = abs(signal.skew_z) <= config.skew_model_extreme_threshold
        trend_active = abs(signal.trend_score) >= config.trend_direction_threshold
        duration_active = abs(signal.term_ratio_z) <= config.duration_model_term_threshold

        if atm_active:
            instructions.extend(self.delta_neutral_pair(sleeve_contracts, "atm_surface"))

        if skew_active:
            if abs(signal.skew_z) < 0.25:
                instructions.extend(self.delta_neutral_pair(sleeve_contracts, "skew"))
            elif signal.skew_z > 0:
                instructions.append(TradeInstruction("bull_put", sleeve_contracts, "skew"))
            else:
                instructions.append(TradeInstruction("bear_call", sleeve_contracts, "skew"))

        if trend_active:
            if signal.trend_score > 0:
                instructions.append(TradeInstruction("bull_put", sleeve_contracts, "trend_breakout"))
            else:
                instructions.append(TradeInstruction("bear_call", sleeve_contracts, "trend_breakout"))

        if duration_active:
            if abs(signal.term_ratio_z) < 0.5:
                instructions.extend(self.delta_neutral_pair(sleeve_contracts, "durational_influence"))
            elif signal.term_ratio_z > 0:
                instructions.extend(self.delta_neutral_pair(max(1, sleeve_contracts // 2), "durational_influence"))

        if atm_active and skew_active and duration_active:
            instructions.extend(self.delta_neutral_pair(confluence_contracts, "model_confluence"))
            if trend_active:
                side = "bull_put" if signal.trend_score > 0 else "bear_call"
                instructions.append(TradeInstruction(side, confluence_contracts, "model_confluence_directional"))

        return self.merge_instructions(instructions)

    @staticmethod
    def delta_neutral_pair(contracts: int, model: str) -> List[TradeInstruction]:
        put_contracts = max(1, math.ceil(contracts / 2))
        call_contracts = contracts - put_contracts
        instructions = [TradeInstruction("bull_put", put_contracts, model)]
        if call_contracts > 0:
            instructions.append(TradeInstruction("bear_call", call_contracts, model))
        return instructions

    @staticmethod
    def merge_instructions(instructions: Sequence[TradeInstruction]) -> List[TradeInstruction]:
        merged: Dict[Tuple[str, str], int] = {}
        for instruction in instructions:
            if instruction.contracts <= 0:
                continue
            key = (instruction.side, instruction.model)
            merged[key] = merged.get(key, 0) + instruction.contracts
        return [TradeInstruction(side, contracts, model) for (side, model), contracts in merged.items()]

    def side(self, signal: Optional[SignalSnapshot]) -> str:
        if signal is None:
            return "bull_put"
        if signal.trend_score > 0.25:
            return "bull_put"
        if signal.trend_score < -0.25:
            return "bear_call"
        if signal.skew_z >= 0:
            return "bull_put"
        return "bear_call"


def read_quotes_csv(path: str | Path) -> List[OptionQuote]:
    quotes: List[OptionQuote] = []
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            quotes.append(
                OptionQuote(
                    timestamp=parse_timestamp(row["timestamp"]),
                    expiry=row["expiry"],
                    option_type=normalize_option_type(row["option_type"]),
                    strike=float(row["strike"]),
                    bid=float(row["bid"]),
                    ask=float(row["ask"]),
                    delta=_float_or_none(row.get("delta")),
                    iv=_float_or_none(row.get("iv")),
                    underlying_price=_float_or_none(row.get("underlying_price")),
                )
            )
    return quotes


def read_signals_csv(path: str | Path) -> List[SignalSnapshot]:
    signals: List[SignalSnapshot] = []
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            signals.append(
                SignalSnapshot(
                    timestamp=parse_timestamp(row["timestamp"]),
                    straddle_residual_z=float(row.get("straddle_residual_z") or 0.0),
                    skew_z=float(row.get("skew_z") or 0.0),
                    term_ratio_z=float(row.get("term_ratio_z") or 0.0),
                    trend_score=float(row.get("trend_score") or 0.0),
                    realized_vs_implied_z=float(row.get("realized_vs_implied_z") or 0.0),
                    vix=_float_or_none(row.get("vix")),
                )
            )
    return signals


def group_quotes_by_time(quotes: Iterable[OptionQuote]) -> Dict[datetime, List[OptionQuote]]:
    grouped: Dict[datetime, List[OptionQuote]] = {}
    for quote in quotes:
        grouped.setdefault(quote.timestamp, []).append(quote)
    return grouped


def signal_by_time(signals: Iterable[SignalSnapshot]) -> Dict[datetime, SignalSnapshot]:
    return {signal.timestamp: signal for signal in signals}


def quote_lookup(snapshot: Sequence[OptionQuote]) -> Dict[Tuple[str, float], OptionQuote]:
    return {(normalize_option_type(quote.option_type), quote.strike): quote for quote in snapshot}


def quote_lookup_by_expiry(snapshot: Sequence[OptionQuote]) -> Dict[Tuple[str, str, float], OptionQuote]:
    return {(quote.expiry, normalize_option_type(quote.option_type), quote.strike): quote for quote in snapshot}


def snapshot_spot(snapshot: Sequence[OptionQuote]) -> float:
    for quote in snapshot:
        if quote.underlying_price is not None:
            return quote.underlying_price
    raise ValueError("Snapshot is missing underlying_price")


def select_spread(
    snapshot: Sequence[OptionQuote],
    side: str,
    config: StrategyConfig,
) -> Optional[Tuple[OptionQuote, OptionQuote]]:
    target_expiry = config.target_expiry or min(quote.expiry for quote in snapshot)
    trading_snapshot = [quote for quote in snapshot if quote.expiry == target_expiry]
    if side == "bull_put":
        option_type = "PUT"
        long_direction = -1
    elif side == "bear_call":
        option_type = "CALL"
        long_direction = 1
    else:
        raise ValueError(f"Unsupported side: {side}")

    candidates = []
    for quote in trading_snapshot:
        if normalize_option_type(quote.option_type) != option_type or quote.delta is None:
            continue
        abs_delta = abs(quote.delta)
        if config.min_abs_delta <= abs_delta <= config.max_abs_delta:
            candidates.append((abs(abs_delta - config.target_abs_delta), quote))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0])
    for _, short_quote in candidates:
        long_quote = select_long_wing(trading_snapshot, short_quote, long_direction, config)
        if long_quote is not None:
            return short_quote, long_quote
    return None


def select_long_wing(
    snapshot: Sequence[OptionQuote],
    short_quote: OptionQuote,
    long_direction: int,
    config: StrategyConfig,
) -> Optional[OptionQuote]:
    same_expiry_type = [
        quote
        for quote in snapshot
        if quote.expiry == short_quote.expiry and normalize_option_type(quote.option_type) == normalize_option_type(short_quote.option_type)
    ]
    wings = []
    for quote in same_expiry_type:
        distance = (quote.strike - short_quote.strike) * long_direction
        if distance < config.min_wing_width or distance > config.max_wing_width:
            continue
        wings.append((distance, quote))

    if not wings:
        return None

    if config.wing_selection_mode == "fixed_width":
        return min(wings, key=lambda item: abs(item[0] - config.wing_width))[1]

    if config.wing_selection_mode == "target_delta":
        with_delta = [(abs(abs(quote.delta or 0.0) - config.target_long_abs_delta), distance, quote) for distance, quote in wings if quote.delta is not None]
        if with_delta:
            with_delta.sort(key=lambda item: (item[0], item[1]))
            return with_delta[0][2]

    return min(wings, key=lambda item: abs(item[0] - config.wing_width))[1]


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _side_option_type(side: str) -> str:
    if side == "bull_put":
        return "PUT"
    if side == "bear_call":
        return "CALL"
    raise ValueError(f"Unsupported side: {side}")


def _side_long_direction(side: str) -> int:
    if side == "bull_put":
        return -1
    if side == "bear_call":
        return 1
    raise ValueError(f"Unsupported side: {side}")


def _distance_pct(side: str, short_strike: float, spot: float) -> float:
    if spot <= 0:
        return 0.0
    if side == "bull_put":
        return max(0.0, (spot - short_strike) / spot)
    if side == "bear_call":
        return max(0.0, (short_strike - spot) / spot)
    raise ValueError(f"Unsupported side: {side}")


def _side_gate_reason(side: str, signal: SignalSnapshot, config: StrategyConfig) -> str:
    if config.require_positive_premium_richness and signal.straddle_residual_z < config.atm_surface_min_residual:
        return "cheap_premium"
    if abs(signal.term_ratio_z) > config.candidate_max_abs_term_ratio_z:
        return "term_structure_dislocation"
    if abs(signal.realized_vs_implied_z) > config.candidate_max_abs_realized_z:
        return "realized_vol_shock"
    if abs(signal.trend_score) > config.hard_trend_skip_threshold:
        return "extreme_trend"
    if side == "bull_put" and signal.trend_score < -config.candidate_max_adverse_trend:
        return "adverse_downtrend"
    if side == "bear_call" and signal.trend_score > config.candidate_max_adverse_trend:
        return "adverse_uptrend"
    if side == "bull_put" and signal.trend_score > config.candidate_max_chase_trend:
        return "bull_put_trend_chase"
    if side == "bear_call" and signal.trend_score < -config.candidate_max_chase_trend:
        return "bear_call_trend_chase"
    if side == "bull_put" and signal.skew_z < -config.candidate_max_adverse_skew:
        return "adverse_put_skew"
    if side == "bear_call" and signal.skew_z > config.candidate_max_adverse_skew:
        return "adverse_call_skew"
    return ""


def _candidate_score(
    side: str,
    signal: SignalSnapshot,
    abs_delta: float,
    credit_to_width: float,
    distance_pct: float,
    stop_loss_to_credit: float,
    config: StrategyConfig,
) -> float:
    premium = _clamp((signal.straddle_residual_z - config.atm_surface_min_residual) / 1.5, -1.0, 1.5)
    term = _clamp(1.0 - abs(signal.term_ratio_z) / max(config.candidate_max_abs_term_ratio_z, 0.01), -1.0, 1.0)
    realized = _clamp(1.0 - abs(signal.realized_vs_implied_z) / max(config.candidate_max_abs_realized_z, 0.01), -1.0, 1.0)
    trend_alignment = signal.trend_score if side == "bull_put" else -signal.trend_score
    skew_alignment = signal.skew_z if side == "bull_put" else -signal.skew_z
    trend = _clamp(trend_alignment / max(config.candidate_max_adverse_trend, 0.01), -1.5, 1.5)
    skew = _clamp(skew_alignment / max(config.candidate_max_adverse_skew, 0.01), -1.5, 1.5)
    delta = _clamp(1.0 - abs(abs_delta - config.target_abs_delta) / max(config.max_abs_delta - config.min_abs_delta, 0.01), -1.0, 1.0)
    credit = _clamp(credit_to_width / 0.04, 0.0, 1.5)
    distance = _clamp(distance_pct * config.candidate_distance_weight, 0.0, 1.5)
    stop = _clamp(1.0 - stop_loss_to_credit / max(config.candidate_max_stop_loss_to_credit, 0.01), -1.0, 1.0)

    return (
        0.45 * premium
        + 0.25 * term
        + 0.20 * realized
        + 0.40 * trend
        + 0.35 * skew
        + 0.20 * delta
        + 0.35 * credit
        + 0.25 * distance
        + 0.25 * stop
    )


def _effective_candidate_min_score(config: StrategyConfig) -> float:
    if config.use_two_tier_engine:
        return min(config.candidate_min_score, config.exploratory_min_score)
    return config.candidate_min_score


def build_scored_candidates(
    snapshot: Sequence[OptionQuote],
    signal: SignalSnapshot,
    config: StrategyConfig,
) -> List[CandidateRecord]:
    target_expiry = config.target_expiry or min(quote.expiry for quote in snapshot)
    trading_snapshot = [quote for quote in snapshot if quote.expiry == target_expiry]
    spot = snapshot_spot(trading_snapshot)
    records: List[CandidateRecord] = []

    for side in ("bull_put", "bear_call"):
        option_type = _side_option_type(side)
        long_direction = _side_long_direction(side)
        gate_reason = _side_gate_reason(side, signal, config)

        for short_quote in trading_snapshot:
            if normalize_option_type(short_quote.option_type) != option_type or short_quote.delta is None:
                continue
            abs_delta = abs(short_quote.delta)
            if not (config.min_abs_delta <= abs_delta <= config.max_abs_delta):
                continue
            long_quote = select_long_wing(trading_snapshot, short_quote, long_direction, config)
            if long_quote is None:
                continue

            width = abs(long_quote.strike - short_quote.strike)
            if width <= 0:
                continue
            credit = short_quote.bid - long_quote.ask
            credit_to_width = credit / width if width else 0.0
            distance_pct = _distance_pct(side, short_quote.strike, spot)
            raw_stop_loss = short_quote.bid * max(config.stop_multiple - 1.0, 0.0) + long_quote.ask
            stop_loss_to_credit = raw_stop_loss / max(credit, 0.01)
            score = _candidate_score(side, signal, abs_delta, credit_to_width, distance_pct, stop_loss_to_credit, config)

            status = "pass"
            reason = "accepted"
            if gate_reason:
                status = "gated"
                reason = gate_reason
            elif credit < config.candidate_min_credit:
                status = "rejected"
                reason = "insufficient_credit"
            elif credit_to_width < config.candidate_min_credit_to_width:
                status = "rejected"
                reason = "thin_credit_to_width"
            elif stop_loss_to_credit > config.candidate_max_stop_loss_to_credit:
                status = "rejected"
                reason = "poor_stop_reward"
            elif score < _effective_candidate_min_score(config):
                status = "rejected"
                reason = "low_score"

            records.append(
                CandidateRecord(
                    timestamp=short_quote.timestamp,
                    side=side,
                    status=status,
                    reason=reason,
                    score=score,
                    expiry=short_quote.expiry,
                    short_type=normalize_option_type(short_quote.option_type),
                    short_strike=short_quote.strike,
                    long_strike=long_quote.strike,
                    short_delta=short_quote.delta,
                    long_delta=long_quote.delta,
                    spot=spot,
                    distance_pct=distance_pct,
                    width=width,
                    credit=credit,
                    credit_to_width=credit_to_width,
                    stop_loss_to_credit=stop_loss_to_credit,
                    straddle_residual_z=signal.straddle_residual_z,
                    skew_z=signal.skew_z,
                    term_ratio_z=signal.term_ratio_z,
                    trend_score=signal.trend_score,
                    realized_vs_implied_z=signal.realized_vs_implied_z,
                    short_quote=short_quote,
                    long_quote=long_quote,
                )
            )

    records.sort(key=lambda record: (record.score, record.credit_to_width, record.distance_pct), reverse=True)
    return records


def _candidate_contracts(base_contracts: int, score: float, config: StrategyConfig) -> int:
    if score >= config.candidate_full_score:
        scale = 1.0
    elif score >= config.candidate_half_score:
        scale = 0.5
    else:
        scale = 0.25
    return max(0, round(base_contracts * scale))


def _exploratory_contracts(base_contracts: int, config: StrategyConfig) -> int:
    return max(1, round(base_contracts * config.exploratory_size_fraction))


def _condor_contracts(base_contracts: int, config: StrategyConfig) -> int:
    return max(1, round(base_contracts * config.condor_size_fraction))


def _one_dte_contracts(base_contracts: int, config: StrategyConfig) -> int:
    return max(1, round(base_contracts * config.one_dte_size_fraction))


def _trend_debit_contracts(base_contracts: int, config: StrategyConfig) -> int:
    return max(1, round(base_contracts * config.trend_debit_size_fraction))


def _long_put_hedge_contracts(base_contracts: int, config: StrategyConfig) -> int:
    return max(1, round(base_contracts * config.long_put_hedge_size_fraction))


def _side_conflicts_with_skew_and_trend(candidate: CandidateRecord) -> bool:
    if candidate.side == "bull_put":
        return candidate.trend_score < 0 and candidate.skew_z < 0
    if candidate.side == "bear_call":
        return candidate.trend_score > 0 and candidate.skew_z > 0
    return False


def _exploratory_selection_block_reason(candidate: CandidateRecord, config: StrategyConfig) -> str:
    if (
        config.use_exploratory_bear_call_guard
        and candidate.side == "bear_call"
        and candidate.timestamp.time() <= config.exploratory_bear_call_guard_end
    ):
        if candidate.score < config.exploratory_bear_call_min_score:
            return "exploratory_morning_bear_call_low_score"
        if candidate.distance_pct < config.exploratory_bear_call_min_distance_pct:
            return "exploratory_morning_bear_call_too_close"
    return ""


def _csv_set(value: str) -> Set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def _entry_control_block_reason(
    candidate: CandidateRecord,
    timestamp: datetime,
    config: StrategyConfig,
    side_stop_counts: Dict[str, int],
) -> str:
    current_time = timestamp.time()
    if config.use_time_of_day_controls:
        if current_time < config.early_entry_cutoff and candidate.score < config.early_entry_min_score:
            return "early_entry_low_score"
        if candidate.sleeve == "exploratory" and current_time > config.exploratory_entry_end:
            return "exploratory_after_entry_window"
        if candidate.sleeve == "core" and current_time > config.late_entry_cutoff and candidate.score < config.late_core_min_score:
            return "late_core_low_score"
        if current_time >= config.final_hour_cutoff:
            if candidate.distance_pct < config.final_hour_min_distance_pct:
                return "final_hour_too_close_to_spot"
        if (
            current_time >= config.same_side_stop_late_reentry_cutoff
            and side_stop_counts.get(candidate.side, 0) > 0
        ):
            return "late_same_side_reentry_after_stop"

    if config.use_event_controls:
        event_bucket = config.event_bucket or "unlabeled"
        shock_buckets = _csv_set(config.event_shock_buckets)
        macro_buckets = _csv_set(config.scheduled_macro_buckets)
        if candidate.sleeve == "exploratory" and event_bucket in shock_buckets:
            return "exploratory_event_shock_block"
        if event_bucket in macro_buckets and current_time < config.early_entry_cutoff and candidate.score < config.scheduled_macro_early_min_score:
            return "macro_early_low_score"
        if (
            candidate.sleeve == "exploratory"
            and event_bucket in macro_buckets
            and candidate.score < config.scheduled_macro_exploratory_min_score
        ):
            return "macro_exploratory_low_score"
        if (
            candidate.sleeve == "exploratory"
            and config.block_exploratory_rich_term_days
            and candidate.straddle_residual_z >= 0.5
            and abs(candidate.term_ratio_z) >= config.candidate_max_abs_term_ratio_z
        ):
            return "exploratory_rich_term_dislocation"

    return ""


def _condor_sleeve_allowed(records: Sequence[CandidateRecord], config: StrategyConfig) -> bool:
    if not config.use_condor_sleeve:
        return False
    pass_records = [record for record in records if record.status == "pass"]
    if not pass_records:
        return False

    sample = pass_records[0]
    current_time = sample.timestamp.time()
    if current_time < config.condor_entry_start or current_time > config.condor_entry_end:
        return False
    allowed_buckets = _csv_set(config.condor_allowed_event_buckets)
    if allowed_buckets and config.event_bucket not in allowed_buckets:
        return False
    if config.event_bucket in _csv_set(config.condor_block_event_buckets):
        return False
    if sample.straddle_residual_z < config.condor_min_straddle_residual_z:
        return False
    if abs(sample.trend_score) > config.condor_max_abs_trend_score:
        return False
    if abs(sample.skew_z) > config.condor_max_abs_skew_z:
        return False
    if abs(sample.term_ratio_z) > config.condor_max_abs_term_ratio_z:
        return False
    if abs(sample.realized_vs_implied_z) > config.condor_max_abs_realized_z:
        return False
    return True


def _condor_signal_allowed(timestamp: datetime, signal: SignalSnapshot, config: StrategyConfig) -> bool:
    if not config.use_condor_sleeve:
        return False
    current_time = timestamp.time()
    if current_time < config.condor_entry_start or current_time > config.condor_entry_end:
        return False
    if config.event_bucket in _csv_set(config.condor_block_event_buckets):
        return False
    if signal.straddle_residual_z < config.condor_min_straddle_residual_z:
        return False
    if abs(signal.trend_score) > config.condor_max_abs_trend_score:
        return False
    if abs(signal.skew_z) > config.condor_max_abs_skew_z:
        return False
    if abs(signal.term_ratio_z) > config.condor_max_abs_term_ratio_z:
        return False
    if abs(signal.realized_vs_implied_z) > config.condor_max_abs_realized_z:
        return False
    return True


def _one_dte_signal_allowed(timestamp: datetime, signal: SignalSnapshot, config: StrategyConfig) -> bool:
    # 1DTE data is retained only as a 0DTE term-structure signal, not as an executable sleeve.
    return False
    if not config.use_one_dte_sleeve:
        return False
    current_time = timestamp.time()
    if current_time < config.one_dte_entry_start or current_time > config.one_dte_entry_end:
        return False
    allowed_buckets = _csv_set(config.one_dte_allowed_event_buckets)
    if allowed_buckets and config.event_bucket not in allowed_buckets:
        return False
    if config.event_bucket in _csv_set(config.one_dte_block_event_buckets):
        return False
    if signal.straddle_residual_z < config.one_dte_min_straddle_residual_z:
        return False
    if abs(signal.trend_score) > config.one_dte_max_abs_trend_score:
        return False
    if abs(signal.skew_z) > config.one_dte_max_abs_skew_z:
        return False
    if abs(signal.term_ratio_z) > config.one_dte_max_abs_term_ratio_z:
        return False
    if abs(signal.realized_vs_implied_z) > config.one_dte_max_abs_realized_z:
        return False
    return True


def _condor_neutral_score(
    signal: SignalSnapshot,
    credit_to_width: float,
    distance_pct: float,
    stop_loss_to_credit: float,
    config: StrategyConfig,
) -> float:
    premium = _clamp((signal.straddle_residual_z - config.condor_min_straddle_residual_z) / 1.5, -1.0, 1.5)
    trend = _clamp(1.0 - abs(signal.trend_score) / max(config.condor_max_abs_trend_score, 0.01), -1.0, 1.0)
    skew = _clamp(1.0 - abs(signal.skew_z) / max(config.condor_max_abs_skew_z, 0.01), -1.0, 1.0)
    term = _clamp(1.0 - abs(signal.term_ratio_z) / max(config.condor_max_abs_term_ratio_z, 0.01), -1.0, 1.0)
    realized = _clamp(1.0 - abs(signal.realized_vs_implied_z) / max(config.condor_max_abs_realized_z, 0.01), -1.0, 1.0)
    credit = _clamp(credit_to_width / 0.04, 0.0, 1.5)
    distance = _clamp(distance_pct * config.candidate_distance_weight, 0.0, 1.5)
    stop = _clamp(1.0 - stop_loss_to_credit / max(config.candidate_max_stop_loss_to_credit, 0.01), -1.0, 1.0)
    return (
        1.50
        + 0.45 * premium
        + 0.30 * trend
        + 0.25 * skew
        + 0.25 * term
        + 0.15 * realized
        + 0.25 * credit
        + 0.15 * distance
        + 0.15 * stop
    )


def _select_condor_leg(
    snapshot: Sequence[OptionQuote],
    signal: SignalSnapshot,
    side: str,
    config: StrategyConfig,
) -> Optional[CandidateRecord]:
    target_expiry = config.target_expiry or min(quote.expiry for quote in snapshot)
    trading_snapshot = [quote for quote in snapshot if quote.expiry == target_expiry]
    spot = snapshot_spot(trading_snapshot)
    option_type = _side_option_type(side)
    long_direction = _side_long_direction(side)
    candidates: List[Tuple[float, float, float, CandidateRecord]] = []

    for short_quote in trading_snapshot:
        if normalize_option_type(short_quote.option_type) != option_type or short_quote.delta is None:
            continue
        abs_delta = abs(short_quote.delta)
        if not (config.condor_min_abs_delta <= abs_delta <= config.condor_max_abs_delta):
            continue
        long_quote = select_long_wing(trading_snapshot, short_quote, long_direction, config)
        if long_quote is None:
            continue
        width = abs(long_quote.strike - short_quote.strike)
        if width <= 0:
            continue
        credit = short_quote.bid - long_quote.ask
        credit_to_width = credit / width if width else 0.0
        distance_pct = _distance_pct(side, short_quote.strike, spot)
        raw_stop_loss = short_quote.bid * max(config.stop_multiple - 1.0, 0.0) + long_quote.ask
        stop_loss_to_credit = raw_stop_loss / max(credit, 0.01)
        if credit < config.candidate_min_credit:
            continue
        if credit_to_width < config.candidate_min_credit_to_width:
            continue
        if stop_loss_to_credit > config.candidate_max_stop_loss_to_credit:
            continue
        score = _condor_neutral_score(signal, credit_to_width, distance_pct, stop_loss_to_credit, config)
        if score < config.condor_min_score:
            continue

        record = CandidateRecord(
            timestamp=short_quote.timestamp,
            side=side,
            status="selected",
            reason="condor_selected",
            score=score,
            expiry=short_quote.expiry,
            short_type=normalize_option_type(short_quote.option_type),
            short_strike=short_quote.strike,
            long_strike=long_quote.strike,
            short_delta=short_quote.delta,
            long_delta=long_quote.delta,
            spot=spot,
            distance_pct=distance_pct,
            width=width,
            credit=credit,
            credit_to_width=credit_to_width,
            stop_loss_to_credit=stop_loss_to_credit,
            straddle_residual_z=signal.straddle_residual_z,
            skew_z=signal.skew_z,
            term_ratio_z=signal.term_ratio_z,
            trend_score=signal.trend_score,
            realized_vs_implied_z=signal.realized_vs_implied_z,
            short_quote=short_quote,
            long_quote=long_quote,
            sleeve="condor",
        )
        candidates.append((abs(abs_delta - config.condor_target_abs_delta), -credit_to_width, -distance_pct, record))

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[:3])
    return candidates[0][3]


def _next_expiry(snapshot: Sequence[OptionQuote]) -> Optional[str]:
    expiries = sorted({quote.expiry for quote in snapshot})
    if len(expiries) < 2:
        return None
    return expiries[1]


def _select_one_dte_leg(
    snapshot: Sequence[OptionQuote],
    signal: SignalSnapshot,
    side: str,
    config: StrategyConfig,
) -> Optional[CandidateRecord]:
    target_expiry = _next_expiry(snapshot)
    if target_expiry is None:
        return None
    trading_snapshot = [quote for quote in snapshot if quote.expiry == target_expiry]
    if not trading_snapshot:
        return None
    spot = snapshot_spot(trading_snapshot)
    option_type = _side_option_type(side)
    long_direction = _side_long_direction(side)
    candidates: List[Tuple[float, float, float, CandidateRecord]] = []

    for short_quote in trading_snapshot:
        if normalize_option_type(short_quote.option_type) != option_type or short_quote.delta is None:
            continue
        abs_delta = abs(short_quote.delta)
        if not (config.one_dte_min_abs_delta <= abs_delta <= config.one_dte_max_abs_delta):
            continue
        long_quote = select_long_wing(trading_snapshot, short_quote, long_direction, config)
        if long_quote is None:
            continue
        width = abs(long_quote.strike - short_quote.strike)
        if width <= 0:
            continue
        credit = short_quote.bid - long_quote.ask
        credit_to_width = credit / width if width else 0.0
        distance_pct = _distance_pct(side, short_quote.strike, spot)
        raw_stop_loss = short_quote.bid * max(config.stop_multiple - 1.0, 0.0) + long_quote.ask
        stop_loss_to_credit = raw_stop_loss / max(credit, 0.01)
        if credit < config.candidate_min_credit:
            continue
        if credit_to_width < config.candidate_min_credit_to_width:
            continue
        if stop_loss_to_credit > config.candidate_max_stop_loss_to_credit:
            continue
        score = _condor_neutral_score(signal, credit_to_width, distance_pct, stop_loss_to_credit, config)
        if score < config.one_dte_min_score:
            continue

        record = CandidateRecord(
            timestamp=short_quote.timestamp,
            side=side,
            status="selected",
            reason="one_dte_selected",
            score=score,
            expiry=short_quote.expiry,
            short_type=normalize_option_type(short_quote.option_type),
            short_strike=short_quote.strike,
            long_strike=long_quote.strike,
            short_delta=short_quote.delta,
            long_delta=long_quote.delta,
            spot=spot,
            distance_pct=distance_pct,
            width=width,
            credit=credit,
            credit_to_width=credit_to_width,
            stop_loss_to_credit=stop_loss_to_credit,
            straddle_residual_z=signal.straddle_residual_z,
            skew_z=signal.skew_z,
            term_ratio_z=signal.term_ratio_z,
            trend_score=signal.trend_score,
            realized_vs_implied_z=signal.realized_vs_implied_z,
            short_quote=short_quote,
            long_quote=long_quote,
            sleeve="one_dte",
        )
        candidates.append((abs(abs_delta - config.one_dte_target_abs_delta), -credit_to_width, -distance_pct, record))

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[:3])
    return candidates[0][3]


def select_condor_entries(
    snapshot: Sequence[OptionQuote],
    signal: SignalSnapshot,
    base_contracts: int,
    config: StrategyConfig,
) -> Tuple[List[CandidateRecord], List[CandidateRecord]]:
    if not _condor_signal_allowed(snapshot[0].timestamp, signal, config):
        return [], []
    legs = [
        _select_condor_leg(snapshot, signal, "bull_put", config),
        _select_condor_leg(snapshot, signal, "bear_call", config),
    ]
    if any(leg is None for leg in legs):
        return [], []
    contracts = _condor_contracts(base_contracts, config)
    if contracts <= 0:
        return [], []
    selected = [leg for leg in legs if leg is not None]
    for leg in selected:
        leg.contracts = contracts
    return selected, selected


def select_one_dte_entries(
    snapshot: Sequence[OptionQuote],
    signal: SignalSnapshot,
    base_contracts: int,
    config: StrategyConfig,
) -> Tuple[List[CandidateRecord], List[CandidateRecord]]:
    if not _one_dte_signal_allowed(snapshot[0].timestamp, signal, config):
        return [], []
    legs = [
        _select_one_dte_leg(snapshot, signal, "bull_put", config),
        _select_one_dte_leg(snapshot, signal, "bear_call", config),
    ]
    if any(leg is None for leg in legs):
        return [], []
    contracts = _one_dte_contracts(base_contracts, config)
    if contracts <= 0:
        return [], []
    selected = [leg for leg in legs if leg is not None]
    for leg in selected:
        leg.contracts = contracts
    return selected, selected


def _debit_spread_record(
    snapshot: Sequence[OptionQuote],
    signal: SignalSnapshot,
    side: str,
    option_type: str,
    buy_direction: int,
    target_abs_delta: float,
    min_abs_delta: float,
    max_abs_delta: float,
    target_width: float,
    max_debit_to_width: float,
    min_score: float,
    sleeve: str,
) -> Optional[CandidateRecord]:
    target_expiry = min(quote.expiry for quote in snapshot)
    trading_snapshot = [quote for quote in snapshot if quote.expiry == target_expiry]
    if not trading_snapshot:
        return None
    spot = snapshot_spot(trading_snapshot)
    same_type = [
        quote
        for quote in trading_snapshot
        if normalize_option_type(quote.option_type) == option_type and quote.delta is not None
    ]
    lookup = {(quote.option_type, quote.strike): quote for quote in same_type}
    candidates: List[Tuple[float, float, CandidateRecord]] = []

    for long_quote in same_type:
        abs_delta = abs(long_quote.delta or 0.0)
        if not (min_abs_delta <= abs_delta <= max_abs_delta):
            continue
        short_strike = long_quote.strike + buy_direction * target_width
        short_quote = lookup.get((long_quote.option_type, short_strike))
        if short_quote is None:
            continue
        width = abs(short_quote.strike - long_quote.strike)
        if width <= 0:
            continue
        debit = long_quote.ask - short_quote.bid
        if debit <= 0:
            continue
        debit_to_width = debit / width
        if debit_to_width > max_debit_to_width:
            continue
        distance_pct = abs(long_quote.strike - spot) / spot if spot > 0 else 0.0
        trend_alignment = abs(signal.trend_score)
        cheapness = max(0.0, max_debit_to_width - debit_to_width)
        convexity = max(0.0, (width - debit) / width)
        score = trend_alignment + cheapness + 0.25 * convexity
        if score < min_score:
            continue
        record = CandidateRecord(
            timestamp=long_quote.timestamp,
            side=side,
            status="selected",
            reason=f"{sleeve}_selected",
            score=score,
            expiry=long_quote.expiry,
            short_type=normalize_option_type(short_quote.option_type),
            short_strike=short_quote.strike,
            long_strike=long_quote.strike,
            short_delta=short_quote.delta,
            long_delta=long_quote.delta,
            spot=spot,
            distance_pct=distance_pct,
            width=width,
            credit=-debit,
            credit_to_width=-debit_to_width,
            stop_loss_to_credit=0.0,
            straddle_residual_z=signal.straddle_residual_z,
            skew_z=signal.skew_z,
            term_ratio_z=signal.term_ratio_z,
            trend_score=signal.trend_score,
            realized_vs_implied_z=signal.realized_vs_implied_z,
            short_quote=short_quote,
            long_quote=long_quote,
            sleeve=sleeve,
        )
        candidates.append((abs(abs_delta - target_abs_delta), debit_to_width, record))

    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


def select_trend_debit_entries(
    snapshot: Sequence[OptionQuote],
    signal: SignalSnapshot,
    base_contracts: int,
    config: StrategyConfig,
) -> Tuple[List[CandidateRecord], List[CandidateRecord]]:
    if not config.use_trend_debit_sleeve:
        return [], []
    current_time = snapshot[0].timestamp.time()
    if current_time < config.trend_debit_min_entry_time or current_time > config.trend_debit_max_entry_time:
        return [], []
    if abs(signal.trend_score) < config.trend_debit_min_abs_trend_score:
        return [], []

    if signal.trend_score > 0:
        record = _debit_spread_record(
            snapshot,
            signal,
            "bull_call_debit",
            "CALL",
            1,
            config.trend_debit_target_abs_delta,
            config.trend_debit_min_abs_delta,
            config.trend_debit_max_abs_delta,
            config.trend_debit_width,
            config.trend_debit_max_debit_to_width,
            config.trend_debit_min_score,
            "trend_debit",
        )
    else:
        record = _debit_spread_record(
            snapshot,
            signal,
            "bear_put_debit",
            "PUT",
            -1,
            config.trend_debit_target_abs_delta,
            config.trend_debit_min_abs_delta,
            config.trend_debit_max_abs_delta,
            config.trend_debit_width,
            config.trend_debit_max_debit_to_width,
            config.trend_debit_min_score,
            "trend_debit",
        )
    if record is None:
        return [], []
    record.contracts = _trend_debit_contracts(base_contracts, config)
    return [record], [record]


def select_long_put_hedge_entries(
    snapshot: Sequence[OptionQuote],
    signal: SignalSnapshot,
    base_contracts: int,
    config: StrategyConfig,
) -> Tuple[List[CandidateRecord], List[CandidateRecord]]:
    if not config.use_long_put_hedge_sleeve:
        return [], []
    current_time = snapshot[0].timestamp.time()
    if current_time < config.long_put_hedge_entry_start or current_time > config.long_put_hedge_entry_end:
        return [], []
    failure_mode = (
        signal.trend_score <= -config.long_put_hedge_min_downtrend_score
        or signal.realized_vs_implied_z >= config.long_put_hedge_min_realized_z
        or config.event_bucket in _csv_set(config.event_shock_buckets)
    )
    if not failure_mode:
        return [], []
    record = _debit_spread_record(
        snapshot,
        signal,
        "long_put_hedge",
        "PUT",
        -1,
        config.long_put_hedge_target_abs_delta,
        config.long_put_hedge_min_abs_delta,
        config.long_put_hedge_max_abs_delta,
        config.long_put_hedge_width,
        config.long_put_hedge_max_debit_to_width,
        config.long_put_hedge_min_score,
        "long_put_hedge",
    )
    if record is None:
        return [], []
    record.contracts = _long_put_hedge_contracts(base_contracts, config)
    return [record], [record]


def _add_condor_candidates(
    selected: List[CandidateRecord],
    records: List[CandidateRecord],
    base_contracts: int,
    config: StrategyConfig,
) -> None:
    if not _condor_sleeve_allowed(records, config):
        return

    already_selected = set(id(record) for record in selected)
    condor_legs: List[CandidateRecord] = []
    for side in ("bull_put", "bear_call"):
        leg = next(
            (
                record
                for record in records
                if record.status == "pass"
                and record.side == side
                and record.score >= config.condor_min_score
                and id(record) not in already_selected
            ),
            None,
        )
        if leg is None:
            return
        condor_legs.append(leg)

    contracts = _condor_contracts(base_contracts, config)
    if contracts <= 0:
        return
    for leg in condor_legs:
        leg.status = "selected"
        leg.reason = "condor_selected"
        leg.sleeve = "condor"
        leg.contracts = contracts
        selected.append(leg)


def select_candidate_entries(
    snapshot: Sequence[OptionQuote],
    signal: SignalSnapshot,
    base_contracts: int,
    config: StrategyConfig,
) -> Tuple[List[CandidateRecord], List[CandidateRecord]]:
    records = build_scored_candidates(snapshot, signal, config)
    if config.use_two_tier_engine:
        return select_two_tier_candidate_entries(records, base_contracts, config)

    selected: List[CandidateRecord] = []
    used_sides = set()
    for record in records:
        if record.status != "pass":
            continue
        if record.side in used_sides:
            continue
        contracts = _candidate_contracts(base_contracts, record.score, config)
        if contracts <= 0:
            continue
        record.status = "selected"
        record.reason = "selected"
        record.sleeve = "core"
        record.contracts = contracts
        selected.append(record)
        used_sides.add(record.side)
        if len(selected) >= config.candidate_max_sides:
            break
    return selected, records


def select_two_tier_candidate_entries(
    records: List[CandidateRecord],
    base_contracts: int,
    config: StrategyConfig,
) -> Tuple[List[CandidateRecord], List[CandidateRecord]]:
    selected: List[CandidateRecord] = []
    used_core_sides = set()
    used_exploratory_sides = set()

    for record in records:
        if record.status != "pass" or record.score < config.candidate_min_score:
            continue
        if record.side in used_core_sides:
            continue
        contracts = _candidate_contracts(base_contracts, record.score, config)
        if contracts <= 0:
            continue
        record.status = "selected"
        record.reason = "core_selected"
        record.sleeve = "core"
        record.contracts = contracts
        selected.append(record)
        used_core_sides.add(record.side)
        if len(used_core_sides) >= config.candidate_max_sides:
            break

    for record in records:
        if record.status != "pass":
            continue
        if record.score < config.exploratory_min_score or record.score > config.exploratory_max_score:
            continue
        if record.side in used_exploratory_sides:
            continue
        if record in selected:
            continue
        block_reason = _exploratory_selection_block_reason(record, config)
        if block_reason:
            record.status = "blocked"
            record.reason = block_reason
            continue
        contracts = _exploratory_contracts(base_contracts, config)
        if contracts <= 0:
            continue
        record.status = "selected"
        record.reason = "exploratory_selected"
        record.sleeve = "exploratory"
        record.contracts = contracts
        selected.append(record)
        used_exploratory_sides.add(record.side)
        if len(used_exploratory_sides) >= config.exploratory_max_sides:
            break

    return selected, records


def open_trade(
    trade_id: int,
    timestamp: datetime,
    side: str,
    model: str,
    contracts: int,
    short_quote: OptionQuote,
    long_quote: OptionQuote,
    config: StrategyConfig,
    candidate: Optional[CandidateRecord] = None,
) -> Optional[Trade]:
    short_sell = short_quote.bid
    long_buy = long_quote.ask
    credit = short_sell - long_buy
    is_debit_candidate = candidate is not None and candidate.sleeve in {"trend_debit", "long_put_hedge"}
    if credit <= 0 and not is_debit_candidate:
        return None

    spot = candidate.spot if candidate else short_quote.underlying_price
    width = abs(long_quote.strike - short_quote.strike)
    credit_to_width = credit / width if width else 0.0
    distance_pct = candidate.distance_pct if candidate else (_distance_pct(side, short_quote.strike, spot or 0.0) if spot else 0.0)

    return Trade(
        trade_id=trade_id,
        entry_time=timestamp,
        expiry=short_quote.expiry,
        side=side,
        model=model,
        contracts=contracts,
        short_type=normalize_option_type(short_quote.option_type),
        short_strike=short_quote.strike,
        long_type=normalize_option_type(long_quote.option_type),
        long_strike=long_quote.strike,
        short_entry_sell=short_sell,
        long_entry_buy=long_buy,
        entry_credit=credit,
        stop_price=short_sell * config.stop_multiple,
        entry_spot=spot,
        short_delta=short_quote.delta,
        long_delta=long_quote.delta,
        spread_width=width,
        credit_to_width=credit_to_width,
        distance_pct=distance_pct,
        candidate_score=candidate.score if candidate else 0.0,
        candidate_reason=candidate.reason if candidate else "",
        entry_straddle_residual_z=candidate.straddle_residual_z if candidate else 0.0,
        entry_skew_z=candidate.skew_z if candidate else 0.0,
        entry_term_ratio_z=candidate.term_ratio_z if candidate else 0.0,
        entry_trend_score=candidate.trend_score if candidate else 0.0,
        entry_realized_vs_implied_z=candidate.realized_vs_implied_z if candidate else 0.0,
    )


def close_trade_at_snapshot(
    trade: Trade,
    timestamp: datetime,
    snapshot: Sequence[OptionQuote],
    config: StrategyConfig,
    reason: str = "daily_loss_flatten",
) -> None:
    """Force-close an open trade at the current snapshot.

    Buys back the short leg at its ask and sells the long leg at its bid (or
    intrinsic if no quote). Used by the daily-loss flatten governor so a bad day
    is capped near the loss limit instead of running open positions to close.
    """
    if trade.exit_reason != "open" or trade.closed_early:
        return
    lookup = quote_lookup_by_expiry(snapshot)
    spot = snapshot_spot(snapshot)
    long_quote = lookup.get((trade.expiry, trade.long_type, trade.long_strike))
    long_exit = long_quote.bid if long_quote else _intrinsic(trade.long_type, trade.long_strike, spot)

    if trade.stopped:
        stop_fill = trade.stop_fill if trade.stop_fill is not None else trade.stop_price
        per_contract = trade.short_entry_sell - stop_fill - trade.long_entry_buy + long_exit
        trade.close_fee_legs = 1  # short already closed at stop; close long leg only
    else:
        short_quote = lookup.get((trade.expiry, trade.short_type, trade.short_strike))
        short_exit = short_quote.ask if short_quote else _intrinsic(trade.short_type, trade.short_strike, spot)
        per_contract = trade.short_entry_sell - short_exit - trade.long_entry_buy + long_exit
        trade.close_fee_legs = 2  # close both legs

    trade.gross_pnl = per_contract * trade.contracts * config.multiplier
    fee_legs = trade.open_fee_legs + trade.stop_fee_legs + trade.close_fee_legs
    trade.fees = fee_legs * trade.contracts * config.fee_per_contract
    trade.net_pnl = trade.gross_pnl - trade.fees
    trade.closed_early = True
    trade.exit_reason = reason


def mark_trade(trade: Trade, snapshot: Sequence[OptionQuote], config: StrategyConfig) -> float:
    if trade.closed_early:
        return trade.gross_pnl
    lookup = quote_lookup_by_expiry(snapshot)
    spot = snapshot_spot(snapshot)
    long_quote = lookup.get((trade.expiry, trade.long_type, trade.long_strike))
    long_mark = long_quote.bid if long_quote else _intrinsic(trade.long_type, trade.long_strike, spot)

    if trade.stopped:
        stop_fill = trade.stop_fill or 0.0
        per_contract = trade.short_entry_sell - stop_fill - trade.long_entry_buy + long_mark
    else:
        short_quote = lookup.get((trade.expiry, trade.short_type, trade.short_strike))
        short_mark = short_quote.ask if short_quote else _intrinsic(trade.short_type, trade.short_strike, spot)
        per_contract = trade.short_entry_sell - short_mark - trade.long_entry_buy + long_mark
    return per_contract * trade.contracts * config.multiplier


def _intrinsic(option_type: str, strike: float, spot: float) -> float:
    option_type = normalize_option_type(option_type)
    if option_type == "CALL":
        return max(spot - strike, 0.0)
    if option_type == "PUT":
        return max(strike - spot, 0.0)
    raise ValueError(f"Unsupported option type: {option_type}")


def process_stops(
    trades: Sequence[Trade],
    timestamp: datetime,
    snapshot: Sequence[OptionQuote],
) -> List[Trade]:
    lookup = quote_lookup_by_expiry(snapshot)
    spot = snapshot_spot(snapshot)
    newly_stopped: List[Trade] = []
    for trade in trades:
        if trade.stopped or trade.exit_reason != "open":
            continue
        if trade.entry_credit < 0:
            continue
        short_quote = lookup.get((trade.expiry, trade.short_type, trade.short_strike))
        if short_quote is None:
            continue
        if short_quote.ask >= trade.stop_price:
            trade.stopped = True
            trade.stop_time = timestamp
            trade.stop_fill = short_quote.ask
            trade.stop_spot = spot
            newly_stopped.append(trade)
    return newly_stopped


def is_entry_time(timestamp: datetime, config: StrategyConfig) -> bool:
    if not (config.entry_start <= timestamp.time() <= config.entry_end):
        return False
    start_minutes = config.entry_start.hour * 60 + config.entry_start.minute
    current_minutes = timestamp.time().hour * 60 + timestamp.time().minute
    return (current_minutes - start_minutes) % config.entry_interval_minutes == 0


def settle_trade(
    trade: Trade,
    close_spot: float,
    close_snapshot: Sequence[OptionQuote],
    config: StrategyConfig,
) -> None:
    if trade.closed_early:
        return
    is_expiring_today = trade.expiry == trade.entry_time.date().isoformat()
    quote_lookup = quote_lookup_by_expiry(close_snapshot)
    short_close = quote_lookup.get((trade.expiry, trade.short_type, trade.short_strike))
    long_close = quote_lookup.get((trade.expiry, trade.long_type, trade.long_strike))
    short_exit = _intrinsic(trade.short_type, trade.short_strike, close_spot)
    long_exit = _intrinsic(trade.long_type, trade.long_strike, close_spot)
    if not is_expiring_today:
        if short_close is not None:
            short_exit = short_close.ask
        if long_close is not None:
            long_exit = long_close.bid

    if trade.stopped:
        stop_fill = trade.stop_fill if trade.stop_fill is not None else trade.stop_price
        per_contract = trade.short_entry_sell - stop_fill - trade.long_entry_buy + long_exit
        trade.exit_reason = "short_stopped_long_settled" if is_expiring_today else "short_stopped_long_marked"
    else:
        per_contract = trade.short_entry_sell - short_exit - trade.long_entry_buy + long_exit
        trade.exit_reason = "settled_at_close" if is_expiring_today else "marked_at_close"

    trade.gross_pnl = per_contract * trade.contracts * config.multiplier
    fee_legs = trade.open_fee_legs + trade.stop_fee_legs
    trade.fees = fee_legs * trade.contracts * config.fee_per_contract
    trade.net_pnl = trade.gross_pnl - trade.fees


def entry_risk_block_reason(
    candidate: CandidateRecord,
    trades: Sequence[Trade],
    timestamp: datetime,
    config: StrategyConfig,
    global_stop_cooldown_until: Optional[datetime],
    side_stop_cooldown_until: Dict[str, datetime],
    side_stop_counts: Dict[str, int],
    intraday_memory_reasons: Set[str],
) -> str:
    if config.use_intraday_memory_gate and intraday_memory_reasons:
        return f"intraday_memory_{sorted(intraday_memory_reasons)[0]}"
    if global_stop_cooldown_until is not None and timestamp < global_stop_cooldown_until:
        return "global_stop_cooldown"
    side_cooldown = side_stop_cooldown_until.get(candidate.side)
    if side_cooldown is not None and timestamp < side_cooldown:
        return "side_stop_cooldown"
    if side_stop_counts.get(candidate.side, 0) >= config.max_stops_per_side:
        return "side_stop_limit"
    entry_control_reason = _entry_control_block_reason(candidate, timestamp, config, side_stop_counts)
    if entry_control_reason:
        return entry_control_reason

    open_same_side = [
        trade
        for trade in trades
        if trade.exit_reason == "open" and not trade.stopped and trade.side == candidate.side
    ]
    if len(open_same_side) >= config.max_open_trades_per_side:
        return "side_concentration_limit"
    same_strike = [
        trade
        for trade in open_same_side
        if trade.short_type == candidate.short_type and trade.short_strike == candidate.short_strike
    ]
    if len(same_strike) >= config.max_open_trades_same_side_strike:
        return "same_strike_concentration_limit"
    if candidate.sleeve == "exploratory":
        if timestamp.time() > config.exploratory_entry_end and candidate.score < config.candidate_min_score:
            return "exploratory_late_low_score"
        same_side_cluster = [
            trade
            for trade in open_same_side
            if trade.short_type == candidate.short_type
            and abs(trade.short_strike - candidate.short_strike) <= config.exploratory_same_side_cluster_points
        ]
        if same_side_cluster:
            return "exploratory_same_side_strike_cluster"
        if _side_conflicts_with_skew_and_trend(candidate):
            return "exploratory_skew_trend_conflict"
    return ""


def update_intraday_memory(signal: Optional[SignalSnapshot], config: StrategyConfig, memory_reasons: Set[str]) -> None:
    if signal is None or not config.use_intraday_memory_gate:
        return
    if signal.term_ratio_z <= -config.memory_term_ratio_skip_threshold:
        memory_reasons.add("negative_term_dislocation")
    if abs(signal.skew_z) >= config.memory_skew_skip_threshold:
        memory_reasons.add("skew_shock")
    if abs(signal.trend_score) >= config.memory_trend_skip_threshold:
        memory_reasons.add("trend_shock")


def candidate_margin_per_contract(candidate: CandidateRecord, config: StrategyConfig) -> float:
    if candidate.credit < 0:
        return abs(candidate.credit) * config.multiplier
    return max(candidate.width - candidate.credit, 0.0) * config.multiplier


def trade_margin(trade: Trade, config: StrategyConfig) -> float:
    if trade.entry_credit < 0:
        return abs(trade.entry_credit) * trade.contracts * config.multiplier
    return max(trade.spread_width - trade.entry_credit, 0.0) * trade.contracts * config.multiplier


def sleeve_margin_budget_pct(sleeve: str, config: StrategyConfig) -> float:
    if sleeve == "exploratory":
        return config.exploratory_margin_budget_pct
    if sleeve == "condor":
        return config.condor_margin_budget_pct
    if sleeve == "one_dte":
        return config.one_dte_margin_budget_pct
    if sleeve == "trend_debit":
        return config.trend_debit_margin_budget_pct
    if sleeve == "long_put_hedge":
        return config.long_put_hedge_margin_budget_pct
    return config.core_margin_budget_pct


def allocator_contract_limit(
    candidate: CandidateRecord,
    sleeve_margin_used: Dict[str, float],
    portfolio_margin_used: float,
    config: StrategyConfig,
) -> int:
    if not config.use_portfolio_allocator:
        return candidate.contracts

    margin_per_contract = candidate_margin_per_contract(candidate, config)
    if margin_per_contract <= 0:
        return candidate.contracts

    sleeve = candidate.sleeve or "core"
    sleeve_budget = config.account_equity * sleeve_margin_budget_pct(sleeve, config)
    portfolio_budget = config.account_equity * config.portfolio_margin_budget_pct
    sleeve_remaining = max(sleeve_budget - sleeve_margin_used.get(sleeve, 0.0), 0.0)
    portfolio_remaining = max(portfolio_budget - portfolio_margin_used, 0.0)
    max_by_sleeve = math.floor(sleeve_remaining / margin_per_contract)
    max_by_portfolio = math.floor(portfolio_remaining / margin_per_contract)
    return min(candidate.contracts, max_by_sleeve, max_by_portfolio)


def simulate_day(
    quotes: Sequence[OptionQuote],
    signals: Sequence[SignalSnapshot],
    config: StrategyConfig | None = None,
    policy: DefaultSignalPolicy | None = None,
    close_spot: Optional[float] = None,
) -> SimulationResult:
    config = config or StrategyConfig()
    policy = policy or DefaultSignalPolicy()

    grouped = group_quotes_by_time(quotes)
    signals_by_ts = signal_by_time(signals)
    timestamps = sorted(grouped)
    trades: List[Trade] = []
    messages: List[str] = []
    candidate_records: List[CandidateRecord] = []
    next_trade_id = 1
    halted = False
    flattened = False
    halt_time: Optional[datetime] = None
    gross_credit_sold = 0.0
    daily_credit_cap = config.account_equity * config.daily_credit_cap_pct
    daily_loss_limit = -config.account_equity * config.daily_loss_limit_pct
    flatten_loss_limit = (
        -config.account_equity * config.flatten_loss_limit_pct
        if config.flatten_loss_limit_pct > 0
        else daily_loss_limit
    )
    sleeve_margin_used: Dict[str, float] = {
        "core": 0.0,
        "exploratory": 0.0,
        "condor": 0.0,
        "one_dte": 0.0,
        "trend_debit": 0.0,
        "long_put_hedge": 0.0,
    }
    portfolio_margin_used = 0.0
    global_stop_cooldown_until: Optional[datetime] = None
    side_stop_cooldown_until: Dict[str, datetime] = {}
    side_stop_counts: Dict[str, int] = {}
    intraday_memory_reasons: Set[str] = set()

    for timestamp in timestamps:
        snapshot = grouped[timestamp]
        newly_stopped = process_stops(trades, timestamp, snapshot)
        for stopped_trade in newly_stopped:
            side_stop_counts[stopped_trade.side] = side_stop_counts.get(stopped_trade.side, 0) + 1
            if config.stop_cooldown_minutes > 0:
                global_stop_cooldown_until = timestamp + timedelta(minutes=config.stop_cooldown_minutes)
            if config.same_side_stop_cooldown_minutes > 0:
                side_stop_cooldown_until[stopped_trade.side] = timestamp + timedelta(minutes=config.same_side_stop_cooldown_minutes)

        marked_pnl = sum(mark_trade(trade, snapshot, config) for trade in trades)
        if not halted and marked_pnl <= daily_loss_limit:
            halted = True
            halt_time = timestamp
            messages.append(f"Daily loss halt at {timestamp.isoformat()} with marked PnL {marked_pnl:.2f}")
        if config.flatten_on_daily_loss and not flattened and marked_pnl <= flatten_loss_limit:
            flattened = True
            for trade in trades:
                close_trade_at_snapshot(trade, timestamp, snapshot, config, reason="daily_loss_flatten")
            messages.append(f"Flattened {len(trades)} open trades at {timestamp.isoformat()} on daily-loss governor")

        signal = signals_by_ts.get(timestamp)
        update_intraday_memory(signal, config, intraday_memory_reasons)

        if halted:
            continue
        if not is_entry_time(timestamp, config):
            continue

        if config.use_candidate_engine and signal is not None:
            base_contracts = policy.contracts(signal, config)
            if base_contracts <= 0:
                continue
            selected_candidates, records = select_candidate_entries(snapshot, signal, base_contracts, config)
            condor_candidates, condor_records = select_condor_entries(snapshot, signal, base_contracts, config)
            one_dte_candidates, one_dte_records = select_one_dte_entries(snapshot, signal, base_contracts, config)
            trend_debit_candidates, trend_debit_records = select_trend_debit_entries(snapshot, signal, base_contracts, config)
            hedge_candidates, hedge_records = select_long_put_hedge_entries(snapshot, signal, base_contracts, config)
            selected_candidates.extend(condor_candidates)
            selected_candidates.extend(one_dte_candidates)
            selected_candidates.extend(trend_debit_candidates)
            selected_candidates.extend(hedge_candidates)
            candidate_records.extend(records)
            candidate_records.extend(condor_records)
            candidate_records.extend(one_dte_records)
            candidate_records.extend(trend_debit_records)
            candidate_records.extend(hedge_records)
            for candidate in selected_candidates:
                if candidate.short_quote is None or candidate.long_quote is None:
                    continue

                block_reason = entry_risk_block_reason(
                    candidate,
                    trades,
                    timestamp,
                    config,
                    global_stop_cooldown_until,
                    side_stop_cooldown_until,
                    side_stop_counts,
                    intraday_memory_reasons,
                )
                if block_reason:
                    candidate.status = "risk_blocked"
                    candidate.reason = block_reason
                    candidate.contracts = 0
                    continue

                if candidate.credit > 0:
                    remaining_credit = daily_credit_cap - gross_credit_sold
                    max_contracts_by_credit = math.floor(remaining_credit / (candidate.credit * config.multiplier))
                    contracts = min(candidate.contracts, max_contracts_by_credit)
                else:
                    contracts = candidate.contracts
                candidate.contracts = contracts
                contracts = allocator_contract_limit(candidate, sleeve_margin_used, portfolio_margin_used, config)
                candidate.contracts = contracts
                if contracts <= 0:
                    candidate.status = "risk_blocked"
                    candidate.reason = "allocator_margin_budget" if config.use_portfolio_allocator else "daily_credit_cap"
                    if config.use_portfolio_allocator:
                        continue
                    halted = True
                    halt_time = timestamp
                    messages.append(f"Daily credit cap reached at {timestamp.isoformat()}")
                    break

                trade = open_trade(
                    next_trade_id,
                    timestamp,
                    candidate.side,
                    f"candidate_{candidate.sleeve or 'core'}",
                    contracts,
                    candidate.short_quote,
                    candidate.long_quote,
                    config,
                    candidate=candidate,
                )
                if trade is None:
                    continue
                trades.append(trade)
                next_trade_id += 1
                if trade.entry_credit > 0:
                    gross_credit_sold += trade.entry_credit * trade.contracts * config.multiplier
                opened_margin = trade_margin(trade, config)
                sleeve = candidate.sleeve or "core"
                sleeve_margin_used[sleeve] = sleeve_margin_used.get(sleeve, 0.0) + opened_margin
                portfolio_margin_used += opened_margin
            continue

        for instruction in policy.instructions(signal, config):
            if instruction.contracts <= 0:
                continue

            selected = select_spread(snapshot, instruction.side, config)
            if selected is None:
                messages.append(f"No eligible {instruction.side} spread at {timestamp.isoformat()}")
                continue

            short_quote, long_quote = selected
            credit = short_quote.bid - long_quote.ask
            if credit <= 0:
                messages.append(f"Skipped non-positive credit spread at {timestamp.isoformat()}")
                continue

            remaining_credit = daily_credit_cap - gross_credit_sold
            max_contracts_by_credit = math.floor(remaining_credit / (credit * config.multiplier))
            contracts = min(instruction.contracts, max_contracts_by_credit)
            if contracts <= 0:
                halted = True
                halt_time = timestamp
                messages.append(f"Daily credit cap reached at {timestamp.isoformat()}")
                break

            trade = open_trade(
                next_trade_id,
                timestamp,
                instruction.side,
                instruction.model,
                contracts,
                short_quote,
                long_quote,
                config,
            )
            if trade is None:
                continue
            trades.append(trade)
            next_trade_id += 1
            gross_credit_sold += trade.entry_credit * trade.contracts * config.multiplier

    if close_spot is None:
        if not timestamps:
            raise ValueError("No quote timestamps supplied")
        close_spot = snapshot_spot(grouped[timestamps[-1]])
    close_snapshot = grouped[timestamps[-1]]

    for trade in trades:
        settle_trade(trade, close_spot, close_snapshot, config)

    gross_pnl = sum(trade.gross_pnl for trade in trades)
    fees = sum(trade.fees for trade in trades)
    net_pnl = sum(trade.net_pnl for trade in trades)

    return SimulationResult(
        trades=trades,
        account_equity=config.account_equity,
        gross_pnl=gross_pnl,
        fees=fees,
        net_pnl=net_pnl,
        gross_credit_sold=gross_credit_sold,
        halted=halted,
        halt_time=halt_time,
        messages=messages,
        candidate_records=candidate_records,
    )


def trades_to_rows(trades: Sequence[Trade]) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for trade in trades:
        rows.append(
            {
                "trade_id": trade.trade_id,
                "entry_time": trade.entry_time.isoformat(),
                "expiry": trade.expiry,
                "side": trade.side,
                "model": trade.model,
                "contracts": trade.contracts,
                "short": f"{compact_option_type(trade.short_type)}{trade.short_strike:g}",
                "long": f"{compact_option_type(trade.long_type)}{trade.long_strike:g}",
                "entry_credit": round(trade.entry_credit, 4),
                "stop_price": round(trade.stop_price, 4),
                "entry_spot": round(trade.entry_spot, 4) if trade.entry_spot is not None else "",
                "short_delta": round(trade.short_delta, 6) if trade.short_delta is not None else "",
                "long_delta": round(trade.long_delta, 6) if trade.long_delta is not None else "",
                "spread_width": round(trade.spread_width, 4),
                "credit_to_width": round(trade.credit_to_width, 6),
                "distance_pct": round(trade.distance_pct, 6),
                "candidate_score": round(trade.candidate_score, 6),
                "candidate_reason": trade.candidate_reason,
                "entry_straddle_residual_z": round(trade.entry_straddle_residual_z, 6),
                "entry_skew_z": round(trade.entry_skew_z, 6),
                "entry_term_ratio_z": round(trade.entry_term_ratio_z, 6),
                "entry_trend_score": round(trade.entry_trend_score, 6),
                "entry_realized_vs_implied_z": round(trade.entry_realized_vs_implied_z, 6),
                "stopped": trade.stopped,
                "stop_time": trade.stop_time.isoformat() if trade.stop_time else "",
                "stop_fill": round(trade.stop_fill, 4) if trade.stop_fill is not None else "",
                "stop_spot": round(trade.stop_spot, 4) if trade.stop_spot is not None else "",
                "exit_reason": trade.exit_reason,
                "gross_pnl": round(trade.gross_pnl, 2),
                "fees": round(trade.fees, 2),
                "net_pnl": round(trade.net_pnl, 2),
            }
        )
    return rows


def candidate_records_to_rows(records: Sequence[CandidateRecord]) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for record in records:
        rows.append(
            {
                "timestamp": record.timestamp.isoformat(),
                "side": record.side,
                "status": record.status,
                "reason": record.reason,
                "sleeve": record.sleeve,
                "contracts": record.contracts,
                "score": round(record.score, 6),
                "expiry": record.expiry,
                "short": f"{compact_option_type(record.short_type)}{record.short_strike:g}",
                "long": f"{compact_option_type(record.short_type)}{record.long_strike:g}",
                "short_delta": round(record.short_delta, 6) if record.short_delta is not None else "",
                "long_delta": round(record.long_delta, 6) if record.long_delta is not None else "",
                "spot": round(record.spot, 4),
                "distance_pct": round(record.distance_pct, 6),
                "width": round(record.width, 4),
                "credit": round(record.credit, 4),
                "credit_to_width": round(record.credit_to_width, 6),
                "stop_loss_to_credit": round(record.stop_loss_to_credit, 6),
                "straddle_residual_z": round(record.straddle_residual_z, 6),
                "skew_z": round(record.skew_z, 6),
                "term_ratio_z": round(record.term_ratio_z, 6),
                "trend_score": round(record.trend_score, 6),
                "realized_vs_implied_z": round(record.realized_vs_implied_z, 6),
            }
        )
    return rows


def classify_failure_mode(trade: Trade) -> str:
    if trade.entry_realized_vs_implied_z >= 1.50:
        return "realized_vol_shock"
    if abs(trade.entry_term_ratio_z) >= 1.50:
        return "term_structure_shock"
    if trade.side == "bull_put" and trade.entry_trend_score <= -1.00:
        return "downtrend_continuation"
    if trade.side == "bear_call" and trade.entry_trend_score >= 1.00:
        return "uptrend_continuation"
    if abs(trade.entry_skew_z) >= 1.50:
        return "skew_dislocation"
    if trade.stop_time is not None:
        minutes_to_stop = (trade.stop_time - trade.entry_time).total_seconds() / 60.0
        if minutes_to_stop <= 30:
            return "fast_adverse_move"
    return "ordinary_stop"


def stop_diagnostics_to_rows(trades: Sequence[Trade]) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for trade in trades:
        if not trade.stopped:
            continue
        minutes_to_stop = ""
        if trade.stop_time is not None:
            minutes_to_stop = round((trade.stop_time - trade.entry_time).total_seconds() / 60.0, 2)
        spot_move = ""
        adverse_spot_move = ""
        if trade.entry_spot is not None and trade.stop_spot is not None:
            spot_move = round(trade.stop_spot - trade.entry_spot, 4)
            if trade.side == "bull_put":
                adverse_spot_move = round(trade.entry_spot - trade.stop_spot, 4)
            elif trade.side == "bear_call":
                adverse_spot_move = round(trade.stop_spot - trade.entry_spot, 4)
        rows.append(
            {
                "trade_id": trade.trade_id,
                "entry_time": trade.entry_time.isoformat(),
                "stop_time": trade.stop_time.isoformat() if trade.stop_time else "",
                "minutes_to_stop": minutes_to_stop,
                "side": trade.side,
                "model": trade.model,
                "failure_mode": classify_failure_mode(trade),
                "contracts": trade.contracts,
                "short": f"{compact_option_type(trade.short_type)}{trade.short_strike:g}",
                "long": f"{compact_option_type(trade.long_type)}{trade.long_strike:g}",
                "entry_credit": round(trade.entry_credit, 4),
                "stop_price": round(trade.stop_price, 4),
                "stop_fill": round(trade.stop_fill, 4) if trade.stop_fill is not None else "",
                "entry_spot": round(trade.entry_spot, 4) if trade.entry_spot is not None else "",
                "stop_spot": round(trade.stop_spot, 4) if trade.stop_spot is not None else "",
                "spot_move": spot_move,
                "adverse_spot_move": adverse_spot_move,
                "candidate_score": round(trade.candidate_score, 6),
                "short_delta": round(trade.short_delta, 6) if trade.short_delta is not None else "",
                "spread_width": round(trade.spread_width, 4),
                "credit_to_width": round(trade.credit_to_width, 6),
                "distance_pct": round(trade.distance_pct, 6),
                "entry_straddle_residual_z": round(trade.entry_straddle_residual_z, 6),
                "entry_skew_z": round(trade.entry_skew_z, 6),
                "entry_term_ratio_z": round(trade.entry_term_ratio_z, 6),
                "entry_trend_score": round(trade.entry_trend_score, 6),
                "entry_realized_vs_implied_z": round(trade.entry_realized_vs_implied_z, 6),
                "net_pnl": round(trade.net_pnl, 2),
            }
        )
    return rows
