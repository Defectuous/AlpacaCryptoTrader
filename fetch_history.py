"""
Download / refresh the local price history used by backtests (data/history_store.py).

Uses BACKTEST_ALPACA_API_KEY / BACKTEST_ALPACA_SECRET_KEY, never the bot's key.
Run it on the dev machine, not the Pi.

Usage
-----
    python fetch_history.py --symbols all --days 400   # first run: full download
    python fetch_history.py                            # refresh every cached coin
    python fetch_history.py --symbols BTC/USD ETH/USD --timeframe 15Min --days 90

Then backtest from the cache:
    python backtest_runner.py --cache --symbols all --days 360
"""
from __future__ import annotations

import argparse
import sys

from loguru import logger

from data import history_store


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh the local backtest price history")
    parser.add_argument("--symbols", nargs="+", default=None, metavar="SYM",
                        help='Coins to fetch, or "all" for every tradable USD coin (default: those already cached)')
    parser.add_argument("--days", type=int, default=400, help="How far back to backfill (default: 400)")
    parser.add_argument("--timeframe", default="1Hour", help='Bar size, e.g. "1Hour" "15Min" (default: 1Hour)')
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stdout, format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}")

    if args.symbols and [s.lower() for s in args.symbols] == ["all"]:
        symbols = history_store.tradable_symbols()
    elif args.symbols:
        symbols = [s.upper() for s in args.symbols]
    else:
        symbols = history_store.cached_symbols(args.timeframe)
        if not symbols:
            sys.exit('Nothing cached yet; run with --symbols all (or a list) first.')

    logger.info(f"Fetching {args.timeframe} bars for {len(symbols)} coins ({args.days}d backfill)")
    added = history_store.update(symbols, args.timeframe, args.days)
    logger.success(f"Done: {sum(added.values())} new bars across {len(added)} coins")


if __name__ == "__main__":
    main()
