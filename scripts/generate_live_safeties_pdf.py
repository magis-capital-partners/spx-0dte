"""Generate a brief PDF summarizing live executor safeties."""
from __future__ import annotations

from pathlib import Path

from fpdf import FPDF

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "Live_Executor_Safeties.pdf"

MARGIN = 18


class GuidePDF(FPDF):
    def __init__(self) -> None:
        super().__init__()
        self.set_margins(MARGIN, MARGIN, MARGIN)
        self.set_auto_page_break(auto=True, margin=MARGIN)

    def header(self) -> None:
        if self.page_no() == 1:
            return
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(120, 120, 120)
        self.cell(0, 6, "SPX 0DTE Live Executor - Safety Controls", align="L")
        self.ln(8)
        self.set_text_color(0, 0, 0)

    def footer(self) -> None:
        self.set_y(-12)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(120, 120, 120)
        self.cell(0, 8, f"Magis Capital Partners  |  Page {self.page_no()}", align="C")

    @property
    def content_w(self) -> float:
        return self.epw

    def section_title(self, title: str) -> None:
        self.ln(4)
        self.set_font("Helvetica", "B", 12)
        self.set_text_color(20, 60, 120)
        self.multi_cell(self.content_w, 6, title)
        y = self.get_y()
        self.set_draw_color(20, 60, 120)
        self.line(MARGIN, y, MARGIN + self.content_w, y)
        self.ln(3)
        self.set_text_color(0, 0, 0)

    def body(self, text: str) -> None:
        self.set_font("Helvetica", "", 9.5)
        self.multi_cell(self.content_w, 4.8, text)
        self.ln(1.5)

    def bullet(self, text: str) -> None:
        self.set_font("Helvetica", "", 9.5)
        x = self.get_x()
        self.cell(4, 4.8, "-")
        self.set_x(x + 4)
        self.multi_cell(self.content_w - 4, 4.8, text)
        self.ln(0.5)

    def table(self, headers: list[str], rows: list[list[str]], col_widths: list[float]) -> None:
        line_h = 4.5
        self.set_font("Helvetica", "B", 8.5)
        self.set_fill_color(230, 236, 248)
        for hdr, cw in zip(headers, col_widths):
            self.cell(cw, 6, hdr, border=1, fill=True)
        self.ln()
        self.set_font("Helvetica", "", 8)
        for row in rows:
            heights = []
            for cell, cw in zip(row, col_widths):
                lines = self.multi_cell(cw, line_h, cell, dry_run=True, output="LINES")
                heights.append(len(lines) * line_h)
            row_h = max(heights) if heights else line_h
            if self.get_y() + row_h > self.h - MARGIN:
                self.add_page()
            x0 = self.get_x()
            y0 = self.get_y()
            x = x0
            for cell, cw in zip(row, col_widths):
                self.set_xy(x, y0)
                self.multi_cell(cw, line_h, cell, border=1)
                x += cw
            self.set_xy(x0, y0 + row_h)
        self.ln(2)


