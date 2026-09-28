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
    python backtest_runner.py --start 2026-02-04 --end 2026-03-06

Arguments
---------
--symbols    One or more symbols to test (default: all from config.SYMBOLS)
--days       Calendar days of history to fetch (default: 90) [ignored if --start/--end given]
--start      Explicit start date (YYYY-MM-DD), overrides --days
--end        Explicit end date (YYYY-MM-DD), overrides --days
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
from backtest.engine import run_backtest, run_rotation_backtest
from backtest.report import compute_stats, print_report, save_trades_csv


def _combined_max_drawdown(trades, initial_equity: float) -> float:
    """Compute portfolio drawdown from symbol-independent trade P&L streams."""
    trades_by_exit_time = {}
    for trade in trades:
        if trade.exit_time is not None:
            trades_by_exit_time.setdefault(trade.exit_time, []).append(trade)

    equity = peak = initial_equity
    max_drawdown = 0.0
    for exit_time in sorted(trades_by_exit_time):
        equity += sum(trade.pnl_usd for trade in trades_by_exit_time[exit_time])
        peak = max(peak, equity)
        for trade in trades_by_exit_time[exit_time]:
            trade.equity_after = equity
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)
    return max_drawdown


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
        help="Calendar days of history to fetch (default: 90, ignored if --start/--end given)",
    )
    parser.add_argument(
        "--start", type=str, default=None,
        metavar="YYYY-MM-DD",
        help="Explicit start date (overrides --days if provided)",
    )
    parser.add_argument(
        "--end", type=str, default=None,
        metavar="YYYY-MM-DD",
        help="Explicit end date (overrides --days if provided)",
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

    # Parse start/end dates or use --days
    if args.start and args.end:
        start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        days_label = (end - start).days
    else:
        end   = datetime.now(timezone.utc)
        start = end - timedelta(days=args.days)
        days_label = args.days

    out_dir = Path(args.out)

    logger.info("=" * 60)
    logger.info("AlpacaCryptoTrader — Backtest")
    logger.info(f"  Period     : {start.strftime('%Y-%m-%d')} → {end.strftime('%Y-%m-%d')}")
    logger.info(f"  Symbols    : {args.symbols}")
    logger.info(f"  Timeframe  : {config.BAR_TIMEFRAME}")
    logger.info(f"  Equity     : ${args.equity:,.0f}")
    logger.info(f"  Shorts     : {args.shorts}")
    logger.info("=" * 60)

    histories = {}
    run_ts     = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")

    for symbol in args.symbols:
        logger.info(f"Fetching history for {symbol}…")
        df = get_bars_history(symbol, start, end, args.timeframe)
        if df.empty:
            logger.warning(f"{symbol}: No data returned — skipping")
            continue
        histories[symbol] = df

    if not histories:
        logger.error("No historical bars available; backtest was not run")
        return

    if config.STRATEGY_MODE == "breakout_rotation":
        trades = run_rotation_backtest(histories, args.equity)
        stats = compute_stats(trades, args.equity)
        print_report("BREAKOUT ROTATION", stats, args.equity)
        if trades:
            csv_path = out_dir / f"rotation_{days_label}d_{run_ts}.csv"
            save_trades_csv(trades, csv_path)
        return

    all_trades = []
    for symbol, df in histories.items():

        enable_shorts = args.shorts or (
            config.STRATEGY_MODE == "4h_trend_momentum"
            and config.ENABLE_SHORT_SELLING
        )
        trades = run_backtest(symbol, df, args.equity, enable_shorts=enable_shorts)
        stats  = compute_stats(trades, args.equity)

        print_report(symbol, stats, args.equity)
        all_trades.extend(trades)

        if trades:
            safe_sym = symbol.replace("/", "")
            csv_path = out_dir / f"{safe_sym}_{days_label}d_{run_ts}.csv"
            save_trades_csv(trades, csv_path)

    # Combined summary when more than one symbol is tested
    if len(args.symbols) > 1 and all_trades:
        combined_equity = args.equity * len(args.symbols)
        combined = compute_stats(all_trades, combined_equity)
        combined["max_drawdown"] = _combined_max_drawdown(all_trades, combined_equity)

        if combined:
            sep = "=" * 60
            print(f"\n{sep}")
            print(f"  COMBINED SUMMARY ({len(args.symbols)} symbols, {days_label}d)")
            print(sep)
            print(f"  Total trades  : {combined['total_trades']}")
            print(f"  Win rate      : {combined['win_rate']*100:.1f}%")
            print(f"  Profit factor : {combined['profit_factor']:.2f}")
            print(f"  Total PnL     : ${combined['total_pnl']:+.2f}  ({combined['total_pnl_pct']:+.2f}%)")
            print(f"  Max drawdown  : {combined['max_drawdown']*100:.1f}%")
            print(f"  Avg R-mult    : {combined['avg_r_multiple']:.2f}R")
            print(sep)

        # Save combined trades CSV
        combined_path = out_dir / f"combined_{days_label}d_{run_ts}.csv"
        combined_trades = sorted(all_trades, key=lambda trade: trade.exit_time)
        save_trades_csv(combined_trades, combined_path)


if __name__ == "__main__":
    main()
