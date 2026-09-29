"""
Coinbase Advanced Trade broker (framework).

Status
------
* Market data: working. Uses Coinbase's public endpoints (no account or keys).
* Trading / account: NOT implemented yet. Those methods raise
  NotImplementedError, and supports_trading is False so main.py refuses to
  start the bot on this broker. See "Finishing the adapter" below.

Markets
-------
COINBASE_MARKET=spot     -> trade BTC-USD / ETH-USD spot. Long only.
COINBASE_MARKET=futures  -> trade US perpetual-style futures (Coinbase
                            Financial Markets), which can go long or short.
                            Contracts are fixed sizes (BIP = 0.01 BTC,
                            ETP = 0.1 ETH), so quantities are rounded down to
                            whole contracts.

Signals always use spot candles/quotes (deepest history, 24/7); only order
routing changes with COINBASE_MARKET. Override product ids per symbol with
COINBASE_FUTURES_PRODUCTS="BTC/USD=BIP-20DEC30-CDE,ETH/USD=ETP-20DEC30-CDE".

Finishing the adapter
---------------------
1. pip install coinbase-advanced-py and create a CDP API key
   (COINBASE_API_KEY / COINBASE_API_SECRET in .env).
2. Implement get_account / get_positions (futures: get_futures_balance_summary,
   list_futures_positions), get_open_orders / get_closed_orders (list_orders),
   submit_order (create_order; attach the stop via a trigger_bracket or a
   separate stop-limit order), cancel_order (cancel_orders), close_position.
3. Set supports_trading to True, test with tiny size, then enable.
Coinbase has no working paper environment (its sandbox returns canned
responses), so paper trading on Coinbase needs a simulated broker.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from urllib import parse, request

import pandas as pd
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

_API = "https://api.coinbase.com/api/v3/brokerage"
_GRANULARITY: dict[str, tuple[str, int]] = {
    "1Min":  ("ONE_MINUTE", 60),
    "5Min":  ("FIVE_MINUTE", 300),
    "15Min": ("FIFTEEN_MINUTE", 900),
    "1Hour": ("ONE_HOUR", 3600),
    "1Day":  ("ONE_DAY", 86400),
}
_MAX_CANDLES = 350          # Coinbase per-request limit

# US perpetual-style futures (Coinbase Financial Markets). Five-year expiries;
# check the current ids with GET /market/products?product_type=FUTURE.
DEFAULT_FUTURES_PRODUCTS: dict[str, str] = {
    "BTC/USD": "BIP-20DEC30-CDE",
    "ETH/USD": "ETP-20DEC30-CDE",
}

_NOT_READY = (
    "Coinbase trading is not implemented yet — only market data works. "
    "See the module docstring in brokers/coinbase.py."
)


def _get(path: str, params: dict[str, str | int]) -> dict:
    url = f"{_API}{path}?{parse.urlencode(params)}"
    req = request.Request(url, headers={"User-Agent": "AlpacaCryptoTrader"})
    with request.urlopen(req, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def _parse_product_map(raw: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in filter(None, (part.strip() for part in raw.split(","))):
        symbol, _, product = item.partition("=")
        if product:
            mapping[symbol.strip()] = product.strip()
    return mapping


class CoinbaseBroker(Broker):
    name = "coinbase"

    def __init__(self) -> None:
        self.market = os.getenv("COINBASE_MARKET", "spot").strip().lower()
        if self.market not in ("spot", "futures"):
            raise ValueError("COINBASE_MARKET must be 'spot' or 'futures'")
        self.futures_products = {
            **DEFAULT_FUTURES_PRODUCTS,
            **_parse_product_map(os.getenv("COINBASE_FUTURES_PRODUCTS", "")),
        }
        logger.info(f"Coinbase broker initialised ({self.market}; market data only)")

    # ---- Capabilities -------------------------------------------------------
    @property
    def is_paper(self) -> bool:
        return False                # Coinbase has no paper environment

    @property
    def supports_short(self) -> bool:
        return self.market == "futures"

    @property
    def supports_trading(self) -> bool:
        return False                # flip once the trading methods are implemented

    # ---- Symbols ----------------------------------------------------------------
    @staticmethod
    def data_product(symbol: str) -> str:
        """Spot product used for candles and quotes: 'BTC/USD' -> 'BTC-USD'."""
        return symbol.replace("/", "-")

    def trade_product(self, symbol: str) -> str:
        """Product orders are routed to for this market."""
        if self.market == "futures":
            if symbol not in self.futures_products:
                raise ValueError(f"No Coinbase futures product configured for {symbol}")
            return self.futures_products[symbol]
        return self.data_product(symbol)

    # ---- Account / orders (to implement) ----------------------------------------
    def get_account(self) -> AccountInfo:
        raise NotImplementedError(_NOT_READY)

    def get_positions(self) -> dict[str, Position]:
        raise NotImplementedError(_NOT_READY)

    def get_open_orders(self, symbol: str | None = None, nested: bool = False) -> list[Order]:
        raise NotImplementedError(_NOT_READY)

    def get_closed_orders(self, limit: int = 500) -> list[Order]:
        raise NotImplementedError(_NOT_READY)

    def submit_order(self, request: OrderRequest) -> Order:
        raise NotImplementedError(_NOT_READY)

    def cancel_order(self, order_id: str) -> None:
        raise NotImplementedError(_NOT_READY)

    def close_position(self, symbol: str) -> Order:
        raise NotImplementedError(_NOT_READY)

    # ---- Market data ----------------------------------------------------------------
    def list_symbols(self) -> list[str]:
        """Online USD spot pairs; in futures mode, only those with a futures product."""
        payload = _get("/market/products", {"product_type": "SPOT", "limit": 1000})
        symbols = sorted(
            f"{p['base_currency_id']}/USD"
            for p in payload.get("products", [])
            if p.get("quote_currency_id") == "USD"
            and p.get("status") == "online"
            and not p.get("trading_disabled")
            and not p.get("is_disabled")
        )
        if self.market == "futures":
            symbols = [s for s in symbols if s in self.futures_products]
        return symbols

    def get_bars_history(
        self, symbol: str, start: datetime, end: datetime, timeframe: str
    ) -> pd.DataFrame:
        granularity, seconds = _GRANULARITY.get(timeframe, _GRANULARITY["15Min"])
        product = self.data_product(symbol)
        rows: list[dict] = []
        chunk_start = int(start.timestamp())
        end_ts = int(end.timestamp())
        while chunk_start < end_ts:
            chunk_end = min(chunk_start + seconds * _MAX_CANDLES, end_ts)
            payload = _get(
                f"/market/products/{product}/candles",
                {"start": chunk_start, "end": chunk_end, "granularity": granularity},
            )
            rows.extend(payload.get("candles", []))
            chunk_start = chunk_end
            if chunk_start < end_ts:
                time.sleep(0.1)     # stay well under public rate limits

        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df.index = pd.to_datetime(df.pop("start").astype(int), unit="s", utc=True)
        df = df.astype(float)[["open", "high", "low", "close", "volume"]]
        # Coinbase has no trade_count / vwap; approximate vwap with typical price.
        df["trade_count"] = 0
        df["vwap"] = (df["high"] + df["low"] + df["close"]) / 3.0
        df.index.name = "timestamp"
        return df[~df.index.duplicated(keep="last")].sort_index()

    def get_bars(self, symbol: str, lookback: int, timeframe: str) -> pd.DataFrame:
        seconds = _GRANULARITY.get(timeframe, _GRANULARITY["15Min"])[1]
        end = datetime.now(timezone.utc)
        start = end - timedelta(seconds=seconds * int(lookback * 1.2 + 2))
        return self.get_bars_history(symbol, start, end, timeframe).tail(lookback)

    def get_latest_quote(self, symbol: str) -> dict[str, float]:
        book = _get("/market/product_book", {"product_id": self.data_product(symbol), "limit": 1})
        pricebook = book.get("pricebook", {})
        bids, asks = pricebook.get("bids") or [], pricebook.get("asks") or []
        if not bids or not asks:
            logger.warning(f"{symbol}: Empty Coinbase order book")
            return dict(BLOCKING_QUOTE)
        bid, ask = float(bids[0]["price"]), float(asks[0]["price"])
        return {"bid": bid, "ask": ask, "spread_pct": spread_pct(bid, ask)}

    # ---- Streaming ----------------------------------------------------------------------
    def create_stream(
        self,
        symbols: list[str],
        on_bar_close: BarCloseCallback,
        on_trade_update: TradeUpdateCallback,
    ) -> StreamRunner:
        from brokers.polling_stream import PollingStreamRunner

        return PollingStreamRunner(self, symbols, on_bar_close, on_trade_update)
