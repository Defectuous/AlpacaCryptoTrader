"""
AlpacaCryptoTrader — Backtest runner (CLI entry point).

Fetches historical OHLCV data from Alpaca, replays the live strategy
bar-by-bar, and produces per-symbol performance reports plus CSV trade logs.

Usage
-----
    python backtest_runner.py
    python backtest_runner.py --symbols BTC/USD ETH/USD --days 180
    python backtest_runner.py --symbols BTC/USD --days 90 --equity 5000 --shorts
    python backtest_runner.py --days 60 --out backtest/results

Arguments
---------
--symbols    One or more symbols to test (default: all from config.SYMBOLS)
--days       Calendar days of history to fetch (default: 90)
--equity     Starting equity for position sizing (default: 10000)
--shorts     Enable short-selling for this run (overrides config)
--out        Output folder for CSV results (default: backtest/results)
--timeframe  Bar timeframe, e.g. "15Min" "1Hour" (default: from config)
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from loguru import logger

import config
from data.market_data import get_bars_history
from backtest.engine import run_backtest
from backtest.report import compute_stats, print_report, save_trades_csv


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="AlpacaCryptoTrader — walk-forward backtest runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--symbols", nargs="+", default=config.SYMBOLS,
        metavar="SYM",
        help="Symbols to backtest (default: all from config.SYMBOLS)",
    )
    parser.add_argument(
        "--days", type=int, default=90,
        help="Calendar days of history to fetch (default: 90)",
    )
    parser.add_argument(
        "--equity", type=float, default=10_000.0,
        help="Starting equity in USD for position sizing (default: 10000)",
    )
    parser.add_argument(
        "--shorts", action="store_true",
        help="Enable short-selling for this run (overrides config.ENABLE_SHORT_SELLING)",
    )
    parser.add_argument(
        "--out", default="backtest/results",
        metavar="DIR",
        help="Output directory for CSV trade logs (default: backtest/results)",
    )
    parser.add_argument(
        "--timeframe", default=None,
        metavar="TF",
        help='Bar timeframe override, e.g. "15Min" "1Hour" (default: from config)',
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    args = _parse_args()

    # Configure logging for the standalone CLI
    logger.remove()
    logger.add(
        sys.stdout,
        colorize=True,
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}",
        level="INFO",
    )

    # Apply timeframe override before importing anything that reads config
    if args.timeframe:
        config.BAR_TIMEFRAME = args.timeframe

    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)
    out_dir = Path(args.out)

    logger.info("=" * 60)
    logger.info("AlpacaCryptoTrader — Backtest")
    logger.info(f"  Period     : {start.strftime('%Y-%m-%d')} → {end.strftime('%Y-%m-%d')}")
    logger.info(f"  Symbols    : {args.symbols}")
    logger.info(f"  Timeframe  : {config.BAR_TIMEFRAME}")
    logger.info(f"  Equity     : ${args.equity:,.0f}")
    logger.info(f"  Shorts     : {args.shorts}")
    logger.info("=" * 60)

    all_trades = []
    run_ts     = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")

    for symbol in args.symbols:
        logger.info(f"Fetching history for {symbol}…")
        df = get_bars_history(symbol, start, end, args.timeframe)
        if df.empty:
            logger.warning(f"{symbol}: No data returned — skipping")
            continue

        trades = run_backtest(symbol, df, args.equity, enable_shorts=args.shorts)
        stats  = compute_stats(trades, args.equity)

        print_report(symbol, stats, args.equity)
        all_trades.extend(trades)

        if trades:
            safe_sym = symbol.replace("/", "")
            csv_path = out_dir / f"{safe_sym}_{args.days}d_{run_ts}.csv"
            save_trades_csv(trades, csv_path)

    # Combined summary when more than one symbol is tested
    if len(args.symbols) > 1 and all_trades:
        combined_equity = args.equity * len(args.symbols)
        combined = compute_stats(all_trades, combined_equity)

        if combined:
            sep = "=" * 60
            print(f"\n{sep}")
            print(f"  COMBINED SUMMARY ({len(args.symbols)} symbols, {args.days}d)")
            print(sep)
            print(f"  Total trades  : {combined['total_trades']}")
            print(f"  Win rate      : {combined['win_rate']*100:.1f}%")
            print(f"  Profit factor : {combined['profit_factor']:.2f}")
            print(f"  Total PnL     : ${combined['total_pnl']:+.2f}  ({combined['total_pnl_pct']:+.2f}%)")
            print(f"  Max drawdown  : {combined['max_drawdown']*100:.1f}%")
            print(f"  Avg R-mult    : {combined['avg_r_multiple']:.2f}R")
            print(sep)

        # Save combined trades CSV
        combined_path = out_dir / f"combined_{args.days}d_{run_ts}.csv"
        save_trades_csv(all_trades, combined_path)


if __name__ == "__main__":
    main()
