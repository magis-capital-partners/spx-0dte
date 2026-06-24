from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List


MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]


def parse_pct(value: str) -> float:
    value = (value or "").strip()
    if not value or value.upper() == "NA":
        return 0.0
    return float(value.replace("%", "").replace(",", "")) / 100.0


def read_mbh_monthly(path: Path, year: str) -> Dict[str, float]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.reader(handle))
    if not rows:
        return {}
    header = rows[0]
    month_index = {month: header.index(month) for month in MONTHS if month in header}
    for row in rows[1:]:
        if row and row[0].strip() == year:
            return {month: parse_pct(row[index]) for month, index in month_index.items() if index < len(row)}
    return {}


def compound(values: Iterable[float]) -> float:
    total = 1.0
    for value in values:
        total *= 1.0 + value
    return total - 1.0


def read_strategy_monthly(path: Path) -> Dict[str, float]:
    grouped: Dict[str, List[float]] = defaultdict(list)
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            date = row["date"]
            month_number = int(date.split("-")[1])
            month = MONTHS[month_number - 1]
            grouped[month].append(float(row["return_on_equity"]))
    return {month: compound(values) for month, values in grouped.items()}


def write_csv(path: Path, rows: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def annualized_return(total_return: float, days: int) -> float:
    if days <= 0:
        return 0.0
    return (1.0 + total_return) ** (252.0 / days) - 1.0


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare strategy validation returns to MBH monthly net returns.")
    parser.add_argument("--strategy-daily", required=True)
    parser.add_argument("--mbh-net-returns", default=str(Path(__file__).resolve().parents[1] / "data" / "mbh_returns" / "All_Time_Net_Returns.csv"))
    parser.add_argument("--year", default="2025")
    parser.add_argument("--output-csv", required=True)
    parser.add_argument("--output-md", required=True)
    args = parser.parse_args()

    strategy_path = Path(args.strategy_daily)
    mbh_monthly = read_mbh_monthly(Path(args.mbh_net_returns), args.year)
    strategy_monthly = read_strategy_monthly(strategy_path)
    overlap = [month for month in MONTHS if month in strategy_monthly and month in mbh_monthly]

    rows: List[dict] = []
    for month in overlap:
        strategy_return = strategy_monthly[month]
        mbh_return = mbh_monthly[month]
        rows.append(
            {
                "month": month,
                "strategy_return": round(strategy_return, 8),
                "mbh_net_return": round(mbh_return, 8),
                "return_gap": round(strategy_return - mbh_return, 8),
            }
        )

    strategy_total = compound(strategy_monthly[month] for month in overlap)
    mbh_total = compound(mbh_monthly[month] for month in overlap)
    with strategy_path.open(newline="", encoding="utf-8-sig") as handle:
        day_count = sum(1 for _ in csv.DictReader(handle))
    strategy_annualized = annualized_return(strategy_total, day_count)

    write_csv(Path(args.output_csv), rows)
    lines = [
        "# MBH Return Calibration",
        "",
        f"Strategy daily file: `{strategy_path}`",
        f"MBH net return file: `{args.mbh_net_returns}`",
        f"Year: `{args.year}`",
        f"Overlap months: `{', '.join(overlap)}`",
        "",
        f"Strategy compounded overlap return: `{strategy_total:.2%}`",
        f"MBH compounded overlap return: `{mbh_total:.2%}`",
        f"Overlap gap: `{strategy_total - mbh_total:.2%}`",
        f"Strategy annualized over validation day count: `{strategy_annualized:.2%}`",
        "",
        "| Month | Strategy | MBH Net | Gap |",
        "|---|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row['month']} | {row['strategy_return']:.2%} | {row['mbh_net_return']:.2%} | {row['return_gap']:.2%} |"
        )
    Path(args.output_md).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"rows={len(rows)} csv={args.output_csv} md={args.output_md}")


if __name__ == "__main__":
    main()
