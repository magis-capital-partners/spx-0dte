"""Run an exact production-profile calendar-sizing A/B backtest."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))

from export_dashboard_run import PRESETS, export_historical_3d_variant  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--preset", default="p3_poststop_cooldown_120")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--signals-filename", default="signals_unconditional_calendar_ab.csv")
    args = p.parse_args()
    spec = {**PRESETS[args.preset], "calendar_overlay": {"opex_multiplier": 2.0, "month_end_multiplier": 0.5}}
    if spec["kind"] != "historical_3d":
        raise SystemExit("This runner handles non-compounding historical_3d presets only.")
    result = export_historical_3d_variant(
        f"{args.preset}_calendar_ab", spec, Path(args.out_dir), ROOT / "data" / "processed",
        "SPXW", args.signals_filename, 40,
    )
    print(result, flush=True)


if __name__ == "__main__": main()
