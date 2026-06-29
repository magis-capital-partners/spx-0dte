"""Dump every 15-min tranche for one day and explain why the clone did or did not deploy.

Cross-references MBH's actual daily return so we can see, tranche-by-tranche, why
MBH harvested premium all day while our gate/model sat mostly flat.

Usage:
  python simulator/tranche_day_diagnostic.py --date 2025-04-24
  python simulator/tranche_day_diagnostic.py --date 2025-04-24 --profile baseline
  python simulator/tranche_day_diagnostic.py --pick-date --recon-daily simulator/_tmp_robustness_summary/daily_regime_validation.csv
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from statistics import mean
from typing import Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "simulator"))

from live.strategy_profiles import PROFILES  # noqa: E402
from mbh_simulator import (  # noqa: E402
    StrategyConfig,
    candidate_records_to_rows,
    read_quotes_csv,
    read_signals_csv,
    simulate_day,
    tranche_summaries_to_rows,
    trades_to_rows,
)
from regime_validation import (  # noqa: E402
    apply_rolling_baseline,
    classify_regime,
    discover_dates,
    read_event_calendar,
)

DEFAULT_PROCESSED = ROOT / "data" / "processed"
DEFAULT_EVENT_CALENDAR = ROOT / "regime_expansion_dates_2025.csv"
DEFAULT_OUT = ROOT / "data" / "tranche_day_diagnostics"
SIGNALS_FILENAME = "signals_regime_validation.csv"
MBH_DAILY_RETURN_COL = 2


def parse_pct(value: str) -> Optional[float]:
    value = (value or "").strip().strip('"')
    if not value or value.upper() == "NA":
        return None
    try:
        return float(value.replace("%", "").replace(",", "")) / 100.0
    except ValueError:
        return None


def parse_sheet_date(value: str) -> Optional[date]:
    value = (value or "").strip().strip('"')
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def read_mbh_daily(path: Path, year: Optional[int] = None) -> Dict[date, float]:
    out: Dict[date, float] = {}
    if not path.exists():
        return out
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.reader(handle):
            if len(row) <= MBH_DAILY_RETURN_COL:
                continue
            d = parse_sheet_date(row[0])
            if d is None:
                continue
            if year is not None and d.year != year:
                continue
            ret = parse_pct(row[MBH_DAILY_RETURN_COL])
            if ret is not None:
                out[d] = ret
    return out


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


def best_score(row: dict) -> float:
    values = [
        safe_float(row.get("best_pass_score"), default=-1.0),
        safe_float(row.get("best_overall_score"), default=-1.0),
        safe_float(row.get("top_pass_bull_score"), default=-1.0),
        safe_float(row.get("top_pass_bear_score"), default=-1.0),
    ]
    return max(values)


def build_config(profile_name: str, account_equity: float) -> StrategyConfig:
    profile = PROFILES[profile_name]
    return StrategyConfig(
        account_equity=account_equity,
        baseline_contracts=profile["baseline_contracts"],
        daily_credit_cap_pct=profile["daily_credit_cap_pct"],
        daily_loss_limit_pct=profile["daily_loss_limit_pct"],
        flatten_on_daily_loss=profile.get("flatten_on_daily_loss", False),
        flatten_loss_limit_pct=profile.get("flatten_loss_limit_pct", 0.0),
        use_two_tier_engine=profile.get("use_two_tier_engine", True),
        use_event_controls=profile.get("use_event_controls", True),
        use_time_of_day_controls=profile.get("use_time_of_day_controls", True),
        exploratory_min_score=profile.get("exploratory_min_score", 2.40),
        exploratory_max_score=profile.get("exploratory_max_score", 2.49),
        use_portfolio_allocator=profile.get("use_portfolio_allocator", True),
        portfolio_margin_budget_pct=profile.get("portfolio_margin_budget_pct", 0.40),
        core_margin_budget_pct=profile.get("core_margin_budget_pct", 0.35),
        exploratory_margin_budget_pct=profile.get("exploratory_margin_budget_pct", 0.02),
        candidate_min_score=2.50,
        record_tranche_summaries=True,
    )


def classify_tranche(row: dict, min_score: float) -> Tuple[str, str]:
    skip = row.get("skip_reason", "")
    if skip == "halted":
        return "halted", "Daily loss or credit cap halted new entries"
    if skip == "no_signal":
        return "no_signal", "No signal snapshot at tranche time"
    if skip == "zero_policy_contracts":
        return "zero_policy", "Signal policy returned zero contracts (danger/regime sizing)"

    executed = safe_int(row.get("candidates_executed"))
    if executed > 0:
        return "executed", f"Opened {executed} candidate(s): {row.get('selected_summary', '')}"

    pass_count = safe_int(row.get("candidates_pass"))
    gated = safe_int(row.get("candidates_gated"))
    rejected = safe_int(row.get("candidates_rejected"))
    score = best_score(row)
    top_reject = row.get("top_reject_reason", "")

    if pass_count > 0:
        detail = row.get("selected_summary") or top_reject or "risk/allocator block"
        return "passed_not_executed", f"{pass_count} pass candidate(s) blocked: {detail}"

    if gated > 0 and pass_count == 0:
        if score >= min_score:
            return "gated", f"Score {score:.3f} cleared gate but {gated} candidate(s) blocked by side/regime gates"
        return "gated", f"{gated} candidate(s) blocked by side/regime gates (best score {score:.3f})"

    if top_reject == "low_score" or (0 <= score < min_score and score >= 0):
        return "low_score", f"Best score {score:.3f} below gate {min_score:.2f}"

    if gated > 0:
        return "gated_or_low_score", f"Gated {gated}, rejected {rejected}, best {score:.3f}"

    return "no_trade", f"No pass candidates (best {score:.3f}, rejected {rejected})"


def write_csv(path: Path, rows: Sequence[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def pick_representative_date(recon_daily: Path, mbh_sheet: Path) -> str:
    """Pick a day with high MBH return and zero recon trades."""
    recon_by_date: Dict[str, dict] = {}
    with recon_daily.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            recon_by_date[row["date"]] = row

    mbh = read_mbh_daily(mbh_sheet)
    candidates: List[Tuple[float, str]] = []
    for d, mbh_ret in mbh.items():
        iso = d.isoformat()
        recon = recon_by_date.get(iso)
        if recon is None:
            continue
        if safe_int(recon.get("trades")) == 0 and mbh_ret > 0.005:
            candidates.append((mbh_ret, iso))
    if not candidates:
        raise SystemExit("No overlap day found with MBH > 0.5% and recon trades == 0")
    candidates.sort(reverse=True)
    return candidates[0][1]


def simulate_one_day(
    test_date: str,
    processed_dir: Path,
    symbol: str,
    config: StrategyConfig,
    train_count: int,
    event_calendar: dict,
) -> Tuple[dict, List[dict], List[dict], List[dict]]:
    dates = discover_dates(processed_dir, symbol)
    if test_date not in dates:
        raise SystemExit(f"Date {test_date} not in processed dates")
    index = dates.index(test_date)
    if index < train_count:
        raise SystemExit(f"Need at least {train_count} prior dates for rolling baseline; {test_date} is too early")

    train_dates = dates[index - train_count : index]
    apply_rolling_baseline(processed_dir, symbol, train_dates, test_date, SIGNALS_FILENAME)

    day_dir = processed_dir / f"symbol={symbol}" / f"date={test_date}"
    quotes = read_quotes_csv(day_dir / "normalized_option_quotes.csv")
    signals = read_signals_csv(day_dir / SIGNALS_FILENAME)
    regime, regime_metrics = classify_regime(signals)
    event_info = event_calendar.get(test_date, {"event_bucket": "unlabeled", "event_note": ""})
    day_config = replace(config, event_bucket=event_info["event_bucket"])
    result = simulate_day(quotes, signals, config=day_config)

    daily = {
        "date": test_date,
        "regime": regime,
        "event_bucket": event_info["event_bucket"],
        "trades": len(result.trades),
        "net_pnl": round(result.net_pnl, 2),
        "return_on_equity": round(result.return_on_equity, 8),
        "gross_credit_sold": round(result.gross_credit_sold, 2),
        "halted": result.halted,
        **{key: round(value, 6) for key, value in regime_metrics.items()},
    }

    tranche_rows = tranche_summaries_to_rows(result.tranche_summaries)
    for row in tranche_rows:
        row["date"] = test_date

    candidate_rows = candidate_records_to_rows(result.candidate_records)
    for row in candidate_rows:
        row["date"] = test_date

    trade_rows = trades_to_rows(result.trades)
    for row in trade_rows:
        row["date"] = test_date

    return daily, tranche_rows, candidate_rows, trade_rows


def enrich_tranches(tranche_rows: Sequence[dict], min_score: float) -> List[dict]:
    enriched: List[dict] = []
    for row in tranche_rows:
        decision, detail = classify_tranche(row, min_score)
        enriched.append(
            {
                **row,
                "best_score": round(best_score(row), 4) if best_score(row) >= 0 else "",
                "decision": decision,
                "decision_detail": detail,
                "mbh_would_deploy": "likely_yes" if decision != "executed" else "matched",
            }
        )
    return enriched


def build_report(
    test_date: str,
    daily: dict,
    tranches: Sequence[dict],
    candidates: Sequence[dict],
    trades: Sequence[dict],
    mbh_return: Optional[float],
    profile_name: str,
    min_score: float,
) -> str:
    decisions = Counter(row["decision"] for row in tranches)
    scores = [best_score(row) for row in tranches if best_score(row) >= 0]
    executed = [row for row in tranches if row["decision"] == "executed"]
    near_miss = sorted(
        [row for row in tranches if row["decision"] in {"low_score", "gated", "gated_or_low_score", "no_trade"}],
        key=lambda row: best_score(row),
        reverse=True,
    )[:8]

    recon_ret = safe_float(daily.get("return_on_equity"))
    mbh_str = f"{mbh_return * 100:.2f}%" if mbh_return is not None else "n/a"
    recon_str = f"{recon_ret * 100:.2f}%"

    lines = [
        f"# Tranche Day Diagnostic — {test_date}",
        "",
        f"- Profile: `{profile_name}` (candidate_min_score={min_score:.2f})",
        f"- Regime: `{daily.get('regime')}` | Event: `{daily.get('event_bucket')}`",
        f"- **MBH daily return:** {mbh_str} | **Reconstruction:** {recon_str} "
        f"({daily.get('trades')} trades, credit ${safe_float(daily.get('gross_credit_sold')):,.0f})",
        "",
        "## Headline",
        "",
    ]

    if mbh_return and mbh_return > 0 and safe_int(daily.get("trades")) == 0:
        lines.append(
            f"- MBH earned **{mbh_str}** this day; the clone earned **{recon_str}** with "
            f"**{len(executed)}/{len(tranches)}** tranches executed."
        )
    elif executed:
        lines.append(f"- Clone executed on **{len(executed)}** of **{len(tranches)}** tranches.")
    else:
        lines.append(f"- Clone executed on **0** of **{len(tranches)}** tranches.")

    low_score_count = decisions.get("low_score", 0) + decisions.get("gated_or_low_score", 0)
    lines.append(
        f"- Decision mix: {dict(decisions)}. "
        f"**{low_score_count}** tranches blocked primarily by score/gates; "
        f"mean best score **{mean(scores):.3f}** (max **{max(scores):.3f}**)."
    )
    lines.append(
        f"- Only **{sum(1 for s in scores if s >= min_score)}** tranches reached score ≥ {min_score:.2f} "
        f"({sum(1 for s in scores if s >= min_score) / len(scores):.1%} of tranches)."
        if scores
        else "- No scored tranches."
    )
    lines.append("")

    lines.extend(["## Every 15-min tranche", "", "| Time | Decision | Best | Pass | Gated | Rej | Exec | Detail |", "|---|---|---:|---:|---:|---:|---:|---|"])
    for row in tranches:
        ts = row.get("timestamp", "")[11:16]
        lines.append(
            f"| {ts} | {row['decision']} | {row.get('best_score', '')} | "
            f"{row.get('candidates_pass', 0)} | {row.get('candidates_gated', 0)} | "
            f"{row.get('candidates_rejected', 0)} | {row.get('candidates_executed', 0)} | "
            f"{row.get('decision_detail', '')[:80]} |"
        )
    lines.append("")

    if near_miss:
        lines.extend(["## Highest-scoring tranches that did NOT execute", ""])
        for row in near_miss:
            ts = row.get("timestamp", "")[11:16]
            lines.append(
                f"- **{ts}** score **{row.get('best_score')}** ({row['decision']}): {row.get('decision_detail')}"
            )
        lines.append("")

    if trades:
        lines.extend(["## Trades opened", ""])
        for trade in trades:
            lines.append(
                f"- {trade.get('entry_time')} {trade.get('side')} {trade.get('short')}/{trade.get('long')} "
                f"score={trade.get('candidate_score')} contracts={trade.get('contracts')} "
                f"credit={trade.get('entry_credit')} pnl={trade.get('net_pnl')}"
            )
        lines.append("")

    # Sample candidate reject reasons for the best near-miss tranche
    if near_miss and candidates:
        sample_ts = near_miss[0].get("timestamp", "")
        sample_candidates = [c for c in candidates if c.get("timestamp") == sample_ts]
        if sample_candidates:
            lines.extend([f"## Candidate detail @ {sample_ts[11:16]} (best near-miss)", ""])
            lines.append("| Side | Status | Reason | Score | Sleeve | Credit |")
            lines.append("|---|---|---|---:|---|---:|")
            for cand in sorted(sample_candidates, key=lambda c: safe_float(c.get("score")), reverse=True)[:12]:
                lines.append(
                    f"| {cand.get('side')} | {cand.get('status')} | {cand.get('reason')} | "
                    f"{cand.get('score')} | {cand.get('sleeve')} | {cand.get('credit')} |"
                )
            lines.append("")

    lines.extend(
        [
            "## Read",
            "",
            "- MBH's smooth daily returns imply **many small premium clips across most tranches**. "
            "If this day shows mostly `low_score` / `gated` with best scores clustered below 2.50, "
            "the fix is deployment frequency (lower gate + MBH-like sizing), not proof that edge is zero.",
            "- Compare `gated` vs `low_score` counts: gates = side/regime filters; low_score = placeholder signal model.",
            "",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Per-day 15-min tranche deployment diagnostic.")
    parser.add_argument("--date", default="", help="YYYY-MM-DD (or use --pick-date)")
    parser.add_argument("--pick-date", action="store_true", help="Auto-pick high-MBH / zero-recon day")
    parser.add_argument("--recon-daily", default=str(ROOT / "simulator" / "_tmp_robustness_summary" / "daily_regime_validation.csv"))
    parser.add_argument("--mbh-sheet", default="")
    parser.add_argument("--symbol", default="SPXW")
    parser.add_argument("--processed-dir", default=str(DEFAULT_PROCESSED))
    parser.add_argument("--event-calendar", default=str(DEFAULT_EVENT_CALENDAR))
    parser.add_argument("--profile", default="baseline", choices=sorted(PROFILES))
    parser.add_argument("--account-equity", type=float, default=13_000_000)
    parser.add_argument("--train-count", type=int, default=40)
    parser.add_argument("--candidate-min-score", type=float, default=2.50)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUT))
    args = parser.parse_args()

    test_date = args.date
    if args.pick_date:
        year = None
        mbh_sheet = Path(args.mbh_sheet) if args.mbh_sheet else ROOT / "data" / "mbh_returns" / "2025.csv"
        test_date = pick_representative_date(Path(args.recon_daily), mbh_sheet)
        print(f"picked date {test_date}")
    if not test_date:
        test_date = "2025-04-24"

    year = int(test_date[:4])
    mbh_sheet = Path(args.mbh_sheet) if args.mbh_sheet else ROOT / "data" / "mbh_returns" / f"{year}.csv"
    mbh_daily = read_mbh_daily(mbh_sheet, year=year)
    mbh_return = mbh_daily.get(parse_sheet_date(test_date))

    config = replace(build_config(args.profile, args.account_equity), candidate_min_score=args.candidate_min_score)
    event_calendar = read_event_calendar(Path(args.event_calendar))
    processed_dir = Path(args.processed_dir)

    daily, tranche_rows, candidate_rows, trade_rows = simulate_one_day(
        test_date,
        processed_dir,
        args.symbol,
        config,
        args.train_count,
        event_calendar,
    )
    tranches = enrich_tranches(tranche_rows, args.candidate_min_score)

    out_dir = Path(args.output_dir) / test_date
    write_csv(out_dir / f"{test_date}_tranches.csv", tranches)
    write_csv(out_dir / f"{test_date}_candidates.csv", candidate_rows)
    write_csv(out_dir / f"{test_date}_trades.csv", trade_rows)

    report = build_report(
        test_date,
        daily,
        tranches,
        candidate_rows,
        trade_rows,
        mbh_return,
        args.profile,
        args.candidate_min_score,
    )
    report_path = out_dir / f"{test_date}_report.md"
    report_path.write_text(report, encoding="utf-8")

    print(f"date={test_date} mbh={mbh_return} recon={daily.get('return_on_equity')} trades={daily.get('trades')}")
    print(f"tranches={len(tranches)} executed={sum(1 for r in tranches if r['decision']=='executed')}")
    print(f"wrote {report_path}")


if __name__ == "__main__":
    main()
