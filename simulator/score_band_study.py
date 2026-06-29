"""Score-band expectancy study for the 0DTE candidate engine (Phase 1).

Runs (or reuses) a validation window with a lowered score gate, buckets executed
trades by entry score, and reports per-band win rate and P&L per contract.
Also summarizes tranche-level opportunity counts from tranche_snapshots.csv.
"""
from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Dict, Iterable, List, Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]
SIMULATOR = Path(__file__).resolve().parent
DEFAULT_RESULTS = ROOT / "data" / "phase1_score_band"

SCORE_BANDS = [
    ("2.50+", 2.50, 99.0),
    ("2.40-2.49", 2.40, 2.499999),
    ("2.30-2.39", 2.30, 2.399999),
    ("2.20-2.29", 2.20, 2.299999),
    ("2.10-2.19", 2.10, 2.199999),
]


def score_band(score: float) -> str:
    for label, low, high in SCORE_BANDS:
        if low <= score <= high:
            return label
    if score >= 2.50:
        return "2.50+"
    return "below_2.10"


def read_csv(path: Path) -> List[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: Sequence[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def safe_float(value: object, default: float = 0.0) -> float:
    try:
        if value in {"", None}:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value: object, default: int = 0) -> int:
    try:
        if value in {"", None}:
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


@dataclass
class BandStats:
    band: str
    trade_count: int = 0
    contract_count: int = 0
    win_count: int = 0
    stop_count: int = 0
    total_net_pnl: float = 0.0
    pnl_per_contract_values: List[float] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.pnl_per_contract_values is None:
            self.pnl_per_contract_values = []

    @property
    def win_rate(self) -> Optional[float]:
        if self.trade_count == 0:
            return None
        return self.win_count / self.trade_count

    @property
    def stop_rate(self) -> Optional[float]:
        if self.trade_count == 0:
            return None
        return self.stop_count / self.trade_count

    @property
    def mean_pnl_per_contract(self) -> Optional[float]:
        if not self.pnl_per_contract_values:
            return None
        return mean(self.pnl_per_contract_values)

    @property
    def positive_expectancy(self) -> Optional[bool]:
        mean_pnl = self.mean_pnl_per_contract
        if mean_pnl is None:
            return None
        return mean_pnl > 0


def aggregate_trade_bands(trades: Sequence[dict]) -> Dict[str, BandStats]:
    bands: Dict[str, BandStats] = {label: BandStats(band=label) for label, _, _ in SCORE_BANDS}
    for trade in trades:
        score = safe_float(trade.get("candidate_score"))
        band = score_band(score)
        if band not in bands:
            bands[band] = BandStats(band=band)
        stats = bands[band]
        contracts = safe_int(trade.get("contracts"))
        net_pnl = safe_float(trade.get("net_pnl"))
        stopped = str(trade.get("stopped", "")).lower() in {"true", "1", "yes"}

        stats.trade_count += 1
        stats.contract_count += contracts
        stats.total_net_pnl += net_pnl
        if net_pnl > 0:
            stats.win_count += 1
        if stopped:
            stats.stop_count += 1
        if contracts > 0:
            stats.pnl_per_contract_values.append(net_pnl / contracts)
    return bands


def aggregate_tranche_opportunity_bands(tranches: Sequence[dict]) -> Dict[str, dict]:
    """Count tranches whose top passing score on either side falls in each band."""
    counts: Dict[str, dict] = {
        label: {"tranches_with_pass": 0, "tranches_executed": 0} for label, _, _ in SCORE_BANDS
    }
    for row in tranches:
        executed = safe_int(row.get("candidates_executed")) > 0
        for field in ("top_pass_bull_score", "top_pass_bear_score"):
            score_text = row.get(field, "")
            if score_text in {"", None}:
                continue
            band = score_band(safe_float(score_text))
            if band not in counts:
                continue
            counts[band]["tranches_with_pass"] += 1
            if executed:
                counts[band]["tranches_executed"] += 1
    return counts


def band_rows(bands: Dict[str, BandStats]) -> List[dict]:
    rows: List[dict] = []
    for label, _, _ in SCORE_BANDS:
        stats = bands.get(label, BandStats(band=label))
        rows.append(
            {
                "score_band": stats.band,
                "trade_count": stats.trade_count,
                "contract_count": stats.contract_count,
                "win_rate": round(stats.win_rate, 4) if stats.win_rate is not None else "",
                "stop_rate": round(stats.stop_rate, 4) if stats.stop_rate is not None else "",
                "mean_pnl_per_contract": round(stats.mean_pnl_per_contract, 2)
                if stats.mean_pnl_per_contract is not None
                else "",
                "total_net_pnl": round(stats.total_net_pnl, 2),
                "positive_expectancy": stats.positive_expectancy
                if stats.positive_expectancy is not None
                else "",
            }
        )
    return rows


def build_report(
    bands: Dict[str, BandStats],
    opportunity: Dict[str, dict],
    trades: Sequence[dict],
    tranches: Sequence[dict],
    results_dir: Path,
    min_score: float,
) -> str:
    total_trades = len(trades)
    total_tranches = len(tranches)
    tranches_with_pass = sum(1 for row in tranches if safe_int(row.get("candidates_pass")) > 0)
    tranches_executed = sum(1 for row in tranches if safe_int(row.get("candidates_executed")) > 0)

    eligible = [label for label, _, _ in SCORE_BANDS if bands.get(label, BandStats(band=label)).positive_expectancy]
    ineligible = [
        label
        for label, _, _ in SCORE_BANDS
        if bands.get(label, BandStats(band=label)).trade_count > 0
        and bands.get(label, BandStats(band=label)).positive_expectancy is False
    ]
    no_data = [label for label, _, _ in SCORE_BANDS if bands.get(label, BandStats(band=label)).trade_count == 0]

    lines = [
        "# Score Band Expectancy Study (Phase 1)",
        "",
        f"- Results directory: `{results_dir}`",
        f"- Minimum score gate used: **{min_score:.2f}**",
        f"- Executed trades: **{total_trades}**",
        f"- Tranche snapshots: **{total_tranches}** ({tranches_with_pass} with pass candidates, {tranches_executed} with executions)",
        "",
        "## Executed trade expectancy by score band",
        "",
        "| Band | Trades | Win rate | Stop rate | Mean P&L / contract | Total net P&L | Expectancy |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]

    for label, _, _ in SCORE_BANDS:
        stats = bands.get(label, BandStats(band=label))
        if stats.trade_count == 0:
            lines.append(f"| {label} | 0 | — | — | — | — | no trades |")
            continue
        expectancy = "GO" if stats.positive_expectancy else "NO-GO"
        lines.append(
            "| {band} | {trades} | {win:.1%} | {stop:.1%} | ${ppc:,.0f} | ${total:,.0f} | **{exp}** |".format(
                band=label,
                trades=stats.trade_count,
                win=stats.win_rate or 0.0,
                stop=stats.stop_rate or 0.0,
                ppc=stats.mean_pnl_per_contract or 0.0,
                total=stats.total_net_pnl,
                exp=expectancy,
            )
        )

    lines.extend(
        [
            "",
            "## Tranche opportunity funnel (top pass score per side)",
            "",
            "| Band | Tranches w/ pass | Tranches executed |",
            "| --- | ---: | ---: |",
        ]
    )
    for label, _, _ in SCORE_BANDS:
        opp = opportunity.get(label, {"tranches_with_pass": 0, "tranches_executed": 0})
        lines.append(
            f"| {label} | {opp['tranches_with_pass']} | {opp['tranches_executed']} |"
        )

    lines.extend(
        [
            "",
            "## Go / no-go for “trade more frequently” hypothesis",
            "",
        ]
    )
    if eligible:
        lines.append(f"- **Eligible bands (positive expectancy):** {', '.join(eligible)}")
    else:
        lines.append("- **Eligible bands:** none with positive per-contract expectancy in this sample")
    if ineligible:
        lines.append(f"- **Ineligible bands (negative expectancy):** {', '.join(ineligible)}")
    if no_data:
        lines.append(f"- **Insufficient trade data:** {', '.join(no_data)}")

    sub_250_labels = ("2.40-2.49", "2.30-2.39", "2.20-2.29", "2.10-2.19")
    sub_250_eligible = [label for label in sub_250_labels if label in eligible]
    sub_250_ineligible = [label for label in sub_250_labels if label in ineligible]
    if sub_250_eligible and not sub_250_ineligible:
        lines.append(
            "- Sub-2.50 bands show positive expectancy in this run; lowering the gate may add frequency "
            "without destroying edge — validate with bootstrap / longer window before sizing up."
        )
    elif sub_250_ineligible and not sub_250_eligible:
        lines.append(
            "- Sub-2.50 bands show **negative** per-contract expectancy when actually executed. "
            "The frequency hypothesis is **not supported** on this sample — keep core gate at 2.50."
        )
    elif sub_250_eligible and sub_250_ineligible:
        lines.append(
            f"- Mixed sub-2.50 results: positive in {', '.join(sub_250_eligible)} but negative in "
            f"{', '.join(sub_250_ineligible)}. Do **not** blanket-lower the gate; at most pilot "
            f"{sub_250_eligible[0]} with tight size caps."
        )
    else:
        lines.append(
            "- Sub-2.50 bands did not produce enough executed trades to confirm edge. "
            "Treat frequency expansion as **inconclusive**, not validated."
        )

    return "\n".join(lines) + "\n"


def run_validation(args: argparse.Namespace) -> None:
    cmd = [
        sys.executable,
        str(SIMULATOR / "regime_validation.py"),
        "--processed-dir",
        str(args.processed_dir),
        "--results-dir",
        str(args.results_dir),
        "--start-date",
        args.start_date,
        "--end-date",
        args.end_date,
        "--account-equity",
        str(args.account_equity),
        "--baseline-contracts",
        str(args.baseline_contracts),
        "--daily-credit-cap-pct",
        str(args.daily_credit_cap_pct),
        "--daily-loss-limit-pct",
        str(args.daily_loss_limit_pct),
        "--candidate-min-score",
        str(args.candidate_min_score),
        "--exploratory-min-score",
        str(args.exploratory_min_score),
        "--exploratory-max-score",
        str(args.exploratory_max_score),
        "--two-tier-engine",
        "--event-controls",
        "--time-of-day-controls",
        "--portfolio-allocator",
        "--portfolio-margin-budget-pct",
        str(args.portfolio_margin_budget_pct),
        "--core-margin-budget-pct",
        str(args.core_margin_budget_pct),
        "--exploratory-margin-budget-pct",
        str(args.exploratory_margin_budget_pct),
    ]
    if args.flatten_on_daily_loss:
        cmd.append("--flatten-on-daily-loss")
    print("Running validation:", " ".join(cmd))
    subprocess.run(cmd, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Score-band expectancy study (Phase 1)")
    parser.add_argument("--processed-dir", type=Path, default=ROOT / "data" / "processed")
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--start-date", default="2025-04-01")
    parser.add_argument("--end-date", default="2025-09-30")
    parser.add_argument("--account-equity", type=float, default=13_000_000.0)
    parser.add_argument("--baseline-contracts", type=int, default=1140)
    parser.add_argument("--daily-credit-cap-pct", type=float, default=0.05)
    parser.add_argument("--daily-loss-limit-pct", type=float, default=0.0225)
    parser.add_argument("--flatten-on-daily-loss", action="store_true", default=True)
    parser.add_argument("--candidate-min-score", type=float, default=2.10)
    parser.add_argument("--exploratory-min-score", type=float, default=2.10)
    parser.add_argument("--exploratory-max-score", type=float, default=2.49)
    parser.add_argument("--portfolio-margin-budget-pct", type=float, default=0.40)
    parser.add_argument("--core-margin-budget-pct", type=float, default=0.35)
    parser.add_argument("--exploratory-margin-budget-pct", type=float, default=0.02)
    parser.add_argument("--skip-run", action="store_true", help="Analyze existing results only")
    args = parser.parse_args()

    if not args.skip_run:
        run_validation(args)

    results_dir = Path(args.results_dir)
    trades = read_csv(results_dir / "trades.csv")
    tranches = read_csv(results_dir / "tranche_snapshots.csv")
    if not tranches:
        raise SystemExit(f"Missing tranche_snapshots.csv in {results_dir}. Re-run without --skip-run.")

    bands = aggregate_trade_bands(trades)
    opportunity = aggregate_tranche_opportunity_bands(tranches)
    summary_rows = band_rows(bands)
    for row in summary_rows:
        opp = opportunity.get(row["score_band"], {})
        row["tranches_with_pass"] = opp.get("tranches_with_pass", 0)
        row["tranches_executed"] = opp.get("tranches_executed", 0)

    write_csv(results_dir / "score_band_summary.csv", summary_rows)
    report = build_report(bands, opportunity, trades, tranches, results_dir, args.candidate_min_score)
    (results_dir / "score_band_report.md").write_text(report, encoding="utf-8")

    print(f"wrote {results_dir / 'score_band_summary.csv'}")
    print(f"wrote {results_dir / 'score_band_report.md'}")
    print()
    print(report)


if __name__ == "__main__":
    main()
