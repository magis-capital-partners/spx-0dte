"""Run the path-dependent full-compounding calendar-sizing A/B."""
from pathlib import Path
import run_compounding_sizing_suite as suite

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "analysis" / "calendar_sizing_ab" / "compounding"
suite.OUT = OUT
suite.DASHBOARD_DIR = OUT / "full"
suite.run_suite(shard=0, shards=1, resume=False, variants_filter=["full"], calendar_overlay=True)
