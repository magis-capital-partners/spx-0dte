from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import List


ROOT = Path(__file__).resolve().parents[1]
TRADING_DAYS = 252


def safe_float(value: object, default: float = 0.0) -> float:
    try:
        if value in {"", None}:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def read_rows(path: Path) -> List[dict]:
    if not path.exists() or path.stat().st_size == 0:
        return []
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


def write_markdown(path: Path, rows: List[dict], source: Path) -> None:
    lines = [
        "# Capital Deployment Ladder",
        "",
        f"Source run: `{source}`",
        "",
        "This is a first-order linear scaling estimate. It does not assume fills, halt behavior, or slippage remain unchanged at larger size.",
        "",
        "| Target max margin / equity | Scale | Est. net P&L | Est. annual return | Est. avg daily credit | Est. worst day |",
        "| ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            "| {target_margin_pct:.1f}% | {scale:.2f}x | ${scaled_net_pnl:,.2f} | {annualized_return_pct:.2f}% | ${scaled_avg_daily_credit:,.2f} | ${scaled_worst_day:,.2f} |".format(
                **row
            )
        )
    lines.extend(
        [
            "",
            "Use this as a sizing screen only. Any tier that looks attractive still needs a real rerun with slippage, credit caps, and daily loss halt behavior.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def build_ladder(rows: List[dict], account_equity: float, target_margin_pcts: List[float]) -> List[dict]:
    days = len(rows)
    total_pnl = sum(safe_float(row.get("net_pnl")) for row in rows)
    total_credit = sum(safe_float(row.get("gross_credit_sold")) for row in rows)
    max_margin = max((safe_float(row.get("approx_spread_margin")) for row in rows), default=0.0)
    worst_day = min((safe_float(row.get("net_pnl")) for row in rows), default=0.0)
    avg_daily_credit = total_credit / days if days else 0.0

    ladder = []
    for target_margin_pct in target_margin_pcts:
        target_margin = account_equity * target_margin_pct
        scale = target_margin / max_margin if max_margin else 0.0
        scaled_pnl = total_pnl * scale
        annualized = (scaled_pnl / account_equity) * (TRADING_DAYS / days) if days and account_equity else 0.0
        ladder.append(
            {
                "days": days,
                "target_margin_pct": target_margin_pct * 100.0,
                "target_margin": round(target_margin, 2),
                "scale": scale,
                "scaled_net_pnl": round(scaled_pnl, 2),
                "annualized_return_pct": annualized * 100.0,
                "scaled_avg_daily_credit": round(avg_daily_credit * scale, 2),
                "scaled_worst_day": round(worst_day * scale, 2),
            }
        )
    return ladder


def parse_target_pcts(value: str) -> List[float]:
    return [float(item.strip()) / 100.0 for item in value.split(",") if item.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Estimate P&L and risk across margin deployment tiers.")
    parser.add_argument("--daily", required=True)
    parser.add_argument("--account-equity", type=float, default=13_000_000)
    parser.add_argument("--target-margin-pcts", default="2,5,10,20,30,40")
    parser.add_argument("--output-csv", default=str(ROOT / "data" / "capital_deployment_ladder.csv"))
    parser.add_argument("--output-md", default=str(ROOT / "capital_deployment_ladder.md"))
    args = parser.parse_args()

    daily_path = Path(args.daily)
    rows = read_rows(daily_path)
    ladder = build_ladder(rows, args.account_equity, parse_target_pcts(args.target_margin_pcts))
    write_csv(Path(args.output_csv), ladder)
    write_markdown(Path(args.output_md), ladder, daily_path)
    print(f"tiers={len(ladder)} csv={args.output_csv} md={args.output_md}")


if __name__ == "__main__":
    main()
