"""Variant registry for selective short-premium overlay (straddle + iron condor).

Anti-overfit:
  - Rank only on selection (<= SELECTION_END)
  - Sealed holdout (>= HOLDOUT_START)

IC fee / low-vol rule (hard default):
  Iron condors skip when VIX open < IC_MIN_VIX or when net credit cannot clear
  MIN_FEE_MULTIPLE × round-trip fees. Low-vol credits are fee-dominated.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

SELECTION_END = "2023-12-29"
HOLDOUT_START = "2024-01-02"

ACCOUNT = 13_000_000.0
FEE_PER_CONTRACT = 0.79
OVERLAY_CONTRACTS = 4
MULTIPLIER = 100

# IC: 4 legs open + 4 close
IC_FEE_LEGS = 8
# Straddle: 2 legs open + 2 close
STRADDLE_FEE_LEGS = 4

IC_MIN_VIX = 15.0
IC_MIN_FEE_MULTIPLE = 5.0  # credit $ must be >= 5× round-trip fees
IC_MIN_CREDIT = 0.50  # points, after fee filter
IC_DEFAULT_WING = 50.0
IC_DEFAULT_DELTA = 0.12


@dataclass(frozen=True)
class Gate:
    name: str
    residual_lo: Optional[float] = None
    residual_hi: Optional[float] = None
    residual_min: Optional[float] = None  # floor-only (no upper)
    abs_trend_max: Optional[float] = None
    rv_max: Optional[float] = None
    rv_lo: Optional[float] = None
    rv_hi: Optional[float] = None
    abs_term_max: Optional[float] = None
    skip_fomc: bool = False
    vix_lo: Optional[float] = None
    vix_hi: Optional[float] = None
    always: bool = False  # ignore features (still subject to structure filters)


@dataclass(frozen=True)
class Structure:
    name: str
    kind: str  # "none" | "straddle" | "ic"
    target_delta: float = IC_DEFAULT_DELTA
    wing_width: float = IC_DEFAULT_WING
    # IC-only: require VIX / fee clearance (False only for diagnostic controls)
    enforce_low_vol_skip: bool = True
    min_vix: float = IC_MIN_VIX
    min_fee_multiple: float = IC_MIN_FEE_MULTIPLE


@dataclass
class Variant:
    phase: str
    name: str
    gate: Gate
    structure: Structure
    # management
    stop_multiple: Optional[float] = None  # debit >= multiple × credit
    take_profit_frac: Optional[float] = None  # flatten if remaining debit <= (1-frac)*credit
    time_exit: Optional[str] = None  # "14:00"
    directional_stop: bool = False  # IC: short ITM by >= 0.5× wing
    stop_confirmation_count: int = 1  # consecutive bars beyond stop before exit
    contracts: int = OVERLAY_CONTRACTS
    # book interaction
    skip_if_vertical_halted: bool = False
    skip_if_vertical_stopped: bool = False
    skip_if_vertical_entered: bool = False


def gate_passes(
    gate: Gate,
    *,
    residual: float,
    trend: float,
    rv: float,
    term: float,
    vix: Optional[float],
    is_fomc: bool,
) -> bool:
    if gate.always:
        ok = True
    else:
        ok = True
        if gate.residual_min is not None and residual < gate.residual_min:
            ok = False
        if gate.residual_lo is not None and residual < gate.residual_lo:
            ok = False
        if gate.residual_hi is not None and residual > gate.residual_hi:
            ok = False
        if gate.abs_trend_max is not None and abs(trend) > gate.abs_trend_max:
            ok = False
        if gate.rv_max is not None and rv > gate.rv_max:
            ok = False
        if gate.rv_lo is not None and rv < gate.rv_lo:
            ok = False
        if gate.rv_hi is not None and rv > gate.rv_hi:
            ok = False
        if gate.abs_term_max is not None and abs(term) > gate.abs_term_max:
            ok = False
    if not ok:
        return False
    if gate.skip_fomc and is_fomc:
        return False
    if gate.vix_lo is not None:
        if vix is None or vix < gate.vix_lo:
            return False
    if gate.vix_hi is not None:
        if vix is None or vix > gate.vix_hi:
            return False
    return True


def ic_fee_dollars(contracts: int) -> float:
    return IC_FEE_LEGS * contracts * FEE_PER_CONTRACT


def straddle_fee_dollars(contracts: int) -> float:
    return STRADDLE_FEE_LEGS * contracts * FEE_PER_CONTRACT


def ic_min_credit_points(contracts: int, fee_multiple: float) -> float:
    """Minimum net credit (points) so credit$ >= fee_multiple × fees."""
    return (fee_multiple * ic_fee_dollars(contracts)) / (contracts * MULTIPLIER)


NONE = Structure("none", "none")
STRADDLE = Structure("straddle", "straddle")
IC_D12 = Structure("ic_d12", "ic", target_delta=0.12, wing_width=50.0)
IC_D10 = Structure("ic_d10", "ic", target_delta=0.10, wing_width=50.0)
IC_D16 = Structure("ic_d16", "ic", target_delta=0.16, wing_width=50.0, enforce_low_vol_skip=True)
# Control: IC without VIX floor (expect fee drag in low vol)
IC_D12_NO_VIX = Structure(
    "ic_d12_novix",
    "ic",
    target_delta=0.12,
    wing_width=50.0,
    enforce_low_vol_skip=False,
    min_vix=0.0,
    min_fee_multiple=0.0,
)


def _g(name: str, **kw) -> Gate:
    return Gate(name=name, **kw)


def build_phase_a_ic_only() -> List[Variant]:
    """Diagnostic: always enter IC_d12 (with low-vol skip) for quintile labeling."""
    return [
        Variant("a1c", "A1c_ic_d12", _g("always", always=True), IC_D12),
        Variant("a1c", "A1c_ic_d12_novix", _g("always", always=True), IC_D12_NO_VIX),
    ]


def build_phase_b_variants() -> List[Variant]:
    variants: List[Variant] = []

    def add(phase: str, name: str, gate: Gate, structure: Structure) -> None:
        variants.append(Variant(phase, name, gate, structure))

    # B0 controls
    add("B0", "B0_prod_only", _g("off"), NONE)
    add("B0", "B_always_S", _g("always", always=True), STRADDLE)
    add("B0", "B_always_IC", _g("always", always=True), IC_D12)
    add("B0", "B_always_IC_novix", _g("always", always=True), IC_D12_NO_VIX)
    add("B0", "B_rich1_S", _g("rich1", residual_min=1.0), STRADDLE)
    add("B0", "B_rich1_IC", _g("rich1", residual_min=1.0), IC_D12)

    # B1 singles — both structures
    b1_gates = [
        _g("res_m025_1", residual_lo=-0.25, residual_hi=1.0),
        _g("res_0_1", residual_lo=0.0, residual_hi=1.0),
        _g("res_05_15", residual_lo=0.5, residual_hi=1.5),
        _g("res_0_15", residual_lo=0.0, residual_hi=1.5),
        _g("res_ge_0", residual_min=0.0),
        _g("res_ge_05", residual_min=0.5),
        _g("trend_025", abs_trend_max=0.25),
        _g("trend_05", abs_trend_max=0.5),
        _g("trend_10", abs_trend_max=1.0),
        _g("rv_le_05", rv_max=0.5),
        _g("rv_le_10", rv_max=1.0),
        _g("rv_band", rv_lo=-1.0, rv_hi=1.0),
    ]
    for g in b1_gates:
        add("B1", f"B1_{g.name}_S", g, STRADDLE)
        add("B1", f"B1_{g.name}_IC", g, IC_D12)

    # B2 combos
    b2 = [
        _g("B2_1", residual_lo=-0.25, residual_hi=1.0, abs_trend_max=0.5),
        _g("B2_2", residual_lo=0.5, residual_hi=1.5, abs_trend_max=0.5),
        _g("B2_3", residual_lo=0.5, residual_hi=1.5, abs_trend_max=0.5, rv_max=1.0),
        _g("B2_4", residual_lo=0.0, residual_hi=1.0, abs_trend_max=0.5, rv_lo=-1.0, rv_hi=1.0),
        _g("B2_5", residual_lo=0.5, residual_hi=1.5, abs_trend_max=0.5, skip_fomc=True),
        _g("B2_6", residual_lo=0.5, residual_hi=1.5, abs_trend_max=0.5, vix_lo=15.0, vix_hi=30.0),
        _g("B2_7", residual_lo=0.5, residual_hi=1.5, abs_trend_max=0.5, abs_term_max=1.0),
        _g("B2_8", abs_trend_max=0.5),
    ]
    for g in b2:
        add("B2", f"{g.name}_S", g, STRADDLE)
        add("B2", f"{g.name}_IC", g, IC_D12)

    # Extra IC structure axis on strongest seed gates only
    for g in [b2[1], b2[2]]:  # B2_2, B2_3
        add("B2", f"{g.name}_IC_d10", g, IC_D10)
        add("B2", f"{g.name}_IC_d16", g, IC_D16)

    return variants


def build_phase_c_variants(base_gate: Gate, structure: Structure, prefix: str) -> List[Variant]:
    """Exit grid on a frozen entry gate × structure."""
    out: List[Variant] = []
    specs = [
        ("C0_eod", None, None, None, False),
        ("C1_stop2x", 2.0, None, None, False),
        ("C2_stop15x", 1.5, None, None, False),
        ("C3_tp50", None, 0.5, None, False),
        ("C4_flat14", None, None, "14:00", False),
        ("C5_stop2x_tp50", 2.0, 0.5, None, False),
    ]
    if structure.kind == "ic":
        specs.append(("C6_dir_stop", 2.0, None, None, True))
    for name, stop, tp, texit, dstop in specs:
        out.append(
            Variant(
                "C",
                f"{prefix}_{name}",
                base_gate,
                structure,
                stop_multiple=stop,
                take_profit_frac=tp,
                time_exit=texit,
                directional_stop=dstop,
            )
        )
    return out


def build_phase_d_variants(base_gate: Gate, structure: Structure, prefix: str) -> List[Variant]:
    """Sizing / book interaction on frozen managed entry (C0 defaults)."""
    out: List[Variant] = []
    for n in (2, 4, 8):
        out.append(Variant("D", f"{prefix}_D1_n{n}", base_gate, structure, contracts=n, stop_multiple=2.0))
    out.append(
        Variant(
            "D",
            f"{prefix}_D3_skip_halt",
            base_gate,
            structure,
            stop_multiple=2.0,
            skip_if_vertical_halted=True,
        )
    )
    out.append(
        Variant(
            "D",
            f"{prefix}_D4_skip_vstop",
            base_gate,
            structure,
            stop_multiple=2.0,
            skip_if_vertical_stopped=True,
        )
    )
    out.append(
        Variant(
            "D",
            f"{prefix}_D5_skip_ventry",
            base_gate,
            structure,
            stop_multiple=2.0,
            skip_if_vertical_entered=True,
        )
    )
    return out


def build_all_b_plus_ref() -> List[Variant]:
    return build_phase_b_variants()
