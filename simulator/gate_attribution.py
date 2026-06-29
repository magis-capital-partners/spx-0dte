"""Test 1: full-sample gate attribution by regime label and MBH return bucket."""
from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
MBH_DAILY_RETURN_COL = 2


def parse_pct(value: str) -> Optional[float]:
    value = (value or "").strip().strip('"')
    if not value or value.upper() == "NA":
        return None
    try:
        return float(value.replace("%", "").replace(",", "")) / 100.0
    except ValueError:
        return None


def parse_sheet_date(value: str) -> Optional[date]:
    value = (value or "").strip().strip('"')
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def read_csv(path: Path) -> List[dict]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def read_mbh_daily(path: Path) -> Dict[str, float]:
    out: Dict[str, float] = {}
    if not path.exists():
        return out
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.reader(handle):
            if len(row) <= MBH_DAILY_RETURN_COL:
                continue
            d = parse_sheet_date(row[0])
            if d is None:
                continue
            ret = parse_pct(row[MBH_DAILY_RETURN_COL])
            if ret is not None:
                out[d.isoformat()] = ret
    return out


def mbh_bucket(ret: Optional[float]) -> str:
    if ret is None:
        return "no_mbh"
    if ret >= 0.01:
        return "strong_green_ge_1pct"
    if ret > 0:
        return "green_0_to_1pct"
    if ret == 0:
        return "flat"
    return "red"


def safe_int(value: object) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def classify_tranche(row: dict) -> str:
    if safe_int(row.get("candidates_executed")) > 0:
        return "executed"
    skip = row.get("skip_reason", "")
    if skip in {"halted", "zero_policy_contracts", "no_signal"}:
        return skip
    if safe_int(row.get("candidates_pass")) > 0:
        return "passed_not_executed"
    if safe_int(row.get("candidates_gated")) > 0:
        return "gated"
    if row.get("top_reject_reason") == "low_score":
        return "low_score"
    return "other"


def run_attribution(
    tranche_csv: Path,
    candidate_reason_csv: Path,
    daily_csv: Path,
    mbh_sheets: List[Path],
    start_date: str,
    end_date: str,
) -> Tuple[List[dict], List[dict], List[dict], dict]:
    mbh: Dict[str, float] = {}
    for sheet in mbh_sheets:
        mbh.update(read_mbh_daily(sheet))

    daily_by_date = {row["date"]: row for row in read_csv(daily_csv)}
    tranches = [row for row in read_csv(tranche_csv) if start_date <= row.get("date", "") <= end_date]

    # Tranche-level attribution
    by_bucket_decision: Dict[Tuple[str, str], int] = Counter()
    by_regime_decision: Dict[Tuple[str, str], int] = Counter()
    gate_reasons_by_bucket: Dict[Tuple[str, str], int] = Counter()

    for row in tranches:
        d = row.get("date", "")
        bucket = mbh_bucket(mbh.get(d))
        decision = classify_tranche(row)
        by_bucket_decision[(bucket, decision)] += 1
        regime = daily_by_date.get(d, {}).get("regime", "unknown")
        primary_regime = regime.split("|")[0] if regime else "unknown"
        by_regime_decision[(primary_regime, decision)] += 1

    for row in read_csv(candidate_reason_csv):
        d = row.get("date", "")
        if not (start_date <= d <= end_date):
            continue
        bucket = mbh_bucket(mbh.get(d))
        status = row.get("status", "")
        reason = row.get("reason", "")
        count = safe_int(row.get("count"))
        if status == "gated":
            gate_reasons_by_bucket[(bucket, reason)] += count
        elif status == "rejected":
            gate_reasons_by_bucket[(bucket, f"rejected:{reason}")] += count
        elif status in {"risk_blocked", "blocked"}:
            gate_reasons_by_bucket[(bucket, f"{status}:{reason}")] += count

    bucket_rows = []
    buckets = sorted({b for b, _ in by_bucket_decision})
    decisions = sorted({d for _, d in by_bucket_decision})
    for bucket in buckets:
        total = sum(by_bucket_decision[(bucket, dec)] for dec in decisions)
        row = {"mbh_bucket": bucket, "tranches": total}
        for dec in decisions:
            row[dec] = by_bucket_decision.get((bucket, dec), 0)
            row[f"{dec}_pct"] = round(by_bucket_decision.get((bucket, dec), 0) / total, 4) if total else 0.0
        bucket_rows.append(row)

    regime_rows = []
    regimes = sorted({r for r, _ in by_regime_decision})
    for regime in regimes:
        total = sum(by_regime_decision[(regime, dec)] for dec in decisions)
        row = {"regime": regime, "tranches": total}
        for dec in decisions:
            row[dec] = by_regime_decision.get((regime, dec), 0)
        regime_rows.append(row)

    reason_rows = [
        {"mbh_bucket": bucket, "reason": reason, "count": count}
        for (bucket, reason), count in sorted(gate_reasons_by_bucket.items(), key=lambda kv: -kv[1])
    ]

    overlap_days = sorted(set(row["date"] for row in tranches) & set(mbh))
    summary = {
        "start_date": start_date,
        "end_date": end_date,
        "overlap_days": len(overlap_days),
        "tranche_rows": len(tranches),
        "executed_tranches": sum(1 for row in tranches if classify_tranche(row) == "executed"),
        "gated_tranches": sum(1 for row in tranches if classify_tranche(row) == "gated"),
        "low_score_tranches": sum(1 for row in tranches if classify_tranche(row) == "low_score"),
    }
    return bucket_rows, regime_rows, reason_rows, summary


