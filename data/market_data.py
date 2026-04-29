"""
Market data retrieval from Alpaca.

Fetches OHLCV bars and latest bid/ask quotes for crypto symbols.
All network errors are caught and logged; callers receive empty data
rather than exceptions so the main loop stays alive.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pandas as pd
from alpaca.data.requests import CryptoBarsRequest, CryptoLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from loguru import logger

import config
from trader.alpaca_client import get_data_client

# ---------------------------------------------------------------------------
# Timeframe mapping
# ---------------------------------------------------------------------------
_TIMEFRAME_MAP: dict[str, TimeFrame] = {
    "1Min":  TimeFrame(1,  TimeFrameUnit.Minute),
    "5Min":  TimeFrame(5,  TimeFrameUnit.Minute),
    "15Min": TimeFrame(15, TimeFrameUnit.Minute),
    "1Hour": TimeFrame(1,  TimeFrameUnit.Hour),
    "1Day":  TimeFrame(1,  TimeFrameUnit.Day),
}

_MINUTES_PER_BAR: dict[str, int] = {
    "1Min":  1,
    "5Min":  5,
    "15Min": 15,
    "1Hour": 60,
    "1Day":  1440,
}


def get_bars(symbol: str, lookback: int = config.BARS_LOOKBACK) -> pd.DataFrame:
    """
    Fetch the most recent *lookback* OHLCV bars for *symbol*.

    Returns a DataFrame indexed by UTC timestamp with columns:
        open, high, low, close, volume, trade_count, vwap  (raw Alpaca fields)

    Returns an empty DataFrame on any error.
    """
    client = get_data_client()
    timeframe = _TIMEFRAME_MAP.get(config.BAR_TIMEFRAME, TimeFrame(15, TimeFrameUnit.Minute))
    minutes_per_bar = _MINUTES_PER_BAR.get(config.BAR_TIMEFRAME, 15)

    # Request 50 % extra bars to account for weekends / gaps in crypto data
    total_minutes = int(lookback * minutes_per_bar * 1.5)
    start = datetime.now(timezone.utc) - timedelta(minutes=total_minutes)

    try:
        request = CryptoBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=timeframe,
            start=start,
        )
        bars = get_data_client().get_crypto_bars(request)
        df: pd.DataFrame = bars.df

        if df.empty:
            logger.warning(f"{symbol}: No bars returned from Alpaca")
            return pd.DataFrame()

        # Alpaca returns a MultiIndex (symbol, timestamp) when one symbol is requested
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")

        df.index = pd.to_datetime(df.index, utc=True)
        df = df.sort_index().tail(lookback).copy()
        return df

    except Exception as exc:
        logger.error(f"{symbol}: Error fetching bars — {exc}")
        return pd.DataFrame()


def get_latest_quote(symbol: str) -> dict[str, float]:
    """
    Fetch the latest bid/ask quote for *symbol*.

    Returns a dict with keys: bid, ask, spread_pct.
    On error returns sentinel values that will block trading (spread_pct=999).
    """
    try:
        request = CryptoLatestQuoteRequest(symbol_or_symbols=symbol)
        quotes = get_data_client().get_crypto_latest_quote(request)

        if symbol not in quotes:
            logger.warning(f"{symbol}: No quote data returned")
            return {"bid": 0.0, "ask": 0.0, "spread_pct": 999.0}

        q = quotes[symbol]
        bid = float(q.bid_price)
        ask = float(q.ask_price)
        mid = (bid + ask) / 2.0
        spread_pct = ((ask - bid) / mid * 100.0) if mid > 0 else 999.0

        return {"bid": bid, "ask": ask, "spread_pct": round(spread_pct, 4)}

    except Exception as exc:
        logger.error(f"{symbol}: Error fetching quote — {exc}")
        return {"bid": 0.0, "ask": 0.0, "spread_pct": 999.0}
