"""One-shot diagnostic: where and why the 2019-2026 3D backtest underperforms.

Reads the dashboard run outputs (daily_summary.csv, trades.csv, stop_diagnostics.csv)
and prints period-level attribution. No simulation, read-only.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

RUN = Path("data/dashboard_runs/linear_decay_downsize")
EQUITY0 = 13_000_000.0

daily = pd.read_csv(RUN / "daily_summary.csv", parse_dates=["date"])
trades = pd.read_csv(RUN / "trades.csv", parse_dates=["date", "entry_time"])

daily = daily.sort_values("date").reset_index(drop=True)
daily["equity"] = EQUITY0 + daily["net_pnl"].cumsum()
daily["ret"] = daily["net_pnl"] / daily["equity"].shift(1).fillna(EQUITY0)
daily["peak"] = daily["equity"].cummax()
daily["dd"] = daily["equity"] / daily["peak"] - 1
daily["year"] = daily["date"].dt.year
daily["ym"] = daily["date"].dt.to_period("M").astype(str)

print("=== COVERAGE ===")
print(daily.groupby("year").agg(days=("date", "count"), first=("date", "min"), last=("date", "max")).to_string())

print("\n=== YEARLY (eligible-day path, simple sum & per-day stats) ===")
g = daily.groupby("year")
yearly = pd.DataFrame({
    "days": g.size(),
    "net_pnl": g["net_pnl"].sum().round(0),
    "mean_day": g["net_pnl"].mean().round(0),
    "win_rate": g.apply(lambda x: (x["net_pnl"] > 0).mean()).round(3),
    "stop_rate": (g["stopped_trades"].sum() / g["trades"].sum()).round(3),
    "halted_days": g["halted"].sum(),
    "worst_day": g["net_pnl"].min().round(0),
    "ret_pct": (g["ret"].sum() * 100).round(1),
    "sharpe": (g["ret"].mean() / g["ret"].std() * np.sqrt(252)).round(2),
})
print(yearly.to_string())

print("\n=== WORST 15 MONTHS ===")
m = daily.groupby("ym").agg(days=("date", "count"), pnl=("net_pnl", "sum"),
                            stops=("stopped_trades", "sum"), trades=("trades", "sum"),
                            halted=("halted", "sum"))
m["stop_rate"] = (m["stops"] / m["trades"]).round(3)
print(m.sort_values("pnl").head(15).to_string())

print("\n=== BEST 10 MONTHS ===")
print(m.sort_values("pnl", ascending=False).head(10).to_string())

print("\n=== DRAWDOWN EPISODES (dd < -5%) ===")
in_dd = daily["dd"] < -0.001
episodes = []
start = None
for i, row in daily.iterrows():
    if row["dd"] < -1e-9 and start is None:
        start = i
    if row["dd"] >= -1e-9 and start is not None:
        seg = daily.loc[start:i]
        episodes.append((seg.loc[seg["dd"].idxmin()], seg["date"].iloc[0], seg["date"].iloc[-1], seg["dd"].min()))
        start = None
if start is not None:
    seg = daily.loc[start:]
    episodes.append((seg.loc[seg["dd"].idxmin()], seg["date"].iloc[0], seg["date"].iloc[-1], seg["dd"].min()))
for trough, s, e, depth in episodes:
    if depth < -0.05:
        print(f"  {s.date()} -> {e.date()}  depth {depth*100:6.2f}%  trough {trough['date'].date()}")

print("\n=== WORST 15 DAYS ===")
cols = ["date", "weekday", "era", "trades", "stopped_trades", "net_pnl", "halted"]
print(daily.nsmallest(15, "net_pnl")[cols].to_string(index=False))

print("\n=== HALTED / FLATTENED DAYS BY YEAR ===")
print(daily[daily["halted"]].groupby("year").agg(n=("date", "count"), pnl=("net_pnl", "sum")).to_string())

# --- trade-level ---
trades["hour"] = trades["entry_time"].dt.hour
trades["year"] = trades["date"].dt.year

print("\n=== TRADE P&L BY SIDE / YEAR ===")
side_year = trades.pivot_table(index="year", columns="side", values="net_pnl", aggfunc=["sum", "count"])
print(side_year.round(0).to_string())

print("\n=== STOP RATE BY SIDE / YEAR ===")
sr = trades.groupby(["year", "side"]).agg(n=("net_pnl", "size"), stop_rate=("stopped", "mean"),
                                          pnl=("net_pnl", "sum"), avg_credit=("entry_credit", "mean"),
                                          avg_dist=("distance_pct", "mean"))
print(sr.round(3).to_string())

print("\n=== ENTRY-HOUR ATTRIBUTION (all years) ===")
h = trades.groupby("hour").agg(n=("net_pnl", "size"), pnl=("net_pnl", "sum"),
                               stop_rate=("stopped", "mean"), expectancy=("net_pnl", "mean"))
print(h.round(2).to_string())

print("\n=== ENTRY-HOUR x YEAR expectancy ===")
he = trades.pivot_table(index="hour", columns="year", values="net_pnl", aggfunc="mean")
print(he.round(0).to_string())

print("\n=== WEEKDAY ATTRIBUTION BY ERA ===")
w = daily.groupby(["era", "weekday"]).agg(days=("date", "count"), pnl=("net_pnl", "sum"),
                                          mean=("net_pnl", "mean"))
print(w.round(0).to_string())

# loss concentration
losses = trades[trades["net_pnl"] < 0]
total_loss = losses["net_pnl"].sum()
stopped_loss = trades.loc[trades["stopped"] == True, "net_pnl"].sum()
print(f"\nTotal gross loss from losing trades: {total_loss:,.0f}")
print(f"Net P&L of stopped trades: {stopped_loss:,.0f}  ({len(trades[trades['stopped']==True])} trades)")
print(f"Net P&L of non-stopped trades: {trades.loc[trades['stopped']!=True,'net_pnl'].sum():,.0f}")

print("\n=== STOPS: exit_reason breakdown ===")
print(trades.groupby("exit_reason").agg(n=("net_pnl", "size"), pnl=("net_pnl", "sum"), avg=("net_pnl", "mean")).round(0).to_string())

# same-day stop cascades
day_stop = daily[daily["stopped_trades"] >= 5]
print(f"\nDays with >=5 stops: {len(day_stop)}, total pnl {day_stop['net_pnl'].sum():,.0f}")
day_stop2 = daily[daily["stopped_trades"] >= 10]
print(f"Days with >=10 stops: {len(day_stop2)}, total pnl {day_stop2['net_pnl'].sum():,.0f}")

# volatility proxy: use daily contracts and credit; approximate regime via rolling realized move of entry_spot
spot = trades.groupby("date")["entry_spot"].first().sort_index()
spot_ret = spot.pct_change().abs()
daily = daily.merge(spot_ret.rename("gap_move").reset_index(), on="date", how="left")
daily["gap_bucket"] = pd.qcut(daily["gap_move"], 4, labels=["q1_calm", "q2", "q3", "q4_vol"])
print("\n=== P&L BY GAP-MOVE QUARTILE (open-to-open |move|) ===")
print(daily.groupby("gap_bucket", observed=True).agg(days=("date", "count"), pnl=("net_pnl", "sum"), mean=("net_pnl", "mean"), stop_rate_day=("stopped_trades", "mean")).round(0).to_string())

# save monthly for canvas
out = {
    "monthly": m.reset_index().to_dict(orient="records"),
    "yearly": yearly.reset_index().to_dict(orient="records"),
}
Path("data/dashboard_runs/linear_decay_downsize/weak_period_analysis.json").write_text(json.dumps(out, indent=2, default=str))
print("\nSaved weak_period_analysis.json")
