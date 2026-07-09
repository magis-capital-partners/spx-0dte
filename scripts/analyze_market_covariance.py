"""Run market covariance / beta analysis for a dashboard preset vs SPX/IXIC/RUT."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))

from index_daily import (  # noqa: E402
    CALENDAR_DIR,
    close_to_close_returns,
    csv_path_for_symbol,
    load_index_daily,
)
from market_factor_analysis import (  # noqa: E402
    build_return_panel,
    load_daily_summary_csv,
    run_full_analysis,
    strategy_returns_from_daily,
)
from vix_daily import DEFAULT_VIX_CSV, load_vix_daily  # noqa: E402

DEFAULT_PRESET = "p3_poststop_cooldown_120"
DEFAULT_EQUITY = 13_000_000.0


def main() -> None:
    parser = argparse.ArgumentParser(description="Strategy vs SPX/IXIC/RUT covariance and beta report.")
    parser.add_argument("--preset", default=DEFAULT_PRESET)
    parser.add_argument("--equity", type=float, default=DEFAULT_EQUITY)
    parser.add_argument(
        "--daily-summary",
        type=Path,
        default=None,
        help="Override path to daily_summary.csv",
    )
    parser.add_argument("--calendar-dir", type=Path, default=CALENDAR_DIR)
    parser.add_argument("--vix-csv", type=Path, default=DEFAULT_VIX_CSV)
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output JSON path (default: data/analysis/market_covariance_<preset>_<end>.json)",
    )
    args = parser.parse_args()

    summary_path = args.daily_summary or (
        ROOT / "data" / "dashboard_runs" / args.preset / "daily_summary.csv"
    )
    rows = load_daily_summary_csv(summary_path)
    strategy_rets = strategy_returns_from_daily(rows, args.equity)

    index_rets = {}
    for symbol, key in (("^GSPC", "spx"), ("^IXIC", "ixic"), ("^RUT", "rut")):
        path = csv_path_for_symbol(symbol, args.calendar_dir)
        by_date = load_index_daily(path)
        if not by_date:
            raise SystemExit(f"missing index calendar {path} — run scripts/download_index_daily.py first")
        index_rets[key] = close_to_close_returns(by_date)

    vix_map = {d: row.open for d, row in load_vix_daily(args.vix_csv).items()}
    panel = build_return_panel(strategy_rets, index_rets, vix_open_by_date=vix_map)
    report = run_full_analysis(panel)
    report["meta"] = {
        "preset": args.preset,
        "account_equity": args.equity,
        "daily_summary": str(summary_path),
        "calendar_dir": str(args.calendar_dir),
        "vix_csv": str(args.vix_csv),
        "strategy_days_raw": len(strategy_rets),
        "aligned_days": panel.n,
    }

    out = args.out
    if out is None:
        out_dir = ROOT / "data" / "analysis"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"market_covariance_{args.preset}_{report['date_end']}.json"
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    h = report["headline"]
    print(f"=== Market covariance {args.preset} ({report['date_start']} .. {report['date_end']}) ===")
    print(f"Aligned days: {report['n_days']}")
    print(
        f"beta SPX={h['beta_spx']:.3f} (R2={h['r2_spx']:.3f})  "
        f"beta IXIC={h['beta_ixic']:.3f}  beta RUT={h['beta_rut']:.3f}"
    )
    print(
        f"corr SPX={h['corr_spx']:.3f}  corr IXIC={h['corr_ixic']:.3f}  corr RUT={h['corr_rut']:.3f}  "
        f"TE={h['te_ann_vs_spx']:.2%}  IR={h['ir_vs_spx']:.2f}"
    )
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
