"""Part 2: directional dependence + what-if overlays on the existing trade table.

Approximations only (no re-simulation): quantifies how much of the weak-period
loss is explained by intraday upward drift (all-short-call book), morning
entries, Tuesdays, and stop cascades.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

RUN = Path("data/dashboard_runs/linear_decay_downsize")
EQUITY0 = 13_000_000.0

daily = pd.read_csv(RUN / "daily_summary.csv", parse_dates=["date"]).sort_values("date")
trades = pd.read_csv(RUN / "trades.csv", parse_dates=["date", "entry_time"])
trades["year"] = trades["date"].dt.year
trades["weekday"] = trades["date"].dt.day_name().str[:3]
trades["hour"] = trades["entry_time"].dt.hour

print(f"side mix: {trades['side'].value_counts().to_dict()}")

# --- intraday drift proxy: last vs first entry spot each day ---
g = trades.sort_values("entry_time").groupby("date")["entry_spot"]
drift = ((g.last() - g.first()) / g.first()).rename("drift")
d = daily.merge(drift.reset_index(), on="date", how="left")
d["drift_bp"] = d["drift"] * 1e4

print("\n=== DAY P&L vs INTRADAY DRIFT (spot last-entry vs first-entry) ===")
d["drift_bucket"] = pd.cut(d["drift_bp"], [-1e9, -100, -25, 25, 100, 1e9],
                           labels=["big_down", "down", "flat", "up", "big_up"])
print(d.groupby("drift_bucket", observed=True).agg(
    days=("date", "count"), pnl=("net_pnl", "sum"), mean=("net_pnl", "mean"),
    halted=("halted", "sum")).round(0).to_string())
print(f"\ncorr(day pnl, drift): {d['net_pnl'].corr(d['drift']):.3f}")

# weak months: how many were up-drift months?
d["ym"] = d["date"].dt.to_period("M").astype(str)
worst = d.groupby("ym")["net_pnl"].sum().nsmallest(8).index
print("\n=== WORST MONTHS: drift character ===")
for ym in sorted(worst):
    sub = d[d["ym"] == ym]
    print(f"  {ym}: pnl {sub['net_pnl'].sum():>12,.0f}  mean drift {sub['drift_bp'].mean():>6.1f}bp  "
          f"up-days {(sub['drift_bp']>25).mean():.0%}  halted {int(sub['halted'].sum())}/{len(sub)}")

best = d.groupby("ym")["net_pnl"].sum().nlargest(5).index
print("=== BEST MONTHS: drift character ===")
for ym in sorted(best):
    sub = d[d["ym"] == ym]
    print(f"  {ym}: pnl {sub['net_pnl'].sum():>12,.0f}  mean drift {sub['drift_bp'].mean():>6.1f}bp  "
          f"up-days {(sub['drift_bp']>25).mean():.0%}  halted {int(sub['halted'].sum())}/{len(sub)}")

# --- what-if overlays (approximate: removes trades, ignores halt interactions) ---
def scenario(name: str, mask: pd.Series) -> None:
    kept = trades[~mask]
    removed = trades[mask]
    print(f"{name:<46} removed {len(removed):>5} trades  pnl_removed {removed['net_pnl'].sum():>13,.0f}  "
          f"new_total {kept['net_pnl'].sum():>13,.0f}")

base = trades["net_pnl"].sum()
print(f"\n=== WHAT-IF OVERLAYS (baseline total {base:,.0f}) ===")
scenario("skip 9:xx entries", trades["hour"] == 9)
scenario("skip 9:xx + 10:0x-10:3x entries", (trades["hour"] == 9) | ((trades["hour"] == 10) & (trades["entry_time"].dt.minute < 30)))
scenario("skip Tuesdays (daily era)", (trades["weekday"] == "Tue") & (trades["date"] >= "2022-04-18"))
scenario("skip Mon+Tue (daily era)", trades["weekday"].isin(["Mon", "Tue"]) & (trades["date"] >= "2022-04-18"))

# stop-cascade circuit breaker: no new entries after Nth stop of the day
stops = trades[trades["stopped"] == True][["date", "stop_time"]].copy()
stops["stop_time"] = pd.to_datetime(stops["stop_time"])
def nth_stop_time(n: int) -> pd.Series:
    return stops.groupby("date")["stop_time"].apply(
        lambda s: s.sort_values().iloc[n - 1] if len(s) >= n else pd.NaT
    ).rename("cut")

for n in (2, 3, 4):
    t2 = trades.merge(nth_stop_time(n).reset_index(), on="date", how="left")
    mask = t2["cut"].notna() & (t2["entry_time"] > t2["cut"])
    scenario(f"halt new entries after stop #{n}", mask.values)

# trend-aware skip: skip bear_call when entry_trend_score strongly positive
for thr in (0.5, 1.0, 1.5):
    scenario(f"skip bear_call entries w/ trend_score > {thr}", (trades["side"] == "bear_call") & (trades["entry_trend_score"] > thr))

# combined
t2 = trades.merge(nth_stop_time(3).reset_index(), on="date", how="left")
combo = ((trades["hour"] == 9)
         | (t2["cut"].notna() & (t2["entry_time"] > t2["cut"])).values
         | ((trades["side"] == "bear_call") & (trades["entry_trend_score"] > 1.0)))
scenario("combo: no 9am + stop#3 breaker + trend skip", combo)

# --- stopped-trade autopsy: expectancy by entry_trend_score bucket, bear_call ---
bc = trades[trades["side"] == "bear_call"].copy()
bc["trend_bucket"] = pd.cut(bc["entry_trend_score"], [-np.inf, -1, -0.25, 0.25, 1, np.inf],
                            labels=["strong_dn", "dn", "flat", "up", "strong_up"])
print("\n=== BEAR_CALL expectancy by entry trend bucket ===")
print(bc.groupby("trend_bucket", observed=True).agg(n=("net_pnl", "size"), stop_rate=("stopped", "mean"),
                                                    expectancy=("net_pnl", "mean"), pnl=("net_pnl", "sum")).round(2).to_string())

print("\n=== BEAR_CALL expectancy by skew_z bucket ===")
bc["skew_bucket"] = pd.qcut(bc["entry_skew_z"], 5, duplicates="drop")
print(bc.groupby("skew_bucket", observed=True).agg(n=("net_pnl", "size"), stop_rate=("stopped", "mean"),
                                                   expectancy=("net_pnl", "mean")).round(2).to_string())

# halted-day P&L distribution: value of earlier flatten
hd = daily[daily["halted"] == True]
print(f"\nhalted days: {len(hd)}  mean {hd['net_pnl'].mean():,.0f}  median {hd['net_pnl'].median():,.0f}  "
      f"p10 {hd['net_pnl'].quantile(.1):,.0f}  min {hd['net_pnl'].min():,.0f}")
print(f"halted-day pnl as % of |total losses|: {hd['net_pnl'].sum():,.0f}")

# 2026 focus
t26 = trades[trades["year"] == 2026]
print("\n=== 2026 by month/side ===")
print(t26.groupby([t26["date"].dt.to_period("M"), "side"]).agg(n=("net_pnl", "size"), pnl=("net_pnl", "sum"),
                                                               stop_rate=("stopped", "mean"),
                                                               avg_credit=("entry_credit", "mean")).round(2).to_string())
