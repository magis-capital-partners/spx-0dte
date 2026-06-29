"""Diagnose why the candidate engine fires infrequently using tranche_snapshots.csv."""
from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean
from typing import Dict, List, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TRANCHE = ROOT / "data" / "phase0_tranche" / "tranche_snapshots.csv"
DEFAULT_OUT = ROOT / "data" / "signal_diagnostics"

SCORE_BANDS = [
    ("below_2.0", 0.0, 1.999),
    ("2.0-2.24", 2.0, 2.249),
    ("2.25-2.39", 2.25, 2.399),
    ("2.40-2.49", 2.40, 2.499),
    ("2.50+", 2.50, 99.0),
]


def read_csv(path: Path) -> List[dict]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
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


def score_band(score: float) -> str:
    for label, low, high in SCORE_BANDS:
        if low <= score <= high:
            return label
    return "below_2.0"


def best_score(row: dict) -> float:
    values = [
        safe_float(row.get("best_pass_score"), default=-1.0),
        safe_float(row.get("best_overall_score"), default=-1.0),
        safe_float(row.get("top_pass_bull_score"), default=-1.0),
        safe_float(row.get("top_pass_bear_score"), default=-1.0),
    ]
    return max(values)


def analyze(rows: Sequence[dict]) -> tuple[List[dict], List[dict], dict]:
    band_counts = Counter()
    band_days = defaultdict(set)
    executed_bands = Counter()
    skip_reasons = Counter()
    reject_reasons = Counter()
    gated_tranches = 0
    low_score_tranches = 0
    pass_tranches = 0
    executed_tranches = 0
    scores: List[float] = []

    for row in rows:
        date = row.get("date", "")
        skip = row.get("skip_reason", "")
        if skip:
            skip_reasons[skip] += 1
        if safe_int(row.get("candidates_gated")) > 0 and safe_int(row.get("candidates_pass")) == 0:
            gated_tranches += 1
        if row.get("top_reject_reason") == "low_score":
            low_score_tranches += 1
        if safe_int(row.get("candidates_pass")) > 0:
            pass_tranches += 1
        if safe_int(row.get("candidates_executed")) > 0:
            executed_tranches += 1
        if row.get("top_reject_reason"):
            reject_reasons[row["top_reject_reason"]] += 1

        score = best_score(row)
        if score >= 0:
            scores.append(score)
            band = score_band(score)
            band_counts[band] += 1
            band_days[band].add(date)
            if safe_int(row.get("candidates_executed")) > 0:
                executed_bands[band] += 1

    score_band_rows = []
    for label, _, _ in SCORE_BANDS:
        count = band_counts[label]
        score_band_rows.append(
            {
                "score_band": label,
                "tranches": count,
                "unique_days": len(band_days[label]),
                "executed_tranches": executed_bands[label],
                "execution_rate": round(executed_bands[label] / count, 4) if count else 0.0,
            }
        )

    daily_rows = []
    by_date: Dict[str, List[dict]] = defaultdict(list)
    for row in rows:
        by_date[row.get("date", "")].append(row)
    for date, day_rows in sorted(by_date.items()):
        day_scores = [best_score(row) for row in day_rows if best_score(row) >= 0]
        daily_rows.append(
            {
                "date": date,
                "tranches": len(day_rows),
                "max_best_score": round(max(day_scores), 4) if day_scores else "",
                "mean_best_score": round(mean(day_scores), 4) if day_scores else "",
                "pass_tranches": sum(1 for row in day_rows if safe_int(row.get("candidates_pass")) > 0),
                "executed_tranches": sum(1 for row in day_rows if safe_int(row.get("candidates_executed")) > 0),
                "gated_only_tranches": sum(
                    1
                    for row in day_rows
                    if safe_int(row.get("candidates_gated")) > 0 and safe_int(row.get("candidates_pass")) == 0
                ),
            }
        )

    summary = {
        "tranche_rows": len(rows),
        "unique_days": len(by_date),
        "executed_tranches": executed_tranches,
        "pass_tranches": pass_tranches,
        "gated_only_tranches": gated_tranches,
        "low_score_reject_tranches": low_score_tranches,
        "mean_best_score": round(mean(scores), 4) if scores else 0.0,
        "max_best_score": round(max(scores), 4) if scores else 0.0,
        "pct_tranches_best_score_ge_2.50": round(sum(1 for score in scores if score >= 2.50) / len(scores), 4) if scores else 0.0,
        "pct_tranches_best_score_ge_2.40": round(sum(1 for score in scores if score >= 2.40) / len(scores), 4) if scores else 0.0,
        "skip_reasons": dict(skip_reasons),
        "top_reject_reasons": reject_reasons.most_common(8),
    }
    return score_band_rows, daily_rows, summary


