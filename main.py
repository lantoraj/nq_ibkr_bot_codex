"""
CLI Entry Point for the NQ / MNQ Futures IBKR Trading Bot.

Usage examples:
  1. Run Backtest on synthetic or CSV data:
     python main.py --mode backtest
     python main.py --mode backtest --csv data/nq_1m.csv --symbol NQ

  2. Download historical bars from IBKR TWS/Gateway to CSV:
     python main.py --mode download-history --csv data/mnq_history.csv --duration "5 D"

  3. Run Live / Paper Trading on IBKR TWS or Gateway:
     python main.py --mode paper --symbol MNQ --bar-size "1 min"
     python main.py --mode live --symbol NQ --bar-size "5 mins"
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

from backtest import Backtester, generate_synthetic_nq_data
from config import BotConfig


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="NQ / MNQ Futures Intraday Trading Bot (IBKR + Python)")
    parser.add_argument(
        "--mode",
        choices=["backtest", "paper", "live", "download-history"],
        default="backtest",
        help="Execution mode (default: backtest)",
    )
    parser.add_argument(
        "--symbol",
        choices=["NQ", "MNQ"],
        default="MNQ",
        help="Futures symbol: MNQ ($2/pt Micro) or NQ ($20/pt E-mini)",
    )
    parser.add_argument(
        "--contract-month",
        default="",
        help="Optional contract month e.g. '202612' (leave empty to auto-select front month)",
    )
    parser.add_argument(
        "--bar-size",
        choices=["1 min", "2 mins", "5 mins"],
        default="2 mins",
        help="Intraday bar timeframe (default: '2 mins' - optimized)",
    )
    parser.add_argument(
        "--entry-mode",
        choices=["pullback", "crossover", "both", "hybrid_all"],
        default="hybrid_all",
        help="Signal trigger mode: pullback, crossover, both, or hybrid_all (default: hybrid_all)",
    )
    parser.add_argument(
        "--contracts",
        type=int,
        default=1,
        help="Fixed number of contracts per trade (default: 1)",
    )
    parser.add_argument(
        "--max-daily-loss",
        type=float,
        default=250.0,
        help="Daily loss limit in USD before halting (default: 250.0)",
    )
    parser.add_argument(
        "--csv",
        default="",
        help="Path to historical CSV file for backtesting or downloading history",
    )
    parser.add_argument(
        "--duration",
        default="5 D",
        help="IBKR history duration when using --mode download-history (default: '5 D')",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="IBKR TWS / Gateway host (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Override IBKR port (defaults: 7497 for paper, 7496 for live)",
    )
    parser.add_argument(
        "--client-id",
        type=int,
        default=101,
        help="IBKR API clientId (default: 101)",
    )
    return parser.parse_args()


def main() -> int:
    setup_logging()
    args = parse_args()
    logger = logging.getLogger("nq_ibkr_bot")

    cfg = BotConfig()
    cfg.contract.symbol = args.symbol
    cfg.contract.contract_month = args.contract_month
    cfg.strategy.bar_size = args.bar_size
    cfg.strategy.entry_mode = args.entry_mode
    cfg.risk.fixed_contracts = args.contracts
    cfg.risk.max_daily_loss_usd = args.max_daily_loss
    cfg.ibkr.host = args.host
    cfg.ibkr.client_id = args.client_id

    if args.port > 0:
        if args.mode == "live":
            cfg.ibkr.live_port = args.port
        else:
            cfg.ibkr.paper_port = args.port

    if args.mode == "backtest":
        if args.csv and Path(args.csv).exists():
            logger.info("Loading historical data from %s...", args.csv)
            df = pd.read_csv(args.csv)
        else:
            logger.info("Generating 5 days of synthetic 1-minute RTH NQ futures bars for backtest...")
            df = generate_synthetic_nq_data(num_days=5)

        comm = 0.62 if cfg.contract.symbol == "MNQ" else 2.10
        bt = Backtester(cfg, commission_per_side_usd=comm, slippage_ticks=1)
        result = bt.run(df)
        metrics = result["metrics"]
        trades_df: pd.DataFrame = result["trades_df"]

        print("\n" + "=" * 68)
        print(f"  NQ / MNQ INTRADAY BACKTEST REPORT ({cfg.contract.symbol} | {cfg.strategy.bar_size})")
        print("=" * 68)
        for k, v in metrics.items():
            print(f"  {k:<22}: {v}")
        print("=" * 68)

        if not trades_df.empty:
            print("\nRecent Simulated Trades:")
            cols = [
                "entry_time",
                "exit_time",
                "direction",
                "contracts",
                "entry_price",
                "exit_price",
                "exit_reason",
                "net_pnl_usd",
            ]
            print(trades_df[cols].tail(10).to_string(index=False))
        return 0

    # Lazy import IBKR runner so offline backtesting works even without TWS open
    from ibkr_execution import IBKRNQBotRunner

    if args.mode == "download-history":
        out_csv = args.csv or f"data/{cfg.contract.symbol.lower()}_history.csv"
        Path(out_csv).parent.mkdir(parents=True, exist_ok=True)
        runner = IBKRNQBotRunner(cfg, mode="paper")
        runner.download_historical_csv(out_csv, duration=args.duration)
        return 0

    if args.mode == "live":
        logger.warning("STARTING IN LIVE TRADING MODE ON REAL ACCOUNT PORT %d!", cfg.ibkr.live_port)

    runner = IBKRNQBotRunner(cfg, mode=args.mode)
    runner.run_streaming()
    return 0


if __name__ == "__main__":
    sys.exit(main())
