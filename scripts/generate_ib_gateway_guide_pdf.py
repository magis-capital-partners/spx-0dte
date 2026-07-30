"""Generate shareable IB Gateway guide PDF for paper/live SPX 0DTE executor."""
from __future__ import annotations

from pathlib import Path

from fpdf import FPDF

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "IB_Gateway_Guide_for_Michael.pdf"

MARGIN = 18
CONTENT_W = 210 - 2 * MARGIN  # A4 width minus margins


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
        self.cell(0, 6, "IB Gateway Setup Guide - SPX 0DTE Executor", align="L")
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
        self.ln(5)
        self.set_font("Helvetica", "B", 13)
        self.set_text_color(20, 60, 120)
        self.multi_cell(self.content_w, 7, title)
        y = self.get_y()
        self.set_draw_color(20, 60, 120)
        self.line(MARGIN, y, MARGIN + self.content_w, y)
        self.ln(4)
        self.set_text_color(0, 0, 0)

    def sub_title(self, title: str) -> None:
        self.ln(2)
        self.set_font("Helvetica", "B", 10)
        self.multi_cell(self.content_w, 5, title)
        self.ln(1)

    def body(self, text: str) -> None:
        self.set_font("Helvetica", "", 10)
        self.multi_cell(self.content_w, 5, text)
        self.ln(2)

    def bullet(self, text: str) -> None:
        self.set_font("Helvetica", "", 10)
        x = self.get_x()
        self.cell(5, 5, "-")
        self.set_x(x + 5)
        self.multi_cell(self.content_w - 5, 5, text)
        self.ln(1)

    def code(self, text: str) -> None:
        self.set_font("Courier", "", 8.5)
        self.set_fill_color(248, 248, 252)
        self.set_draw_color(200, 210, 230)
        x0 = self.get_x()
        y0 = self.get_y()
        self.multi_cell(self.content_w, 4.5, text, fill=True, border=1)
        self.ln(2)
        self.set_font("Helvetica", "", 10)
        self.set_x(x0)

    def table(self, headers: list[str], rows: list[list[str]], col_widths: list[float]) -> None:
        assert len(headers) == len(col_widths)
        line_h = 5
        self.set_font("Helvetica", "B", 9)
        self.set_fill_color(230, 236, 248)
        for i, (hdr, cw) in enumerate(zip(headers, col_widths)):
            self.cell(cw, 7, hdr, border=1, fill=True)
        self.ln()

        self.set_font("Helvetica", "", 9)
        for row in rows:
            # Row height = max lines needed in any cell
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

        self.ln(3)


