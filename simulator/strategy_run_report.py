from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Iterable, List


ROOT = Path(__file__).resolve().parents[1]
TRADING_DAYS = 252


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


def read_csv(path: Path) -> List[dict]:
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


def write_markdown(path: Path, rows: List[dict], account_equity: float) -> None:
    lines = [
        "# Strategy Run Comparison",
        "",
        f"Account equity: ${account_equity:,.0f}",
        "",
        "| Run | Days | Trades | Stops | Stop rate | Net P&L | Annualized return | Avg daily credit | Max margin | Max margin / equity |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            "| {run} | {days} | {trades} | {stopped_trades} | {stop_rate_pct:.1f}% | ${net_pnl:,.2f} | {annualized_return_pct:.2f}% | ${avg_daily_credit:,.2f} | ${max_margin:,.2f} | {max_margin_pct_equity:.2f}% |".format(
                **row
            )
        )
    lines.extend(
        [
            "",
            "## MBH-Style Deployment Reference",
            "",
            f"- 30% annual return target on this equity: ${account_equity * 0.30:,.0f}, or about ${account_equity * 0.30 / TRADING_DAYS:,.0f} per trading day.",
            f"- 40% annual return target on this equity: ${account_equity * 0.40:,.0f}, or about ${account_equity * 0.40 / TRADING_DAYS:,.0f} per trading day.",
            f"- 1.5% daily gross premium reference: ${account_equity * 0.015:,.0f} per day.",
            f"- 40% average margin reference: ${account_equity * 0.40:,.0f}.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def summarize_run(results_dir: Path, account_equity: float) -> dict:
    rows = read_csv(results_dir / "daily_regime_validation.csv")
    days = len(rows)
    trades = sum(safe_int(row.get("trades")) for row in rows)
    stopped = sum(safe_int(row.get("stopped_trades")) for row in rows)
    net_pnl = sum(safe_float(row.get("net_pnl")) for row in rows)
    gross_credit = sum(safe_float(row.get("gross_credit_sold")) for row in rows)
    max_margin = max((safe_float(row.get("approx_spread_margin")) for row in rows), default=0.0)
    annualized_return = (net_pnl / account_equity) * (TRADING_DAYS / days) if days and account_equity else 0.0
    avg_daily_credit = gross_credit / days if days else 0.0
    return {
        "run": results_dir.name,
        "results_dir": str(results_dir),
        "days": days,
        "trades": trades,
        "stopped_trades": stopped,
        "stop_rate": stopped / trades if trades else 0.0,
        "stop_rate_pct": (stopped / trades * 100.0) if trades else 0.0,
        "net_pnl": round(net_pnl, 2),
        "annualized_return": annualized_return,
        "annualized_return_pct": annualized_return * 100.0,
        "gross_credit_sold": round(gross_credit, 2),
        "avg_daily_credit": avg_daily_credit,
        "avg_daily_credit_pct_equity": avg_daily_credit / account_equity if account_equity else 0.0,
        "max_margin": max_margin,
        "max_margin_pct_equity": (max_margin / account_equity * 100.0) if account_equity else 0.0,
        "credit_multiple_to_1p5pct": (account_equity * 0.015 / avg_daily_credit) if avg_daily_credit else 0.0,
        "margin_multiple_to_40pct": (account_equity * 0.40 / max_margin) if max_margin else 0.0,
    }


def parse_dirs(values: Iterable[str]) -> List[Path]:
    return [Path(value) for value in values]


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a compact comparison report for strategy validation runs.")
    parser.add_argument("--results-dirs", nargs="+", required=True)
    parser.add_argument("--account-equity", type=float, default=13_000_000)
    parser.add_argument("--output-csv", default=str(ROOT / "data" / "strategy_run_comparison.csv"))
    parser.add_argument("--output-md", default=str(ROOT / "strategy_run_comparison.md"))
    args = parser.parse_args()

    rows = [summarize_run(path, args.account_equity) for path in parse_dirs(args.results_dirs)]
    write_csv(Path(args.output_csv), rows)
    write_markdown(Path(args.output_md), rows, args.account_equity)
    print(f"runs={len(rows)} csv={args.output_csv} md={args.output_md}")


if __name__ == "__main__":
    main()
