"""Live Alpaca crypto market-data and trade-update streams (WebSocket).

The market-data stream is used only while the data plan allows it. If Alpaca
rejects the subscription (e.g. "symbol limit exceeded" on the free plan) or
sends no market data soon after start, market data switches to REST polling
for the rest of the run. Trade updates always stream: they come from the
trading API, which the data plan does not limit.
"""
from __future__ import annotations

import asyncio
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Any

import pandas as pd
from alpaca.data.live import CryptoDataStream
from alpaca.trading.stream import TradingStream
from loguru import logger

import config
from brokers import get_broker
from brokers.base import BarCloseCallback, StreamRunner, TradeUpdateCallback
from brokers.polling_stream import PollingStreamRunner
from data.market_data import get_bars


_TIMEFRAME_MINUTES = {
    "1Min": 1,
    "5Min": 5,
    "15Min": 15,
    "1Hour": 60,
}

# Quotes arrive every few seconds; this long without any market message means the
# stream (or a scan blocking its loop) is stuck even though the thread is alive.
_STALL_SECONDS = 600

# A working stream sends quotes within seconds. Five minutes also outlasts the
# brief "connection limit exceeded" (406) seen while a previous session times out.
_STREAM_GRACE_SECONDS = 300

# Stream errors meaning the data plan does not cover this subscription.
_PLAN_LIMIT_CODES = {405, 409}  # symbol limit exceeded, insufficient subscription


class _CryptoDataStream(CryptoDataStream):
    """Records plan-limit errors, which alpaca-py only logs."""

    rejection: str | None = None

    async def _dispatch(self, msg: dict) -> None:
        if msg.get("T") == "error" and msg.get("code") in _PLAN_LIMIT_CODES:
            self.rejection = f"{msg.get('msg')} ({msg.get('code')})"
        await super()._dispatch(msg)


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
        self._market_stream = _CryptoDataStream(
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
        self._scan_thread: threading.Thread | None = None
        self._watch_thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._has_market_data = False
        # Set when the market stream is unavailable and REST polling replaces it.
        self._poller: PollingStreamRunner | None = None
        # Completed bars wait here for the scan thread. Scanning inside the
        # websocket handler stopped the stream reading, so keepalive pongs went
        # unanswered and Alpaca dropped the connection during every hourly scan.
        self._scan_queue: queue.Queue[tuple[str, pd.DataFrame, dict[str, float]] | None] = (
            queue.Queue()
        )
        self._scan_lock = threading.Lock()
        self._last_market_message = time.monotonic()
        self._quotes: dict[str, dict[str, float]] = {}
        self._frames: dict[str, pd.DataFrame] = {}
        self._aggregators = {symbol: _BarAggregator(minutes) for symbol in symbols}

        for symbol in symbols:
            bars = get_bars(symbol)
            if not bars.empty:
                self._frames[symbol] = bars.tail(config.BARS_LOOKBACK).copy()

    async def _handle_quote(self, quote: Any) -> None:
        self._last_market_message = time.monotonic()
        self._has_market_data = True
        bid = float(quote.bid_price)
        ask = float(quote.ask_price)
        mid = (bid + ask) / 2.0
        self._quotes[quote.symbol] = {
            "bid": bid,
            "ask": ask,
            "spread_pct": ((ask - bid) / mid * 100.0) if mid > 0 else 999.0,
        }

    async def _handle_bar(self, bar: Any) -> None:
        self._last_market_message = time.monotonic()
        self._has_market_data = True
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
        self._scan_queue.put((bar.symbol, frame.copy(), quote.copy()))

    def _scan_worker(self) -> None:
        while (item := self._scan_queue.get()) is not None:
            try:
                self._run_bar_callback(*item)
            except Exception as exc:
                logger.error(f"{item[0]}: Bar scan failed: {exc}", exc_info=True)

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

    def _watch_market_stream(self) -> None:
        """Switch market data to REST polling if the stream never delivers."""
        assert self._market_thread is not None
        deadline = time.monotonic() + _STREAM_GRACE_SECONDS
        while not self._has_market_data:
            if self._stopping.wait(1):
                return
            if self._market_stream.rejection:
                reason = f"Alpaca rejected the subscription: {self._market_stream.rejection}"
            elif not self._market_thread.is_alive():
                reason = "the stream closed before sending data"
            elif time.monotonic() > deadline:
                reason = f"no market data within {_STREAM_GRACE_SECONDS // 60} min"
            else:
                continue
            self._fall_back_to_polling(reason)
            return
        logger.info("Market-data stream is live")

    def _fall_back_to_polling(self, reason: str) -> None:
        logger.warning(
            f"Market-data stream unavailable ({reason}); polling {len(self.symbols)} "
            f"symbols over REST at each {config.BAR_TIMEFRAME} bar instead"
        )
        try:
            self._market_stream.stop()
        except Exception as exc:
            logger.debug(f"Market stream stop: {exc}")
        poller = PollingStreamRunner(
            get_broker(), self.symbols, self.on_bar_close, self.on_trade_update
        )
        poller.start()
        self._poller = poller

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
        self._scan_thread = threading.Thread(
            target=self._scan_worker,
            name="alpaca-bar-scan",
            daemon=True,
        )
        self._watch_thread = threading.Thread(
            target=self._watch_market_stream,
            name="alpaca-stream-watch",
            daemon=True,
        )
        self._last_market_message = time.monotonic()
        self._scan_thread.start()
        self._market_thread.start()
        self._trade_thread.start()
        self._watch_thread.start()
        logger.info(
            f"Streaming started for {len(self.symbols)} symbols "
            f"({config.BAR_TIMEFRAME} bars + quotes + trade updates)"
        )

    def dead_streams(self) -> list[str]:
        """Stream threads that have exited, or a market stream gone silent."""
        dead = [
            thread.name
            for thread in (self._trade_thread, self._scan_thread)
            if thread is not None and not thread.is_alive()
        ]
        if self._poller is not None:
            dead += self._poller.dead_streams()
        # Until the stream has delivered data, the watcher decides whether to poll.
        elif self._has_market_data and self._market_thread is not None:
            if not self._market_thread.is_alive():
                dead.append(self._market_thread.name)
            silent = time.monotonic() - self._last_market_message
            if silent > _STALL_SECONDS:
                dead.append(f"alpaca-market-stream (no data for {silent / 60:.0f} min)")
        return dead

    def stop(self) -> None:
        self._stopping.set()
        if self._poller is not None:
            self._poller.stop()
        else:
            self._market_stream.stop()
        self._trade_stream.stop()
        self._scan_queue.put(None)
        for thread in (
            self._market_thread,
            self._trade_thread,
            self._scan_thread,
            self._watch_thread,
        ):
            if thread is not None:
                thread.join(timeout=5)
        logger.info("Streaming stopped")