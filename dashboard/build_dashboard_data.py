"""Build the SPX 0DTE dashboard data blob from backtest runs + MBH benchmark + live fills.

Outputs dashboard/data/dashboard_data.json, consumed by the static index.html
(React SPA, served via GitHub Pages). Mirrors the etf-dashboard pattern: a
Python builder writes one JSON, the front-end renders it with no server.

Usage:
  python dashboard/build_dashboard_data.py \
    --run best_2p5x=data/exp5_2p5x:"2.5x + flatten" \
    --run flatten=data/exp1_flatten:"1x + flatten" \
    --run baseline=data/baseline_repro:"Baseline (prior best)"
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))
from summarize_run import summarize  # noqa: E402

MONTHS = ["January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]


def safe_float(value: object, default: float = 0.0) -> float:
    try:
        if value in {"", None}:
            return default
        return float(str(value).replace("%", "").replace(",", ""))
    except (TypeError, ValueError):
        return default


def read_rows(path: Path) -> List[dict]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def parse_mbh_benchmark(path: Path) -> Dict[str, float]:
    """Return {YYYY-MM: net_return_fraction} from All_Time_Net_Returns.csv."""
    monthly: Dict[str, float] = {}
    if not path.exists():
        return monthly
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        for row in reader:
            if not row:
                continue
            year_str = row[0].strip().strip('"')
            if not year_str.isdigit():
                continue
            year = int(year_str)
            for i, month in enumerate(MONTHS, start=1):
                if i < len(row):
                    cell = row[i].strip().strip('"')
                    if cell and cell != "NA":
                        monthly[f"{year:04d}-{i:02d}"] = round(safe_float(cell) / 100.0, 6)
    return monthly


def build_run(run_id: str, results_dir: Path, label: str, account_equity: float) -> Optional[dict]:
    daily_path = results_dir / "daily_regime_validation.csv"
    if not daily_path.exists():
        print(f"  skip {run_id}: no daily file at {daily_path}")
        return None
    rows = read_rows(daily_path)
    summary = summarize(results_dir, account_equity, compound=True)

    daily: List[dict] = []
    cum = 0.0
    equity = account_equity
    for row in rows:
        net = safe_float(row.get("net_pnl"))
        cum += net
        equity += net
        daily.append({
            "date": row.get("date"),
            "net_pnl": round(net, 2),
            "cum_pnl": round(cum, 2),
            "equity": round(equity, 2),
            "return_pct": round(net / account_equity * 100.0, 4),
            "trades": int(safe_float(row.get("trades"))),
            "stopped": int(safe_float(row.get("stopped_trades"))),
            "halted": str(row.get("halted")) == "True",
            "regime": row.get("regime", ""),
            "event_bucket": row.get("event_bucket", ""),
            "gross_credit_sold": round(safe_float(row.get("gross_credit_sold")), 2),
            "approx_spread_margin": round(safe_float(row.get("approx_spread_margin")), 2),
        })

    trades_by_date: Dict[str, List[dict]] = {}
    for t in read_rows(results_dir / "trades.csv"):
        d = t.get("date")
        trades_by_date.setdefault(d, []).append({
            "entry_time": t.get("entry_time"),
            "side": t.get("side"),
            "model": t.get("model"),
            "contracts": int(safe_float(t.get("contracts"))),
            "short": t.get("short"),
            "long": t.get("long"),
            "entry_credit": safe_float(t.get("entry_credit")),
            "score": safe_float(t.get("candidate_score")),
            "stopped": str(t.get("stopped")) == "True",
            "exit_reason": t.get("exit_reason"),
            "net_pnl": round(safe_float(t.get("net_pnl")), 2),
            "short_delta": safe_float(t.get("short_delta")),
        })

    return {
        "id": run_id,
        "label": label,
        "summary": summary,
        "daily": daily,
        "trades_by_date": trades_by_date,
    }


def build_live(live_dir: Path, account_equity: float) -> dict:
    """Read live fills logged by live/ib_executor.py into a comparable shape."""
    days: Dict[str, dict] = {}
    if not live_dir.exists():
        return {"days": {}}
    for day_path in sorted(live_dir.iterdir()):
        fills_file = day_path / "fills.jsonl"
        if not fills_file.exists():
            continue
        d = day_path.name
        entries = []
        for line in fills_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        trades = [e for e in entries if e.get("event") == "entry"]
        days[d] = {
            "date": d,
            "entries": trades,
            "flattened": any(e.get("event") == "daily_loss_flatten" for e in entries),
            "gross_credit_sold": round(sum(e.get("credit", 0) * e.get("contracts", 0) * 100 for e in trades), 2),
        }
    return {"days": days}


def main() -> None:
    parser = argparse.ArgumentParser(description="Build SPX 0DTE dashboard data blob.")
    parser.add_argument("--run", action="append", default=[],
                        help="run spec id=results_dir:label (repeatable).")
    parser.add_argument("--account-equity", type=float, default=13_000_000)
    parser.add_argument("--mbh-returns", default=str(ROOT / "data" / "mbh_returns" / "All_Time_Net_Returns.csv"))
    parser.add_argument("--live-dir", default=str(ROOT / "data" / "live"))
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent / "data" / "dashboard_data.json"))
    args = parser.parse_args()

    runs = []
    for spec in args.run:
        id_part, rest = spec.split("=", 1)
        dir_part, _, label = rest.partition(":")
        results_dir = (ROOT / dir_part).resolve() if not Path(dir_part).is_absolute() else Path(dir_part)
        run = build_run(id_part, results_dir, label or id_part, args.account_equity)
        if run:
            runs.append(run)
            print(f"  added run {id_part}: CAGR {run['summary'].get('cagr_pct')}% "
                  f"Sharpe {run['summary'].get('sharpe')} ({len(run['daily'])} days)")

    blob = {
        "generated_at": datetime.now().isoformat(),
        "account_equity": args.account_equity,
        "runs": runs,
        "mbh_benchmark": {"monthly": parse_mbh_benchmark(Path(args.mbh_returns))},
        "live": build_live(Path(args.live_dir), args.account_equity),
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(blob, separators=(",", ":")), encoding="utf-8")
    size_kb = out_path.stat().st_size / 1024
    print(f"wrote {out_path} ({size_kb:.0f} KB, {len(runs)} runs)")


if __name__ == "__main__":
    main()
