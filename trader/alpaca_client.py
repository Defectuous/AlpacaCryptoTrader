"""
Alpaca API client initialisation.

Provides lazy-initialised singletons for the trading client and data client
so the rest of the codebase never has to manage credentials directly.
"""
from __future__ import annotations

from alpaca.trading.client import TradingClient
from alpaca.data.historical import CryptoHistoricalDataClient
from loguru import logger

import config

_trading_client: TradingClient | None = None
_data_client: CryptoHistoricalDataClient | None = None


def get_trading_client() -> TradingClient:
    """Return the shared TradingClient instance (paper or live per config)."""
    global _trading_client
    if _trading_client is None:
        if not config.ALPACA_API_KEY or not config.ALPACA_SECRET_KEY:
            raise ValueError(
                "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in your .env file."
            )
        _trading_client = TradingClient(
            api_key=config.ALPACA_API_KEY,
            secret_key=config.ALPACA_SECRET_KEY,
            paper=config.ALPACA_PAPER,
        )
        mode = "PAPER" if config.ALPACA_PAPER else "LIVE"
        logger.info(f"TradingClient initialised ({mode} mode)")
    return _trading_client


def get_data_client() -> CryptoHistoricalDataClient:
    """Return the shared CryptoHistoricalDataClient instance."""
    global _data_client
    if _data_client is None:
        _data_client = CryptoHistoricalDataClient(
            api_key=config.ALPACA_API_KEY,
            secret_key=config.ALPACA_SECRET_KEY,
        )
        logger.info("CryptoHistoricalDataClient initialised")
    return _data_client
