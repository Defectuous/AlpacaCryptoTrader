"""
Exchange-neutral broker interface.

Everything outside brokers/ talks to an exchange only through Broker and the
plain dataclasses below, so strategy, risk, journal and notifier code never
sees an SDK object. Statuses and sides are normalised to lowercase strings
("filled", "buy", "short") so comparisons work the same on every exchange.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import pandas as pd

OrderSideStr = Literal["buy", "sell"]
PositionSideStr = Literal["long", "short"]

# Callback signatures used by stream runners.
BarCloseCallback = Callable[
    [str, pd.DataFrame, dict[str, float], dict[str, tuple[pd.DataFrame, dict[str, float]]]],
    None,
]
TradeUpdateCallback = Callable[[Any], None]

# Quote returned when data is unavailable; spread_pct=999 blocks trading.
BLOCKING_QUOTE: dict[str, float] = {"bid": 0.0, "ask": 0.0, "spread_pct": 999.0}


@dataclass
class AccountInfo:
    cash: float
    portfolio_value: float
    buying_power: float
    status: str                     # "active" when trading is allowed

    def as_dict(self) -> dict:
        return {
            "cash": self.cash,
            "portfolio_value": self.portfolio_value,
            "buying_power": self.buying_power,
            "status": self.status,
        }


@dataclass
class Position:
    symbol: str                     # bot symbol, e.g. "BTC/USD"
    qty: float                      # always positive; direction is in `side`
    side: PositionSideStr
    avg_entry: float
    market_value: float
    unrealized_pl: float

    def as_dict(self) -> dict:
        return {
            "qty": self.qty,
            "avg_entry": self.avg_entry,
            "market_value": self.market_value,
            "unrealized_pl": self.unrealized_pl,
            "side": self.side,
        }


@dataclass
class Order:
    id: str
    symbol: str
    side: OrderSideStr
    status: str                     # "new", "accepted", "filled", "canceled", ...
    qty: float
    filled_qty: float = 0.0
    filled_avg_price: float | None = None
    order_type: str = ""            # "market", "limit", "stop", ...
    client_order_id: str = ""
    legs: list[Order] = field(default_factory=list)
    submitted_at: datetime | None = None
    raw: Any = field(default=None, repr=False)


@dataclass
class OrderRequest:
    """
    One entry order, optionally with attached exits.

    stop_loss and take_profit both set -> bracket
    stop_loss only                     -> entry plus protective stop (OTO)
    neither                            -> plain order
    """
    symbol: str
    side: OrderSideStr
    qty: float
    order_type: Literal["market", "limit"] = "market"
    limit_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    client_order_id: str | None = None


class StreamRunner(ABC):
    """Delivers completed strategy bars (and order updates) to callbacks."""

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def dead_streams(self) -> list[str]:
        """Names of feeds that have stopped for good; non-empty means restart the bot."""


class Broker(ABC):
    """A crypto exchange the bot can read data from and trade on."""

    #: Short name used in config and logs, e.g. "alpaca".
    name: str = ""

    # ---- Capabilities -------------------------------------------------------
    @property
    @abstractmethod
    def is_paper(self) -> bool:
        """True when orders go to a simulated / paper account."""

    @property
    @abstractmethod
    def supports_short(self) -> bool:
        """True when this broker/account can open short positions."""

    @property
    def supports_trading(self) -> bool:
        """False for data-only adapters that cannot place orders yet."""
        return True

    # ---- Account --------------------------------------------------------------
    @abstractmethod
    def get_account(self) -> AccountInfo: ...

    @abstractmethod
    def get_positions(self) -> dict[str, Position]:
        """Open positions keyed by bot symbol."""

    # ---- Orders -----------------------------------------------------------------
    @abstractmethod
    def get_open_orders(self, symbol: str | None = None, nested: bool = False) -> list[Order]:
        """Open orders. nested=False returns entry orders; True includes attached exits."""

    @abstractmethod
    def get_closed_orders(self, limit: int = 500) -> list[Order]:
        """Recently closed orders with attached exit orders in `legs`."""

    @abstractmethod
    def submit_order(self, request: OrderRequest) -> Order: ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> None: ...

    @abstractmethod
    def close_position(self, symbol: str) -> Order:
        """Flatten the whole position in *symbol* at market."""

    def ensure_protection(self) -> None:
        """
        Make sure every open bot position has its protective exits.

        Brokers that attach stops/targets natively (bracket orders) need
        nothing here. Brokers that cannot attach them place and maintain
        the exits in this hook; the bot calls it after every order update
        and on every scan.
        """

    # ---- Market data ----------------------------------------------------------------
    @abstractmethod
    def list_symbols(self) -> list[str]:
        """Every tradable USD-quoted crypto pair, in bot form ("BTC/USD"), sorted."""

    @abstractmethod
    def get_bars(self, symbol: str, lookback: int, timeframe: str) -> pd.DataFrame:
        """Most recent *lookback* OHLCV bars, UTC-indexed, oldest first."""

    @abstractmethod
    def get_bars_history(
        self, symbol: str, start: datetime, end: datetime, timeframe: str
    ) -> pd.DataFrame: ...

    @abstractmethod
    def get_latest_quote(self, symbol: str) -> dict[str, float]:
        """{"bid", "ask", "spread_pct"}; BLOCKING_QUOTE on failure."""

    # ---- Streaming ----------------------------------------------------------------------
    @abstractmethod
    def create_stream(
        self,
        symbols: list[str],
        on_bar_close: BarCloseCallback,
        on_trade_update: TradeUpdateCallback,
    ) -> StreamRunner: ...


def spread_pct(bid: float, ask: float) -> float:
    mid = (bid + ask) / 2.0
    return round((ask - bid) / mid * 100.0, 4) if mid > 0 and bid > 0 and ask > 0 else 999.0