def build_report(summary: dict, bucket_rows: List[dict], reason_rows: List[dict]) -> str:
    lines = [
        "# Gate Attribution (Test 1)",
        "",
        f"- Window: `{summary['start_date']}` -> `{summary['end_date']}` ({summary['overlap_days']} MBH overlap days)",
        f"- Tranche rows: **{summary['tranche_rows']}** | Executed: **{summary['executed_tranches']}** | "
        f"Gated: **{summary['gated_tranches']}** | Low-score: **{summary['low_score_tranches']}**",
        "",
        "## By MBH return bucket",
        "",
    ]
    if bucket_rows:
        cols = [k for k in bucket_rows[0] if not k.endswith("_pct") and k != "tranches"]
        header = "| " + " | ".join(cols) + " |"
        lines.append(header)
        lines.append("|" + "|".join(["---"] * len(cols)) + "|")
        for row in bucket_rows:
            lines.append("| " + " | ".join(str(row.get(c, "")) for c in cols) + " |")
    lines.extend(["", "## Top block reasons by MBH bucket", ""])
    lines.append("| Bucket | Reason | Count |")
    lines.append("|---|---|---:|")
    for row in reason_rows[:25]:
        lines.append(f"| {row['mbh_bucket']} | {row['reason']} | {row['count']} |")
    lines.append("")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Full-sample gate attribution study.")
    parser.add_argument("--tranche-csv", default=str(ROOT / "data" / "phase0_tranche_full" / "tranche_snapshots.csv"))
    parser.add_argument("--candidate-reason-csv", default=str(ROOT / "data" / "phase0_tranche_full" / "candidate_reason_summary.csv"))
    parser.add_argument("--daily-csv", default=str(ROOT / "data" / "phase0_tranche_full" / "daily_regime_validation.csv"))
    parser.add_argument("--start-date", default="2025-02-27")
    parser.add_argument("--end-date", default="2025-12-31")
    parser.add_argument("--output-dir", default=str(ROOT / "data" / "pm_refinement_study"))
    args = parser.parse_args()

    mbh_sheets = [
        ROOT / "data" / "mbh_returns" / "2024.csv",
        ROOT / "data" / "mbh_returns" / "2025.csv",
    ]
    bucket_rows, regime_rows, reason_rows, summary = run_attribution(
        Path(args.tranche_csv),
        Path(args.candidate_reason_csv),
        Path(args.daily_csv),
        mbh_sheets,
        args.start_date,
        args.end_date,
    )
    out = Path(args.output_dir)
    write_csv(out / "gate_attribution_by_mbh_bucket.csv", bucket_rows)
    write_csv(out / "gate_attribution_by_regime.csv", regime_rows)
    write_csv(out / "gate_attribution_reasons.csv", reason_rows)
    report = build_report(summary, bucket_rows, reason_rows)
    (out / "gate_attribution_report.md").write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