def build_report(summary: dict, score_band_rows: Sequence[dict], source: Path) -> str:
    lines = [
        "# Tranche Signal Diagnostic",
        "",
        f"- Source: `{source}`",
        f"- Tranche rows: **{summary['tranche_rows']}** across **{summary['unique_days']}** days",
        f"- Executed tranches: **{summary['executed_tranches']}**",
        f"- Tranches with pass candidates: **{summary['pass_tranches']}**",
        "",
        "## Headline",
        "",
    ]

    if summary["pct_tranches_best_score_ge_2.50"] < 0.05:
        lines.append(
            "- **Signal bottleneck:** fewer than 5% of tranches even reach score 2.50. "
            "The model rarely produces high-quality candidates — lowering the gate alone will not create edge."
        )
    elif summary["gated_only_tranches"] > summary["pass_tranches"]:
        lines.append(
            "- **Gate bottleneck:** side/regime gates block more tranches than the score filter. "
            "Review `_side_gate_reason` thresholds before lowering `candidate_min_score`."
        )
    else:
        lines.append(
            "- **Mixed bottleneck:** both score quality and gate filters matter; inspect band table below."
        )

    lines.extend(
        [
            "",
            f"- Mean best score per tranche: **{summary['mean_best_score']:.3f}** (max **{summary['max_best_score']:.3f}**)",
            f"- Tranches with best score >= 2.50: **{summary['pct_tranches_best_score_ge_2.50']:.1%}**",
            f"- Tranches with best score >= 2.40: **{summary['pct_tranches_best_score_ge_2.40']:.1%}**",
            "",
            "## Score band distribution (best score per tranche)",
            "",
            "| Band | Tranches | Unique days | Executed | Exec rate |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in score_band_rows:
        lines.append(
            f"| {row['score_band']} | {row['tranches']} | {row['unique_days']} | "
            f"{row['executed_tranches']} | {row['execution_rate']:.1%} |"
        )

    lines.extend(["", "## Skip reasons", ""])
    for reason, count in sorted(summary["skip_reasons"].items(), key=lambda item: -item[1]):
        lines.append(f"- `{reason or '(executed)'}`: {count}")

    lines.extend(["", "## Top reject reasons (when candidates exist)", ""])
    for reason, count in summary["top_reject_reasons"]:
        lines.append(f"- `{reason}`: {count}")

    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Diagnose tranche score distributions and firing bottlenecks.")
    parser.add_argument("--tranche-csv", type=Path, default=DEFAULT_TRANCHE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    rows = read_csv(args.tranche_csv)
    if not rows:
        raise SystemExit(f"No rows in {args.tranche_csv}")

    score_band_rows, daily_rows, summary = analyze(rows)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(args.output_dir / "score_band_distribution.csv", score_band_rows)
    write_csv(args.output_dir / "daily_score_summary.csv", daily_rows)
    report = build_report(summary, score_band_rows, args.tranche_csv)
    (args.output_dir / "tranche_diagnostic_report.md").write_text(report, encoding="utf-8")
    print(f"wrote {args.output_dir / 'tranche_diagnostic_report.md'}")
    print(report)


if __name__ == "__main__":
    main()
