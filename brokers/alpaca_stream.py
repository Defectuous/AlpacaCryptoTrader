"""Live Alpaca crypto market-data and trade-update streams (WebSocket)."""
from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone
from typing import Any

import pandas as pd
from alpaca.data.live import CryptoDataStream
from alpaca.trading.stream import TradingStream
from loguru import logger

import config
from brokers.base import BarCloseCallback, StreamRunner, TradeUpdateCallback
from data.market_data import get_bars


_TIMEFRAME_MINUTES = {
    "1Min": 1,
    "5Min": 5,
    "15Min": 15,
    "1Hour": 60,
}


def _bucket_start(timestamp: datetime, minutes: int) -> datetime:
    timestamp = timestamp.astimezone(timezone.utc)
    total_minutes = timestamp.hour * 60 + timestamp.minute
    bucket_minutes = (total_minutes // minutes) * minutes
    return timestamp.replace(
        hour=bucket_minutes // 60,
        minute=bucket_minutes % 60,
        second=0,
        microsecond=0,
    )


class _BarAggregator:
    """Aggregate Alpaca minute bars into the configured strategy timeframe."""

    def __init__(self, minutes: int) -> None:
        self.minutes = minutes
        self._bucket: datetime | None = None
        self._bar: dict[str, float | int | datetime] | None = None

    def update(self, bar: Any) -> dict[str, float | int | datetime] | None:
        timestamp = bar.timestamp.astimezone(timezone.utc)
        bucket = _bucket_start(timestamp, self.minutes)
        incoming = {
            "timestamp": bucket,
            "open": float(bar.open),
            "high": float(bar.high),
            "low": float(bar.low),
            "close": float(bar.close),
            "volume": float(bar.volume),
            "trade_count": int(bar.trade_count or 0),
            "vwap": float(bar.vwap) if bar.vwap is not None else 0.0,
        }

        if self._bucket is None:
            self._bucket = bucket
            self._bar = incoming
            return None

        if bucket == self._bucket:
            assert self._bar is not None
            old_volume = float(self._bar["volume"])
            new_volume = old_volume + incoming["volume"]
            if new_volume > 0:
                self._bar["vwap"] = (
                    float(self._bar["vwap"]) * old_volume
                    + float(incoming["vwap"]) * incoming["volume"]
                ) / new_volume
            self._bar["high"] = max(float(self._bar["high"]), incoming["high"])
            self._bar["low"] = min(float(self._bar["low"]), incoming["low"])
            self._bar["close"] = incoming["close"]
            self._bar["volume"] = new_volume
            self._bar["trade_count"] = int(self._bar["trade_count"]) + incoming["trade_count"]
            return None

        completed = self._bar
        self._bucket = bucket
        self._bar = incoming
        return completed


class AlpacaStreamRunner(StreamRunner):
    """Run crypto market data and account updates in dedicated stream threads."""

    def __init__(
        self,
        symbols: list[str],
        on_bar_close: BarCloseCallback,
        on_trade_update: TradeUpdateCallback,
    ) -> None:
        minutes = _TIMEFRAME_MINUTES.get(config.BAR_TIMEFRAME)
        if minutes is None:
            raise ValueError(f"Streaming does not support timeframe {config.BAR_TIMEFRAME!r}")

        self.symbols = symbols
        self.on_bar_close = on_bar_close
        self.on_trade_update = on_trade_update
        self._market_stream = CryptoDataStream(
            config.ALPACA_API_KEY,
            config.ALPACA_SECRET_KEY,
        )
        self._trade_stream = TradingStream(
            config.ALPACA_API_KEY,
            config.ALPACA_SECRET_KEY,
            paper=config.ALPACA_PAPER,
        )
        self._market_thread: threading.Thread | None = None
        self._trade_thread: threading.Thread | None = None
        self._scan_lock = threading.Lock()
        self._quotes: dict[str, dict[str, float]] = {}
        self._frames: dict[str, pd.DataFrame] = {}
        self._aggregators = {symbol: _BarAggregator(minutes) for symbol in symbols}

        for symbol in symbols:
            bars = get_bars(symbol)
            if not bars.empty:
                self._frames[symbol] = bars.tail(config.BARS_LOOKBACK).copy()

    async def _handle_quote(self, quote: Any) -> None:
        bid = float(quote.bid_price)
        ask = float(quote.ask_price)
        mid = (bid + ask) / 2.0
        self._quotes[quote.symbol] = {
            "bid": bid,
            "ask": ask,
            "spread_pct": ((ask - bid) / mid * 100.0) if mid > 0 else 999.0,
        }

    async def _handle_bar(self, bar: Any) -> None:
        completed = self._aggregators[bar.symbol].update(bar)
        if completed is None:
            return

        row = pd.DataFrame([completed]).set_index("timestamp")
        frame = pd.concat([self._frames.get(bar.symbol, pd.DataFrame()), row])
        frame = frame[~frame.index.duplicated(keep="last")].sort_index().tail(config.BARS_LOOKBACK)
        self._frames[bar.symbol] = frame

        quote = self._quotes.get(bar.symbol)
        if quote is None:
            logger.debug(f"{bar.symbol}: Completed bar received before a quote")
            return

        # Account checks and order placement must not block the websocket loop.
        await asyncio.to_thread(self._run_bar_callback, bar.symbol, frame.copy(), quote.copy())

    def _run_bar_callback(
        self,
        symbol: str,
        frame: pd.DataFrame,
        quote: dict[str, float],
    ) -> None:
        with self._scan_lock:
            snapshot = {
                current_symbol: (current_frame.copy(), current_quote.copy())
                for current_symbol, current_frame in self._frames.items()
                if (current_quote := self._quotes.get(current_symbol)) is not None
            }
            self.on_bar_close(symbol, frame, quote, snapshot)

    async def _handle_trade_update(self, update: Any) -> None:
        await asyncio.to_thread(self.on_trade_update, update)

    def start(self) -> None:
        self._market_stream.subscribe_quotes(self._handle_quote, *self.symbols)
        self._market_stream.subscribe_bars(self._handle_bar, *self.symbols)
        self._trade_stream.subscribe_trade_updates(self._handle_trade_update)

        self._market_thread = threading.Thread(
            target=self._market_stream.run,
            name="alpaca-market-stream",
            daemon=True,
        )
        self._trade_thread = threading.Thread(
            target=self._trade_stream.run,
            name="alpaca-trade-stream",
            daemon=True,
        )
        self._market_thread.start()
        self._trade_thread.start()
        logger.info(
            f"Streaming started for {len(self.symbols)} symbols "
            f"({config.BAR_TIMEFRAME} bars + quotes + trade updates)"
        )

    def dead_streams(self) -> list[str]:
        """Names of stream threads that have exited (stream.run returned or raised)."""
        return [
            thread.name
            for thread in (self._market_thread, self._trade_thread)
            if thread is not None and not thread.is_alive()
        ]

    def stop(self) -> None:
        self._market_stream.stop()
        self._trade_stream.stop()
        for thread in (self._market_thread, self._trade_thread):
            if thread is not None:
                thread.join(timeout=5)
        logger.info("Streaming stopped")