def build() -> Path:
    pdf = GuidePDF()
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 20)
    pdf.set_text_color(20, 60, 120)
    pdf.multi_cell(pdf.content_w, 9, "SPX 0DTE Live Executor")
    pdf.set_font("Helvetica", "B", 14)
    pdf.multi_cell(pdf.content_w, 7, "Safety Controls Overview")
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(80, 80, 80)
    pdf.multi_cell(
        pdf.content_w,
        4.5,
        "Brief operator reference for paper and live Interactive Brokers sessions. "
        "Defaults assume LiveConfig pilot sizing ($500k equity, 2 contracts/tranche) "
        "and production profile p3_poststop_cooldown_120. Details: live/README.md.",
    )
    pdf.set_text_color(0, 0, 0)
    pdf.ln(2)

    # --- Startup ---
    pdf.section_title("1. Startup interlocks")
    pdf.body(
        "These run before any new risk is taken. Failure exits the process."
    )
    pdf.table(
        ["Control", "What it does"],
        [
            [
                "Paper / live gate",
                "mode=live also requires allow_live=True. Default stays paper.",
            ],
            [
                "Eligible calendar day",
                "Skips non-tradable SPXW / era days.",
            ],
            [
                "VIX session skip",
                "No trading if same-day VIX open > 35 (or VIX missing when gated).",
            ],
            [
                "Baselines freshness",
                "Gated profiles require fresh signal baselines (max age 3 days).",
            ],
            [
                "Single-instance lock",
                "data/live/<date>/executor.lock - second process exits if PID alive.",
            ],
            [
                "KILL at startup",
                "If data/live/KILL or session KILL exists, refuse to start.",
            ],
            [
                "Account overlay",
                "IB NetLiq >= configured equity; BuyingPower >= 15% of equity. "
                "PnL limits still use static account_equity.",
            ],
            [
                "Live market data",
                "mode=live forces no delayed-quote fallback - fail if OPRA/index missing.",
            ],
        ],
        [48, pdf.content_w - 48],
    )

    # --- Restart ---
    pdf.section_title("2. Restart & book recovery")
    pdf.bullet(
        "Rebuild open spreads from fills.jsonl; cancel orphan SPXW/BAG orders."
    )
    pdf.bullet(
        "Fail loud if IB SPXW risk does not match the recovered book."
    )
    pdf.bullet(
        "Restore halt / flatten / same-side cooldowns / stop counts so a restart "
        "cannot resume selling after a halt."
    )
    pdf.bullet(
        "Re-arm native BUY STP stops on recovered short legs."
    )

    # --- In-session ---
    pdf.section_title("3. In-session risk controls")
    pdf.table(
        ["Control", "Trigger / action"],
        [
            [
                "3x short-leg stop",
                "2-poll confirm; limit buy then MKT. Keep long wing. Confirm IB short qty dropped.",
            ],
            [
                "Native STP backstop",
                "Resting BUY STP at 3x if the process dies; verify/re-arm periodically.",
            ],
            [
                "Side cooldown",
                "After a stop, block new entries on that side for 120 minutes.",
            ],
            [
                "PnL governor",
                "Halt new entries at -2.25% of equity; flatten open risk at -3.25%.",
            ],
            [
                "NetLiq loop guard",
                "Halt entries if NetLiq < 90% of configured equity.",
            ],
            [
                "Mark integrity",
                "Missing quotes: halt (never treat as $0). Unavailable 5 minutes with open risk: flatten.",
            ],
            [
                "Stale quotes",
                "3 consecutive polls age >20s (10s near stop): halt entries only - never flatten on stale alone.",
            ],
            [
                "Open-risk caps",
                "Max 6 open contracts / 3 per side / 2 same strike.",
            ],
            [
                "Live stop caps",
                "Max 2 stops per side / 4 per day (entry_risk_block_reason).",
            ],
            [
                "Pre-entry BP",
                "Block entry if BuyingPower < estimated margin for the size.",
            ],
            [
                "Entry quote hygiene",
                "Require live NBBO, min credit $0.20, quote age <=5s.",
            ],
            [
                "FOMC / VIX sizing",
                "No new entries after 13:30 on FOMC; VIX 25-35 can upsize x1.25 (capped).",
            ],
            [
                "Disconnect breaker",
                "Halt, reconnect with backoff, re-arm STPs. Fail with open risk -> confirmed flatten.",
            ],
            [
                "Exception path",
                "Unexpected error: cancel pending, flatten, audit, exit.",
            ],
        ],
        [42, pdf.content_w - 42],
    )

    # --- Flatten ---
    pdf.section_title("4. Flatten confirmation & audit")
    pdf.body(
        "Governor / kill / reconnect-fail / mark-unavailable flattens place MKT combo "
        "closes, wait for fill (one MKT retry), and only mark closed on fill. "
        "flatten_incomplete is logged if IB residual remains. flatten_audit checks "
        "IB is flat afterward and can Slack-alert on residual lots."
    )
    pdf.body(
        "End of day: defined-risk 0DTE spreads are left to cash settle (matches backtest) "
        "unless flattened earlier by a safety path."
    )

    # --- Operator ---
    pdf.section_title("5. Operator controls (local + Slack)")
    pdf.bullet(
        "KILL file: data/live/KILL or data/live/<date>/KILL - flatten and exit. "
        "Windows: echo. > data\\live\\KILL"
    )
    pdf.bullet(
        "Slack: set SPX_SLACK_WEBHOOK_URL. Alerts on halt, flatten, disconnect, "
        "kill, stop_unconfirmed, native STP reject, watchdog, etc. No-op if unset."
    )
    pdf.bullet(
        "Local watchdog (same machine): .\\scripts\\run_live_watchdog.ps1 - "
        "watches executor.lock + heartbeat.json; Slack if PID dead or heartbeat "
        "stale with open risk. Optional -WriteKill."
    )
    pdf.bullet(
        "Kill / heartbeat / lock are local to the host - they do not sync across PCs. "
        "Slack is the cross-device notifier."
    )

    # --- Broker ---
    pdf.section_title("6. Brokerage-side (every machine)")
    pdf.bullet("TWS/Gateway API on; trusted 127.0.0.1; correct paper/live port.")
    pdf.bullet(
        "Precautionary max order size and daily loss; outside RTH off for SPXW."
    )
    pdf.bullet("OPRA + US index market data (required for live mode).")
    pdf.bullet("Never run two executors against the same account at once.")

    # --- Other computer ---
    pdf.section_title("7. Moving to another computer")
    pdf.body(
        "git pull; Python deps; IB Gateway login; market data; refresh baselines; "
        "set Slack webhook; start watchdog on that host; redo TWS precautionary "
        "settings. Full checklist: live/README.md - Running on another computer."
    )

    pdf.ln(3)
    pdf.set_font("Helvetica", "I", 8.5)
    pdf.set_text_color(90, 90, 90)
    pdf.multi_cell(
        pdf.content_w,
        4.5,
        "Regenerate: python scripts/generate_live_safeties_pdf.py  ->  docs/Live_Executor_Safeties.pdf",
    )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(OUT))
    return OUT


if __name__ == "__main__":
    path = build()
    print(f"Wrote {path}")
