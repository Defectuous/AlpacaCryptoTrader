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
