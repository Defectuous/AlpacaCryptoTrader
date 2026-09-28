"""
Market data retrieval from the configured broker (see BROKER in .env).

Fetches OHLCV bars and latest bid/ask quotes for crypto symbols.
All network errors are caught and logged; callers receive empty data
rather than exceptions so the main loop stays alive.
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd
from loguru import logger

import config
from brokers import get_broker
from brokers.base import BLOCKING_QUOTE


def get_bars(symbol: str, lookback: int = config.BARS_LOOKBACK) -> pd.DataFrame:
    """
    Fetch the most recent *lookback* OHLCV bars for *symbol*.

    Returns a DataFrame indexed by UTC timestamp with columns:
        open, high, low, close, volume, trade_count, vwap

    Returns an empty DataFrame on any error.
    """
    try:
        df = get_broker().get_bars(symbol, lookback, config.BAR_TIMEFRAME)
        if df.empty:
            logger.warning(f"{symbol}: No bars returned from {config.BROKER}")
        return df
    except Exception as exc:
        logger.error(f"{symbol}: Error fetching bars — {exc}")
        return pd.DataFrame()


def get_bars_history(
    symbol: str,
    start: datetime,
    end: datetime,
    timeframe_str: str | None = None,
) -> pd.DataFrame:
    """
    Fetch OHLCV bars for *symbol* between *start* and *end*.

    Unlike get_bars(), which fetches a fixed lookback from "now", this
    function fetches a specific date range and is intended for backtesting.

    Returns an empty DataFrame on any error.
    """
    try:
        df = get_broker().get_bars_history(symbol, start, end, timeframe_str or config.BAR_TIMEFRAME)
        if df.empty:
            logger.warning(f"{symbol}: No historical bars returned from {config.BROKER}")
        else:
            logger.info(f"{symbol}: {len(df)} bars fetched ({start.date()} -> {end.date()})")
        return df
    except Exception as exc:
        logger.error(f"{symbol}: Error fetching history — {exc}")
        return pd.DataFrame()


def get_latest_quote(symbol: str) -> dict[str, float]:
    """
    Fetch the latest bid/ask quote for *symbol*.

    Returns a dict with keys: bid, ask, spread_pct.
    On error returns sentinel values that will block trading (spread_pct=999).
    """
    try:
        return get_broker().get_latest_quote(symbol)
    except Exception as exc:
        logger.error(f"{symbol}: Error fetching quote — {exc}")
        return dict(BLOCKING_QUOTE)
