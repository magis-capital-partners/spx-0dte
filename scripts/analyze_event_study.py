"""Reproducible calendar-event study for the saved dashboard backtests.

The study deliberately uses only dates that can be determined without looking at
strategy P&L.  Event T+1 is the next observed session in each run, so holidays
and unavailable historical dates never turn into artificial zero-return days.
"""
from __future__ import annotations

import calendar
import csv
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "data" / "dashboard_runs"
OUT = ROOT / "data" / "analysis" / "event_study"
EQUITY = 13_000_000.0


def nth_weekday(year, month, weekday, n):
    d = date(year, month, 1)
    return d + timedelta(days=(weekday - d.weekday()) % 7 + 7 * (n - 1))


def last_weekday(year, month, weekday):
    d = date(year, month, calendar.monthrange(year, month)[1])
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def easter(year):
    a = year % 19; b = year // 100; c = year % 100; d = b // 4; e = b % 4
    f = (b + 8) // 25; g = (b - f + 1) // 3; h = (19*a + b - d - g + 15) % 30
    i = c // 4; k = c % 4; l = (32 + 2*e + 2*i - h - k) % 7; m = (a + 11*h + 22*l) // 451
    return date(year, (h + l - 7*m + 114)//31, (h + l - 7*m + 114)%31 + 1)


def observed(d):
    if d.weekday() == 5: return d - timedelta(days=1)
    if d.weekday() == 6: return d + timedelta(days=1)
    return d


def holiday_dates(year):
    # Full NYSE closures relevant to the studied period; Juneteenth began in 2022.
    x = {observed(date(year, 1, 1)), nth_weekday(year, 1, 0, 3),
         nth_weekday(year, 2, 0, 3), easter(year) - timedelta(days=2),
         last_weekday(year, 5, 0), observed(date(year, 7, 4)),
         nth_weekday(year, 9, 0, 1), nth_weekday(year, 11, 3, 4),
         observed(date(year, 12, 25))}
    if year >= 2022: x.add(observed(date(year, 6, 19)))
    # NYSE was closed for these national days in the sample.
    if year in (2018, 2023): x.add(date(year + 1, 1, 2) if year == 2023 else date(year, 12, 5))
    return x


def event_anchors():
    anchors = {}
    def add(name, d): anchors.setdefault(name, set()).add(pd.Timestamp(d))
    years = range(2019, 2027)
    for y in years:
        for m in range(1, 13):
            add("NFP release", nth_weekday(y, m, 4, 1))
            add("Monthly OPEX", nth_weekday(y, m, 4, 3))
        add("Quarter end", date(y, 3, 31)); add("Quarter end", date(y, 6, 30))
        add("Quarter end", date(y, 9, 30)); add("Quarter end", date(y, 12, 31))
        for d in holiday_dates(y): add("Market-holiday anchor", d)
    # Official FOMC decision days maintained in this repository.
    with (ROOT / "data" / "calendar" / "fomc_days.csv").open() as f:
        for r in csv.DictReader(f): add("FOMC decision", pd.Timestamp(r["date"]))
    jackson = {2019:(8,23), 2020:(8,28), 2021:(8,27), 2022:(8,26),
               2023:(8,25), 2024:(8,23), 2025:(8,22), 2026:(8,28)}
    for y, (m, d) in jackson.items(): add("Jackson Hole (Fri)", date(y,m,d))
    for y, m, d in [(2020,11,3),(2022,11,8),(2024,11,5),(2026,11,3)]: add("US federal election", date(y,m,d))
    for y, m, d in [(2021,1,20),(2025,1,20)]: add("Presidential inauguration", date(y,m,d))
    return anchors


def sessions_for_anchor(index, anchor):
    """Return nearest prior, same/next session, and next session after that."""
    prior = index[index < anchor]
    current_or_next = index[index >= anchor]
    return (prior[-1] if len(prior) else None,
            current_or_next[0] if len(current_or_next) else None,
            current_or_next[1] if len(current_or_next) > 1 else None)


def load_run(path):
    d = pd.read_csv(path, parse_dates=["date"]).sort_values("date").copy()
    d = d[d["date"].notna()].drop_duplicates("date")
    d["return"] = d["net_pnl"] / d.get("equity_open", EQUITY)
    d["halted"] = d["halted"].astype(str).str.lower().eq("true")
    return d


def stats(x, base):
    ret = x["return"]
    n = len(x)
    return {"days":n, "mean_return":ret.mean(), "median_return":ret.median(),
            "delta_vs_all":ret.mean()-base["return"].mean(), "win_rate":(ret>0).mean(),
            "p05_return":ret.quantile(.05), "total_pnl":x.net_pnl.sum(),
            "stop_rate":x.stopped_trades.sum()/x.trades.sum() if x.trades.sum() else np.nan,
            "halt_rate":x.halted.mean(), "worst_return":ret.min(),
            "evidence":"robust" if n>=30 else "directional" if n>=12 else "descriptive"}


def inference(x, base):
    """Welch test vs. mutually exclusive non-event days and post-hoc power."""
    event = x["return"].dropna().to_numpy()
    control = base.loc[~base.date.isin(x.date), "return"].dropna().to_numpy()
    if len(event) < 2 or len(control) < 2:
        return {"welch_p": np.nan, "cohen_d": np.nan, "achieved_power": np.nan}
    test = scipy_stats.ttest_ind(event, control, equal_var=False)
    pooled = np.sqrt(((len(event)-1)*event.var(ddof=1)+(len(control)-1)*control.var(ddof=1))/(len(event)+len(control)-2))
    d = (event.mean()-control.mean())/pooled if pooled else np.nan
    ncp = abs(d) / np.sqrt(1/len(event)+1/len(control)) if np.isfinite(d) else np.nan
    df = len(event)+len(control)-2
    crit = scipy_stats.t.ppf(.975, df)
    power = (scipy_stats.nct.cdf(-crit, df, ncp) + 1-scipy_stats.nct.cdf(crit, df, ncp)) if np.isfinite(ncp) else np.nan
    return {"welch_p": test.pvalue, "cohen_d": d, "achieved_power": power}


def holm(p):
    out = np.full(len(p), np.nan); ok = np.isfinite(p); vals = np.asarray(p)[ok]
    order = np.argsort(vals); previous = 0
    for rank, j in enumerate(order):
        adjusted = min(1, (len(vals)-rank)*vals[j]); previous = max(previous, adjusted)
        out[np.flatnonzero(ok)[j]] = previous
    return out


def add_market_state(d):
    spx = pd.read_csv(ROOT / "data" / "calendar" / "spx_daily.csv", parse_dates=["date"])
    vix = pd.read_csv(ROOT / "data" / "calendar" / "vix_daily.csv", parse_dates=["date"])
    spx["prior_day_spx_return"] = spx.close / spx.prior_close - 1
    spx["overnight_gap"] = spx.open / spx.prior_close - 1
    vix["vix_close"] = vix.close
    return d.merge(spx[["date","prior_day_spx_return","overnight_gap"]], on="date", how="left").merge(vix[["date","vix_close"]], on="date", how="left")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    anchors = event_anchors()
    all_rows=[]; detail=[]
    runs = sorted(RUNS.rglob("daily_summary.csv"))
    for p in runs:
        run=p.parent.name; d=add_market_state(load_run(p)); idx=pd.DatetimeIndex(d.date)
        # Calendar event windows.
        for name, dates in anchors.items():
            for label, pos in [(f"{name}: T-1",0),(f"{name}: T0",1),(f"{name}: T+1",2)]:
                chosen=[]
                for anchor in sorted(dates):
                    s=sessions_for_anchor(idx,anchor)[pos]
                    if s is not None: chosen.append(s)
                x=d[d.date.isin(set(chosen))]
                if len(x):
                    r=stats(x,d); r.update(inference(x,d)); r.update(run=run, category="calendar", event=label); all_rows.append(r)
        # Intrinsic calendar labels and observable pre-entry state (all sample dates).
        labels={
          "First observed session of month": d.date.dt.to_period("M") != d.date.shift().dt.to_period("M"),
          "Last observed session of month": d.date.dt.to_period("M") != d.date.shift(-1).dt.to_period("M"),
          "Monday": d.date.dt.weekday.eq(0), "Friday": d.date.dt.weekday.eq(4),
          "VIX < 15": d.vix_close < 15, "VIX 15–20": d.vix_close.between(15,20, inclusive="left"),
          "VIX 20–25": d.vix_close.between(20,25, inclusive="left"), "VIX ≥ 25": d.vix_close >=25,
          "Prior SPX loss ≤ -1%": d.prior_day_spx_return <= -.01,
          "Prior SPX gain ≥ +1%": d.prior_day_spx_return >= .01,
          "Overnight gap down ≤ -0.5%": d.overnight_gap <= -.005,
          "Overnight gap up ≥ +0.5%": d.overnight_gap >= .005,
        }
        for event, mask in labels.items():
            x=d[mask.fillna(False)]
            if len(x):
                r=stats(x,d); r.update(inference(x,d)); r.update(run=run, category="calendar/state", event=event); all_rows.append(r)
        base=stats(d,d); base.update(welch_p=np.nan, cohen_d=np.nan, achieved_power=np.nan); base.update(run=run, category="baseline", event="Entire available sample"); all_rows.append(base)
        if run=="p3_poststop_cooldown_120": detail.append(d.assign(run=run))
    result=pd.DataFrame(all_rows)
    # Robustness = number of non-baseline runs with same sign as production's excess return.
    prod=result[result.run.eq("p3_poststop_cooldown_120")].set_index("event")
    for event, ix in result.groupby("event").groups.items():
        if event in prod.index:
            sign=np.sign(prod.loc[event,"delta_vs_all"])
            vals=result.loc[ix,"delta_vs_all"].dropna()
            result.loc[ix,"same_direction_runs"]=int((np.sign(vals)==sign).sum())
    result.to_csv(OUT / "event_study_all_runs.csv", index=False)
    primary=result[result.run.eq("p3_poststop_cooldown_120")].copy()
    testable=~primary.category.eq("baseline")
    primary.loc[testable, "holm_p"] = holm(primary.loc[testable, "welch_p"])
    primary["significant_5pct_holm"] = primary.holm_p.le(.05)
    primary.sort_values(["category","event"]).to_csv(OUT / "event_study_production.csv", index=False)
    write_report(primary, result)


def pct(x): return "n/a" if pd.isna(x) else f"{x:.2%}"
def write_report(primary, allruns):
    base=primary[primary.category.eq("baseline")].iloc[0]
    rows=primary[~primary.category.eq("baseline")].copy()
    rows["abs_delta"]=rows.delta_vs_all.abs()
    rows=rows.sort_values("abs_delta", ascending=False)
    lines=["# Calendar & Market-State Event Study", "", "## Bottom line", "",
           f"Production sample: **{int(base.days):,} observed eligible sessions**; mean daily return **{pct(base.mean_return)}**; win rate **{pct(base.win_rate)}**; stop rate **{pct(base.stop_rate)}**.",
           "", "This is descriptive research, not an automatically tradable ruleset. `robust` needs at least 30 sessions, `directional` 12–29, and `descriptive` fewer than 12. Event T0 means the first available saved session on or after the anchor; T+1 is its next available session.",
           "", "## Statistical significance and power", "", "Each row uses a two-sided Welch test against mutually exclusive non-event sessions. Holm p controls the family-wise false-positive rate across all tested categories. Achieved power is post-hoc: it describes whether this sample could detect the observed standardized effect, not prospective validation.", "", "| Category | Welch p | Holm p | Cohen's d | Achieved power | Result |", "|---|---:|---:|---:|---:|---|"]
    tested=primary[(~primary.category.eq("baseline")) & primary.holm_p.notna()].sort_values("holm_p")
    for _,r in tested.iterrows():
        label="significant" if r.significant_5pct_holm else "not significant"
        lines.append(f"| {r.event} | {r.welch_p:.4f} | {r.holm_p:.4f} | {r.cohen_d:.2f} | {r.achieved_power:.0%} | {label} |")
    lines += ["", "Only results with both Holm significance and reasonable power should become rule candidates; even those need selection/holdout confirmation.",
           "", "## Largest calendar/event differences vs. the full production sample", "", "| Event window | Days | Mean return | Difference vs. all days | Win rate | Stop rate | Evidence | Same-direction runs |", "|---|---:|---:|---:|---:|---:|---|---:|"]
    calendar_rows=rows[rows.category.eq("calendar")]
    for _,r in calendar_rows.iterrows(): lines.append(f"| {r.event} | {int(r.days)} | {pct(r.mean_return)} | {pct(r.delta_vs_all)} | {pct(r.win_rate)} | {pct(r.stop_rate)} | {r.evidence} | {int(r.same_direction_runs or 0)}/7 |")
    lines += ["", "## Calendar and pre-entry market-state categories", "", "| Category | Days | Mean return | Difference vs. all days | Win rate | Stop rate | Evidence |", "|---|---:|---:|---:|---:|---:|---|"]
    for _,r in rows[rows.category.eq("calendar/state")].iterrows(): lines.append(f"| {r.event} | {int(r.days)} | {pct(r.mean_return)} | {pct(r.delta_vs_all)} | {pct(r.win_rate)} | {pct(r.stop_rate)} | {r.evidence} |")
    lines += ["", "## Interpretation guardrails", "", "- FOMC, NFP, Jackson Hole, elections, and inauguration are mechanically dated calendar studies. The repository's FOMC calendar is the source for decision dates.", "- The saved run set does not contain a complete historical release calendar for CPI, PPI, PCE, GDP, retail sales, ISM, or Treasury auctions. They are intentionally **not approximated** here; guessing those dates would contaminate the result. Add sourced release timestamps before treating those as results.", "- Market-state groups use only data known before the session's options entries: prior close-to-close SPX move, opening gap, and prior VIX close.", "- Results can overlap (for example an NFP can occur near a holiday), so rows should not be added together or treated as independent tests.", "", "## Files", "", "- `event_study_production.csv`: clean table for the production backtest.", "- `event_study_all_runs.csv`: same calculations for every saved run, for robustness checks."]
    (OUT / "event_study_report.md").write_text("\n".join(lines), encoding="utf-8")

if __name__ == "__main__": main()
