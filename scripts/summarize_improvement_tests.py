"""Print formatted summary from improvement_plan_tests/summary.json."""
import json
from pathlib import Path

s = json.loads(Path("data/improvement_plan_tests/summary.json").read_text())
base = next(x for x in s if x["variant"] == "baseline")
years = sorted(base["yearly_pnl"].keys())
n_years = len(years)

print(f"Years in sample: {years} (n={n_years})")
print()
print(f"{'Variant':32s} {'Net PnL':>12s} {'vs base':>10s} {'CAGR':>6s} {'Sharpe':>6s} {'MaxDD':>6s} {'Halt':>5s} {'BC%':>5s} {'drift':>6s} {'Yrs+':>5s}")
print("-" * 110)
for v in sorted(s, key=lambda x: -x["net_pnl"]):
    d = v["net_pnl"] - base["net_pnl"]
    print(
        f"{v['variant']:32s} {v['net_pnl']:12,.0f} {d:+10,.0f} "
        f"{v['cagr_pct']:5.1f}% {v['sharpe']:6.2f} {v['max_drawdown_pct']:5.1f}% "
        f"{v['halted_days']:5d} {v['bear_call_pct']:5.1f} {v['drift_corr']:6.3f} "
        f"{v['years_beat_baseline']}/{n_years}"
    )

print("\n=== Yearly PnL ($k) — top variants ===")
top = [
    "baseline",
    "p3_trend1_skew075",
    "p3_combo",
    "p3_skew_gate_0.75",
    "p2_max_sides_2",
    "p2_portfolio_allocator",
    "p1_flatten_2.0pct",
]
header = f"{'Year':6s}" + "".join(f"{n:>14s}" for n in top)
print(header)
for y in years:
    row = f"{y:6s}"
    b = base["yearly_pnl"][y]
    for name in top:
        pnl = next(x for x in s if x["variant"] == name)["yearly_pnl"][y]
        mark = "*" if pnl > b else " "
        row += f"{pnl/1000:>+13.0f}k{mark}"
    print(row)

passing = [v for v in s if v["years_beat_baseline"] >= 3 and v["variant"] != "baseline"]
print(f"\nVariants beating baseline in >=3/{n_years} years: {len(passing)}")
for v in sorted(passing, key=lambda x: -x["net_pnl"]):
    print(f"  {v['variant']}: {v['years_beat_baseline']}/{n_years} years, net ${v['net_pnl']:,.0f}, CAGR {v['cagr_pct']}%")
