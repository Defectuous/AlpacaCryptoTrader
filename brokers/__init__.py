"""
Broker selection.

The active exchange is chosen with BROKER in .env ("alpaca" or "coinbase").
Call get_broker() anywhere to get the shared instance.
"""
from __future__ import annotations

import threading

import config
from brokers.base import Broker

_broker: Broker | None = None
_lock = threading.Lock()


def get_broker() -> Broker:
    global _broker
    with _lock:
        if _broker is None:
            name = config.BROKER
            if name == "alpaca":
                from brokers.alpaca import AlpacaBroker

                _broker = AlpacaBroker()
            elif name == "coinbase":
                from brokers.coinbase import CoinbaseBroker

                _broker = CoinbaseBroker()
            else:
                raise ValueError(f"Unknown BROKER {name!r}; use 'alpaca' or 'coinbase'")
    return _broker


def resolve_symbols() -> list[str]:
    """
    Fill config.SYMBOLS from the broker when SYMBOLS=all, then return it.

    The list is updated in place so modules that already hold a reference
    to config.SYMBOLS see the resolved symbols.
    """
    if config.TRADE_ALL_SYMBOLS and not config.SYMBOLS:
        symbols = [s for s in get_broker().list_symbols() if s not in config.EXCLUDED_SYMBOLS]
        if not symbols:
            raise RuntimeError(f"{config.BROKER} returned no tradable USD symbols")
        config.SYMBOLS[:] = symbols
    return config.SYMBOLS
