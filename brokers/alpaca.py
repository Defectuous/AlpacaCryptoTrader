"""
Alpaca crypto broker (paper or live, per ALPACA_PAPER).

Alpaca crypto is long-only: the API does not allow short crypto positions,
so supports_short is False and the bot disables short entries on this broker.

Alpaca also rejects bracket/OTO orders for crypto, so exits are managed here:
the entry goes in as a plain order whose client_order_id carries the stop and
take-profit ("acx-e-<stop>-<target>-<id>"). ensure_protection() then places a
stop-limit exit ("acx-x-...") for the filled position size and keeps it in
place, which needs no local state and survives restarts. Take-profits are
checked against the quote whenever ensure_protection() runs.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pandas as pd
from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest, CryptoLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    StopLimitOrderRequest,
)
from loguru import logger

import config
from brokers.base import (
    BLOCKING_QUOTE,
    AccountInfo,
    BarCloseCallback,
    Broker,
    Order,
    OrderRequest,
    Position,
    StreamRunner,
    TradeUpdateCallback,
    spread_pct,
)

_TIMEFRAMES: dict[str, TimeFrame] = {
    "1Min":  TimeFrame(1,  TimeFrameUnit.Minute),
    "5Min":  TimeFrame(5,  TimeFrameUnit.Minute),
    "15Min": TimeFrame(15, TimeFrameUnit.Minute),
    "1Hour": TimeFrame(1,  TimeFrameUnit.Hour),
    "1Day":  TimeFrame(1,  TimeFrameUnit.Day),
}
MINUTES_PER_BAR: dict[str, int] = {"1Min": 1, "5Min": 5, "15Min": 15, "1Hour": 60, "1Day": 1440}

ENTRY_TAG = "acx-e-"            # entry orders placed by the bot
EXIT_TAG = "acx-x-"             # protective exits placed by ensure_protection()
_QTY_TOLERANCE = 0.001          # re-place the stop if position size drifts > 0.1 %


def _entry_client_id(stop: float | None, target: float | None) -> str:
    return f"{ENTRY_TAG}{stop or 0:.10g}-{target or 0:.10g}-{uuid4().hex[:12]}"


def _parse_entry_client_id(client_id: str) -> tuple[float | None, float | None]:
    """Return (stop, target) encoded by _entry_client_id, None when absent."""
    try:
        stop_s, target_s, _ = client_id[len(ENTRY_TAG):].split("-")
        stop, target = float(stop_s), float(target_s)
    except ValueError:
        return None, None
    return (stop or None), (target or None)


def _enum_str(value: Any) -> str:
    """alpaca-py enums stringify as 'OrderStatus.FILLED'; return the bare lowercase value."""
    return str(getattr(value, "value", value) or "").lower()


def _float(value: Any) -> float | None:
    return float(value) if value not in (None, "") else None


class AlpacaBroker(Broker):
    name = "alpaca"

    def __init__(self) -> None:
        if not config.ALPACA_API_KEY or not config.ALPACA_SECRET_KEY:
            raise ValueError(
                "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in your .env file."
            )
        self._trading = TradingClient(
            api_key=config.ALPACA_API_KEY,
            secret_key=config.ALPACA_SECRET_KEY,
            paper=config.ALPACA_PAPER,
        )
        self._data = CryptoHistoricalDataClient(
            api_key=config.ALPACA_API_KEY,
            secret_key=config.ALPACA_SECRET_KEY,
        )
        logger.info(f"Alpaca broker initialised ({'PAPER' if self.is_paper else 'LIVE'} mode)")

    # ---- Capabilities -------------------------------------------------------
    @property
    def is_paper(self) -> bool:
        return config.ALPACA_PAPER

    @property
    def supports_short(self) -> bool:
        return False

    # ---- Symbols ----------------------------------------------------------------
    @staticmethod
    def _bot_symbol(alpaca_symbol: str) -> str:
        """Positions come back as 'BTCUSD'; map to the configured 'BTC/USD' form."""
        compact = alpaca_symbol.replace("/", "")
        for symbol in config.SYMBOLS:
            if symbol.replace("/", "") == compact:
                return symbol
        return alpaca_symbol

    # ---- Account --------------------------------------------------------------
    def get_account(self) -> AccountInfo:
        account = self._trading.get_account()
        return AccountInfo(
            cash=float(account.cash),
            portfolio_value=float(account.portfolio_value),
            buying_power=float(account.buying_power),
            status=_enum_str(account.status),
        )

    def get_positions(self) -> dict[str, Position]:
        positions: dict[str, Position] = {}
        for pos in self._trading.get_all_positions():
            symbol = self._bot_symbol(pos.symbol)
            positions[symbol] = Position(
                symbol=symbol,
                qty=abs(float(pos.qty)),
                side="short" if _enum_str(pos.side) == "short" else "long",
                avg_entry=float(pos.avg_entry_price),
                market_value=float(pos.market_value),
                unrealized_pl=float(pos.unrealized_pl),
            )
        return positions

    # ---- Orders -----------------------------------------------------------------
    def _order(self, raw: Any) -> Order:
        return Order(
            id=str(raw.id),
            symbol=self._bot_symbol(str(raw.symbol)),
            side="sell" if _enum_str(raw.side) == "sell" else "buy",
            status=_enum_str(raw.status),
            qty=float(raw.qty or 0),
            filled_qty=float(raw.filled_qty or 0),
            filled_avg_price=_float(raw.filled_avg_price),
            order_type=_enum_str(raw.order_type),
            client_order_id=str(raw.client_order_id or ""),
            legs=[self._order(leg) for leg in (getattr(raw, "legs", None) or [])],
            submitted_at=raw.submitted_at,
            raw=raw,
        )

    def get_open_orders(self, symbol: str | None = None, nested: bool = False) -> list[Order]:
        """nested=False: entry orders only. nested=True: also the managed exits."""
        request = GetOrdersRequest(
            status=QueryOrderStatus.OPEN,
            symbols=[symbol] if symbol else None,
            nested=nested,
        )
        orders = [self._order(o) for o in self._trading.get_orders(request)]
        if not nested:
            orders = [o for o in orders if not o.client_order_id.startswith(EXIT_TAG)]
        return orders

    def get_closed_orders(self, limit: int = 500) -> list[Order]:
        request = GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=limit, nested=True)
        return [self._order(o) for o in self._trading.get_orders(request)]

    def submit_order(self, request: OrderRequest) -> Order:
        """Submit a plain entry; ensure_protection() adds the stop once it fills."""
        args: dict[str, Any] = {
            "symbol": request.symbol,
            "qty": request.qty,
            "side": OrderSide.SELL if request.side == "sell" else OrderSide.BUY,
            "time_in_force": TimeInForce.GTC,
            "client_order_id": request.client_order_id
            or _entry_client_id(request.stop_loss, request.take_profit),
        }
        if request.order_type == "limit":
            if request.limit_price is None:
                raise ValueError("limit order requires limit_price")
            raw = self._trading.submit_order(
                LimitOrderRequest(limit_price=round(request.limit_price, 8), **args)
            )
        else:
            raw = self._trading.submit_order(MarketOrderRequest(**args))
        return self._order(raw)

    def cancel_order(self, order_id: str) -> None:
        self._trading.cancel_order_by_id(order_id)

    def close_position(self, symbol: str) -> Order:
        return self._order(self._trading.close_position(symbol.replace("/", "")))

    # ---- Managed exits ------------------------------------------------------------
    def _latest_entry_targets(self, symbol: str) -> tuple[float | None, float | None]:
        """Stop/target from the most recent filled bot entry in *symbol*."""
        request = GetOrdersRequest(
            status=QueryOrderStatus.CLOSED, symbols=[symbol], limit=50, direction="desc"
        )
        for raw in self._trading.get_orders(request):
            order = self._order(raw)
            if order.client_order_id.startswith(ENTRY_TAG) and order.filled_qty > 0:
                return _parse_entry_client_id(order.client_order_id)
        return None, None

    def _place_stop(self, position: Position, stop: float) -> None:
        # Stop-limit with a slippage buffer; Alpaca crypto has no plain stop orders.
        buffer = config.MAX_SLIPPAGE_PCT
        is_long = position.side == "long"
        limit = stop * (1 - buffer) if is_long else stop * (1 + buffer)
        raw = self._trading.submit_order(
            StopLimitOrderRequest(
                symbol=position.symbol,
                qty=position.qty,
                side=OrderSide.SELL if is_long else OrderSide.BUY,
                time_in_force=TimeInForce.GTC,
                stop_price=round(stop, 8),
                limit_price=round(limit, 8),
                client_order_id=f"{EXIT_TAG}{uuid4().hex[:16]}",
            )
        )
        logger.success(
            f"{position.symbol}: Protective stop placed qty={position.qty:.8f} "
            f"stop={stop:.8f} limit={limit:.8f} id={raw.id}"
        )

    def ensure_protection(self) -> None:
        positions = self.get_positions()
        if not positions:
            return
        exits: dict[str, list[Order]] = {}
        for order in self.get_open_orders(nested=True):
            if order.client_order_id.startswith(EXIT_TAG):
                exits.setdefault(order.symbol, []).append(order)

        for symbol, position in positions.items():
            stop, target = self._latest_entry_targets(symbol)
            if stop is None:
                logger.debug(f"{symbol}: Position has no bot entry with a stop; leaving it unmanaged")
                continue

            quote = self.get_latest_quote(symbol)
            is_long = position.side == "long"
            price = quote["bid"] if is_long else quote["ask"]
            target_hit = target is not None and price > 0 and (
                price >= target if is_long else price <= target
            )
            stop_breached = price > 0 and (price <= stop if is_long else price >= stop)
            current = exits.get(symbol, [])

            if target_hit or (stop_breached and not current):
                reason = "take-profit reached" if target_hit else "stop breached while unprotected"
                logger.warning(f"{symbol}: {reason} (price={price:.8f}); closing position")
                for order in current:
                    self.cancel_order(order.id)
                self.close_position(symbol)
                continue

            if current and all(
                abs(order.qty - position.qty) <= position.qty * _QTY_TOLERANCE for order in current
            ):
                continue
            for order in current:       # wrong size (partial fill / fees): replace
                self.cancel_order(order.id)
            self._place_stop(position, stop)

    # ---- Market data ----------------------------------------------------------------
    def _fetch_bars(self, symbol: str, timeframe: str, start: datetime, end: datetime | None) -> pd.DataFrame:
        request = CryptoBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=_TIMEFRAMES.get(timeframe, TimeFrame(15, TimeFrameUnit.Minute)),
            start=start,
            end=end,
        )
        df: pd.DataFrame = self._data.get_crypto_bars(request).df
        if df.empty:
            return pd.DataFrame()
        # Alpaca returns a (symbol, timestamp) MultiIndex even for one symbol.
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")
        df.index = pd.to_datetime(df.index, utc=True)
        return df.sort_index().copy()

    def get_bars(self, symbol: str, lookback: int, timeframe: str) -> pd.DataFrame:
        # Request 50 % extra to cover gaps in crypto data.
        minutes = int(lookback * MINUTES_PER_BAR.get(timeframe, 15) * 1.5)
        start = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        return self._fetch_bars(symbol, timeframe, start, None).tail(lookback)

    def get_bars_history(
        self, symbol: str, start: datetime, end: datetime, timeframe: str
    ) -> pd.DataFrame:
        return self._fetch_bars(symbol, timeframe, start, end)

    def get_latest_quote(self, symbol: str) -> dict[str, float]:
        quotes = self._data.get_crypto_latest_quote(CryptoLatestQuoteRequest(symbol_or_symbols=symbol))
        if symbol not in quotes:
            logger.warning(f"{symbol}: No quote data returned")
            return dict(BLOCKING_QUOTE)
        bid, ask = float(quotes[symbol].bid_price), float(quotes[symbol].ask_price)
        return {"bid": bid, "ask": ask, "spread_pct": spread_pct(bid, ask)}

    # ---- Streaming ----------------------------------------------------------------------
    def create_stream(
        self,
        symbols: list[str],
        on_bar_close: BarCloseCallback,
        on_trade_update: TradeUpdateCallback,
    ) -> StreamRunner:
        from brokers.alpaca_stream import AlpacaStreamRunner

        return AlpacaStreamRunner(symbols, on_bar_close, on_trade_update)
