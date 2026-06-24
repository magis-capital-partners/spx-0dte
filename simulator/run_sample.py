from datetime import time
from pathlib import Path

from mbh_simulator import StrategyConfig, read_quotes_csv, read_signals_csv, simulate_day, trades_to_rows


HERE = Path(__file__).resolve().parent


def main() -> None:
    quotes = read_quotes_csv(HERE / "sample_quotes.csv")
    signals = read_signals_csv(HERE / "sample_signals.csv")
    config = StrategyConfig(
        account_equity=1_000_000,
        fee_per_contract=0.79,
        entry_start=time(9, 45),
        entry_end=time(10, 30),
    )
    result = simulate_day(quotes, signals, config=config)

    print(f"Trades: {len(result.trades)}")
    print(f"Gross credit sold: ${result.gross_credit_sold:,.2f}")
    print(f"Gross PnL: ${result.gross_pnl:,.2f}")
    print(f"Fees: ${result.fees:,.2f}")
    print(f"Net PnL: ${result.net_pnl:,.2f}")
    print(f"Halted: {result.halted}")
    for row in trades_to_rows(result.trades):
        print(row)
    for message in result.messages:
        print(message)


if __name__ == "__main__":
    main()