def build() -> None:
    pdf = GuidePDF()
    pdf.add_page()

    # Title block
    pdf.set_font("Helvetica", "B", 22)
    pdf.set_text_color(20, 60, 120)
    pdf.multi_cell(pdf.content_w, 10, "IB Gateway Setup Guide")
    pdf.ln(1)
    pdf.set_font("Helvetica", "", 12)
    pdf.set_text_color(70, 70, 70)
    pdf.multi_cell(pdf.content_w, 6, "SPX 0DTE automated executor")
    pdf.multi_cell(pdf.content_w, 6, "Paper trading with real market data")
    pdf.ln(2)
    pdf.set_font("Helvetica", "I", 9)
    pdf.multi_cell(pdf.content_w, 5, "Prepared for Michael  |  Magis Capital Partners  |  July 2026")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(4)

    pdf.body(
        "This document explains how Interactive Brokers (IB) Gateway fits into our SPX 0DTE "
        "trading system, what you need to configure on your IB account, and how to run a paper "
        "session before we go live. The Python strategy (ib_executor.py) does not connect to IB "
        "directly - it connects to IB Gateway running on your computer."
    )

    pdf.section_title("1. What is IB Gateway?")
    pdf.body(
        "IB Gateway is Interactive Brokers' lightweight trading application. It has no charts or "
        "full trading screens. Its job is to maintain a logged-in session to IB and expose a "
        "socket API that our Python program uses for quotes, orders, and fills."
    )
    pdf.body("Think of it as a bridge:")
    pdf.code(
        "Python executor  -->  localhost:7497  -->  IB Gateway  -->  IB servers\n"
        "(our strategy)       (API port)          (your login)        (orders + data)"
    )
    pdf.sub_title("Gateway vs Trader Workstation (TWS)")
    pdf.bullet("Gateway: headless, low resource - best for automated trading.")
    pdf.bullet("TWS: full UI with charts - best for manual trading.")
    pdf.bullet("Both use the same API ports and rules. We recommend Gateway for the bot.")

    pdf.section_title("2. Paper vs live - ports and logins")
    pdf.body("IB uses separate ports and separate usernames for paper and live:")
    pdf.table(
        ["Mode", "Port", "Login"],
        [
            ["Paper (simulated)", "7497", "Paper username (separate from live)"],
            ["Live (real money)", "7496", "Live username"],
        ],
        [42, 18, pdf.content_w - 60],
    )
    pdf.body(
        "Our executor is currently configured for paper mode (port 7497). Orders are simulated; "
        "no real money is at risk. Market data behavior still depends on your subscriptions."
    )

    pdf.section_title("3. Market data - why paper does not auto-inherit live subs")
    pdf.body(
        "Paying for US Index (SPX) and OPRA (options) on your live account does NOT automatically "
        "give your paper account real-time data. By default, paper only gets delayed data unless "
        "you explicitly share subscriptions."
    )
    pdf.sub_title("One-time setup in IB Client Portal (live login)")
    pdf.bullet("Log in to Client Portal with your LIVE account.")
    pdf.bullet("Click Welcome (top right) -> Settings.")
    pdf.bullet("Under Account Configuration, click Paper Trading Account.")
    pdf.bullet('Set "Share real-time market data subscriptions with paper trading account" to YES.')
    pdf.bullet("In the dropdown, select the LIVE username that owns the Index + OPRA subscriptions.")
    pdf.bullet("Click Save.")
    pdf.sub_title("Market Data API acknowledgement (required for our Python bot)")
    pdf.bullet("Client Portal -> Settings -> Market Data Subscriptions.")
    pdf.bullet("Find Market Data API Acknowledgement (gear icon) -> enable Yes and sign the form.")
    pdf.bullet("Log out of all TWS/Gateway sessions and log back in after signing.")
    pdf.sub_title("Important: only one session at a time")
    pdf.body(
        "When market data is shared between live and paper, IB allows only ONE active session "
        "to use that data at a time. If live TWS/Gateway is open, paper may show error 10168 "
        "(not subscribed). Close live sessions before running paper Gateway for the bot."
    )

    pdf.add_page()
    pdf.section_title("4. Installing and configuring IB Gateway")
    pdf.sub_title("Install")
    pdf.bullet("Download IB Gateway from interactivebrokers.com (same account as TWS).")
    pdf.bullet("Install on the machine that will run the executor during market hours.")
    pdf.sub_title("API settings (Configure -> Settings -> API -> Settings)")
    pdf.bullet("Enable ActiveX and Socket Clients: ON (required).")
    pdf.bullet("Socket port: 7497 for paper, 7496 for live.")
    pdf.bullet("Read-Only API: OFF (we place orders).")
    pdf.bullet("Trusted IP addresses: 127.0.0.1 if Python runs on the same PC.")
    pdf.bullet("Allow connections from localhost only unless remote access is needed.")
    pdf.sub_title("Market data in Gateway")
    pdf.bullet("Settings -> Market Data -> enable Allow delayed market data (fallback if live fails).")
    pdf.bullet("For real-time: subs + paper sharing (Section 3) must be complete.")

    pdf.section_title("5. Daily workflow (paper session)")
    pdf.body("Before 9:30 AM Eastern on a trading day:")
    pdf.bullet("Close any live TWS/Gateway sessions.")
    pdf.bullet("Start IB Gateway -> Paper Trading login -> confirm port 7497.")
    pdf.bullet("Optional check: add SPX to a watchlist - price should update (live, not blank).")
    pdf.bullet("On the same machine, open PowerShell in the project folder:")
    pdf.code(
        "cd C:\\Users\\...\\spx-0dte\n"
        "python scripts\\refresh_live_baselines.py\n"
        "python live\\ib_executor.py"
    )
    pdf.body("After the close:")
    pdf.bullet("Stop the executor (Ctrl+C if still running).")
    pdf.bullet("Quit IB Gateway.")
    pdf.body("Session logs are saved under data/live/YYYY-MM-DD/ (fills, tranches, IB errors).")

    pdf.section_title("6. What success looks like in the console")
    pdf.body("Good startup (real-time data):")
    pdf.code(
        "IB connected (port 7497, market_data_type=1 requested)\n"
        "[market] SPX spot OK with market_data_type=1 (live)\n"
        "[market] streaming subscribed lines=80 ... spot=6xxx.x market_data_type=1 (live)"
    )
    pdf.body("Acceptable fallback (delayed - OK for testing, not for production):")
    pdf.code(
        "IB error 10168 ... [SPX]\n"
        "[market] falling back to delayed market data (type 3)\n"
        "[market] using delayed quotes (type 3) ..."
    )
    pdf.body(
        "Error 10168 on the first line is normal when live SPX is unavailable; the bot then "
        "falls back to 15-minute delayed data if enabled in Gateway."
    )

    pdf.section_title("7. Troubleshooting")
    pdf.sub_title("10168 - Requested market data is not subscribed")
    pdf.bullet("Paper sharing not enabled in Client Portal (Section 3).")
    pdf.bullet("Live TWS/Gateway still open - close it.")
    pdf.bullet("Gateway logged in with wrong user (must be paper user on 7497).")
    pdf.bullet("Subscriptions still Pending - often activate next business day.")
    pdf.bullet("Market Data API Acknowledgement not signed.")
    pdf.sub_title("Connection refused")
    pdf.bullet("Gateway not running, or wrong port (7497 vs 7496).")
    pdf.sub_title("No tranche activity")
    pdf.bullet("Not an eligible SPXW day, VIX gate skipped session, or market closed.")

    pdf.add_page()
    pdf.section_title("8. Roles - who does what")
    pdf.table(
        ["Task", "Owner"],
        [
            ["IB account, market data subscriptions", "Michael"],
            ["Enable paper data sharing + API acknowledgement", "Michael (Client Portal)"],
            ["Start/stop IB Gateway before/after RTH", "Michael (or designated machine)"],
            ["Run refresh_live_baselines + ib_executor", "Michael's PC or shared trading box"],
            ["Strategy code, config, debugging", "Drew / dev team"],
        ],
        [pdf.content_w * 0.62, pdf.content_w * 0.38],
    )

    pdf.section_title("9. Subscriptions needed for SPX 0DTE")
    pdf.bullet("CBOE US Index (SPX spot) - or US Securities Snapshot bundle including indices.")
    pdf.bullet("OPRA (US options) - required for SPXW option chain quotes.")
    pdf.bullet("Account minimum: typically $500 equity + subscription fees (IBKR Pro).")
    pdf.body(
        "Verify in Client Portal -> Market Data Subscriptions that both show Active (not Pending)."
    )

    pdf.section_title("10. Moving from paper to live (later)")
    pdf.bullet("Gateway login: live user, port 7496.")
    pdf.bullet("live_config.py: mode='live', allow_live=True, account_equity set to real NAV.")
    pdf.bullet("Size contracts_per_tranche for production risk limits.")
    pdf.bullet("Run only after paper sessions reconcile cleanly with the backtest.")

    pdf.ln(4)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(100, 100, 100)
    pdf.multi_cell(
        pdf.content_w,
        4,
        "Repository: spx-0dte  |  Executor: live/ib_executor.py  |  Config: live/live_config.py  |  "
        "Support: Drew Goldman",
    )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(OUT))
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    build()
