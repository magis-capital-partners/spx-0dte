"""Variant registry for overlay Calmar structure grid (IC widths × straddle stops).

See overlay_calmar_structure_test_plan.md.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from selective_overlay_variants import (
    NONE,
    STRADDLE,
    Gate,
    Structure,
    Variant,
)

SELECTION_END = "2023-12-29"
HOLDOUT_START = "2024-01-02"
ACCOUNT = 13_000_000.0
DEFAULT_CONTRACTS = 8

ALWAYS = Gate("always", always=True)
OFF = Gate("off")

IC_WIDTHS = (25.0, 35.0, 50.0, 75.0, 100.0, 150.0)
IC_DELTAS = (0.10, 0.12, 0.16)
STRADDLE_STOPS = (None, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0)
SOFT_STOPS = (1.5, 2.0, 2.5)


def _ic(width: float, delta: float) -> Structure:
    tag = f"ic_w{int(width)}_d{int(round(delta * 100))}"
    return Structure(tag, "ic", target_delta=delta, wing_width=width)


def _stop_tag(stop: Optional[float]) -> str:
    if stop is None:
        return "eod"
    # e.g. 1.25 -> k1p25, 2.0 -> k2, 2.5 -> k2p5
    return "k" + f"{stop:g}".replace(".", "p")


def build_phase0() -> List[Variant]:
    return [
        Variant("P0", "P0_prod", OFF, NONE, contracts=DEFAULT_CONTRACTS),
        Variant("P0", "P0_ic50", ALWAYS, _ic(50.0, 0.12), contracts=DEFAULT_CONTRACTS),
        Variant("P0", "P0_s_eod", ALWAYS, STRADDLE, contracts=DEFAULT_CONTRACTS),
        Variant(
            "P0",
            "P0_s_2x",
            ALWAYS,
            STRADDLE,
            stop_multiple=2.0,
            contracts=DEFAULT_CONTRACTS,
        ),
    ]


def build_phase1() -> List[Variant]:
    out: List[Variant] = []
    for w in IC_WIDTHS:
        out.append(
            Variant(
                "P1",
                f"IC_w{int(w)}_d12",
                ALWAYS,
                _ic(w, 0.12),
                contracts=DEFAULT_CONTRACTS,
            )
        )
    return out


def build_phase1b(widths: Optional[Sequence[float]] = None) -> List[Variant]:
    """Delta cross. Default: all widths (full test). Pass top widths when freezing."""
    use = list(widths) if widths is not None else list(IC_WIDTHS)
    out: List[Variant] = []
    for w in use:
        for d in IC_DELTAS:
            if abs(d - 0.12) < 1e-9:
                continue  # already in P1
            out.append(
                Variant(
                    "P1b",
                    f"IC_w{int(w)}_d{int(round(d * 100))}",
                    ALWAYS,
                    _ic(w, d),
                    contracts=DEFAULT_CONTRACTS,
                )
            )
    return out


def build_phase2() -> List[Variant]:
    out: List[Variant] = []
    for stop in STRADDLE_STOPS:
        name = f"S_{_stop_tag(stop)}"
        out.append(
            Variant(
                "P2",
                name,
                ALWAYS,
                STRADDLE,
                stop_multiple=stop,
                contracts=DEFAULT_CONTRACTS,
            )
        )
    return out


def build_phase2b(stops: Optional[Sequence[Optional[float]]] = None) -> List[Variant]:
    use = list(stops) if stops is not None else [s for s in STRADDLE_STOPS if s is not None]
    out: List[Variant] = []
    for stop in use:
        out.append(
            Variant(
                "P2b",
                f"S_{_stop_tag(stop)}_c2",
                ALWAYS,
                STRADDLE,
                stop_multiple=stop,
                stop_confirmation_count=2,
                contracts=DEFAULT_CONTRACTS,
            )
        )
    return out


def build_phase2c(stops: Optional[Sequence[float]] = None) -> List[Variant]:
    use = list(stops) if stops is not None else list(SOFT_STOPS)
    out: List[Variant] = []
    for stop in use:
        tag = _stop_tag(stop)
        out.append(
            Variant(
                "P2c",
                f"S_{tag}_tp50",
                ALWAYS,
                STRADDLE,
                stop_multiple=stop,
                take_profit_frac=0.5,
                contracts=DEFAULT_CONTRACTS,
            )
        )
        out.append(
            Variant(
                "P2c",
                f"S_{tag}_flat14",
                ALWAYS,
                STRADDLE,
                stop_multiple=stop,
                time_exit="14:00",
                contracts=DEFAULT_CONTRACTS,
            )
        )
        out.append(
            Variant(
                "P2c",
                f"S_{tag}_tp50_flat14",
                ALWAYS,
                STRADDLE,
                stop_multiple=stop,
                take_profit_frac=0.5,
                time_exit="14:00",
                contracts=DEFAULT_CONTRACTS,
            )
        )
    return out


def build_phase3(
    ic_structures: Sequence[Structure],
    straddle_bases: Sequence[Variant],
) -> List[Variant]:
    """Size + book interaction on frozen IC structures and straddle stop configs."""
    out: List[Variant] = []
    for st in ic_structures:
        for n in (4, 8, 12):
            out.append(
                Variant("P3", f"{st.name}_n{n}", ALWAYS, st, contracts=n)
            )
        out.append(
            Variant(
                "P3",
                f"{st.name}_n8_skip_vhalt",
                ALWAYS,
                st,
                contracts=8,
                skip_if_vertical_halted=True,
            )
        )
        out.append(
            Variant(
                "P3",
                f"{st.name}_n8_skip_vstop",
                ALWAYS,
                st,
                contracts=8,
                skip_if_vertical_stopped=True,
            )
        )
    for base in straddle_bases:
        for n in (4, 8, 12):
            out.append(
                Variant(
                    "P3",
                    f"{base.name}_n{n}",
                    ALWAYS,
                    STRADDLE,
                    stop_multiple=base.stop_multiple,
                    stop_confirmation_count=base.stop_confirmation_count,
                    take_profit_frac=base.take_profit_frac,
                    time_exit=base.time_exit,
                    contracts=n,
                )
            )
        out.append(
            Variant(
                "P3",
                f"{base.name}_n8_skip_vhalt",
                ALWAYS,
                STRADDLE,
                stop_multiple=base.stop_multiple,
                stop_confirmation_count=base.stop_confirmation_count,
                take_profit_frac=base.take_profit_frac,
                time_exit=base.time_exit,
                contracts=8,
                skip_if_vertical_halted=True,
            )
        )
        out.append(
            Variant(
                "P3",
                f"{base.name}_n8_skip_vstop",
                ALWAYS,
                STRADDLE,
                stop_multiple=base.stop_multiple,
                stop_confirmation_count=base.stop_confirmation_count,
                take_profit_frac=base.take_profit_frac,
                time_exit=base.time_exit,
                contracts=8,
                skip_if_vertical_stopped=True,
            )
        )
    # de-dupe
    seen = set()
    uniq: List[Variant] = []
    for v in out:
        if v.name in seen:
            continue
        seen.add(v.name)
        uniq.append(v)
    return uniq


def build_phase5_salvage(best_ic: Structure, best_straddle: Optional[Variant]) -> List[Variant]:
    """Optional calm gate on frozen structures."""
    calm = Gate("B2_2", residual_lo=0.5, residual_hi=1.5, abs_trend_max=0.5)
    out = [
        Variant("P5", f"{best_ic.name}_calm", calm, best_ic, contracts=DEFAULT_CONTRACTS),
    ]
    if best_straddle is not None:
        out.append(
            Variant(
                "P5",
                f"{best_straddle.name}_calm",
                calm,
                STRADDLE,
                stop_multiple=best_straddle.stop_multiple,
                stop_confirmation_count=best_straddle.stop_confirmation_count,
                take_profit_frac=best_straddle.take_profit_frac,
                time_exit=best_straddle.time_exit,
                contracts=DEFAULT_CONTRACTS,
            )
        )
    return out


def build_structure_grid() -> List[Variant]:
    """P0–P2c in one shot (structure risk shape). P3/P5 need freeze."""
    out: List[Variant] = []
    seen = set()
    for v in (
        build_phase0()
        + build_phase1()
        + build_phase1b()
        + build_phase2()
        + build_phase2b()
        + build_phase2c()
    ):
        if v.name in seen:
            continue
        seen.add(v.name)
        out.append(v)
    return out


def dedupe_variants(variants: Sequence[Variant]) -> List[Variant]:
    seen = set()
    out: List[Variant] = []
    for v in variants:
        if v.name in seen:
            continue
        seen.add(v.name)
        out.append(v)
    return out


def variants_by_name(variants: Sequence[Variant]) -> Dict[str, Variant]:
    return {v.name: v for v in variants}


def save_winners(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_winners(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
