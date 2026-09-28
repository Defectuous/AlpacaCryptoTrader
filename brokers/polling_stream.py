"""
REST-polling stream runner for brokers without a WebSocket integration.

Wakes shortly after each bar boundary, fetches the latest closed bars and a
quote per symbol, and hands them to the same callbacks the WebSocket runner
uses. After each pass it calls on_trade_update(None) so the bot reconciles
orders and the journal over REST.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import pandas as pd
from loguru import logger

import config
from brokers.base import BarCloseCallback, Broker, StreamRunner, TradeUpdateCallback

_TIMEFRAME_SECONDS = {"1Min": 60, "5Min": 300, "15Min": 900, "1Hour": 3600, "1Day": 86400}
_SETTLE_SECONDS = 5          # give the exchange time to publish the closed candle
_MAX_CONSECUTIVE_ERRORS = 10


class PollingStreamRunner(StreamRunner):
    def __init__(
        self,
        broker: Broker,
        symbols: list[str],
        on_bar_close: BarCloseCallback,
        on_trade_update: TradeUpdateCallback,
    ) -> None:
        if config.BAR_TIMEFRAME not in _TIMEFRAME_SECONDS:
            raise ValueError(f"Polling does not support timeframe {config.BAR_TIMEFRAME!r}")
        self.broker = broker
        self.symbols = symbols
        self.on_bar_close = on_bar_close
        self.on_trade_update = on_trade_update
        self._interval = _TIMEFRAME_SECONDS[config.BAR_TIMEFRAME]
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_bar: dict[str, pd.Timestamp] = {}

    def _closed_bars(self, symbol: str) -> pd.DataFrame:
        bars = self.broker.get_bars(symbol, config.BARS_LOOKBACK + 1, config.BAR_TIMEFRAME)
        if bars.empty:
            return bars
        # Drop the candle that is still forming.
        now = pd.Timestamp(datetime.now(timezone.utc))
        closed = bars[bars.index + pd.Timedelta(seconds=self._interval) <= now]
        return closed.tail(config.BARS_LOOKBACK)

    def _poll_once(self) -> None:
        snapshot: dict[str, tuple[pd.DataFrame, dict[str, float]]] = {}
        fresh: list[str] = []
        for symbol in self.symbols:
            bars = self._closed_bars(symbol)
            if bars.empty:
                logger.warning(f"{symbol}: No closed bars from {self.broker.name}")
                continue
            snapshot[symbol] = (bars, self.broker.get_latest_quote(symbol))
            if self._last_bar.get(symbol) != bars.index[-1]:
                self._last_bar[symbol] = bars.index[-1]
                fresh.append(symbol)

        for symbol in fresh:
            bars, quote = snapshot[symbol]
            self.on_bar_close(symbol, bars, quote, snapshot)
        self.on_trade_update(None)

    def _run(self) -> None:
        errors = 0
        # Record the current closed bar without trading it, like the WebSocket
        # runner, which only acts on bars that complete after startup.
        for symbol in self.symbols:
            bars = self._closed_bars(symbol)
            if not bars.empty:
                self._last_bar[symbol] = bars.index[-1]

        while not self._stop.is_set():
            wait = self._interval - (time.time() % self._interval) + _SETTLE_SECONDS
            if self._stop.wait(wait):
                break
            try:
                self._poll_once()
                errors = 0
            except Exception as exc:
                errors += 1
                logger.error(f"Polling error ({errors}/{_MAX_CONSECUTIVE_ERRORS}): {exc}")
                if errors >= _MAX_CONSECUTIVE_ERRORS:
                    logger.error("Too many consecutive polling errors; stopping feed")
                    return

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=f"{self.broker.name}-poller", daemon=True)
        self._thread.start()
        logger.info(
            f"Polling started for {len(self.symbols)} symbols "
            f"({config.BAR_TIMEFRAME} bars via {self.broker.name} REST)"
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        logger.info("Polling stopped")

    def dead_streams(self) -> list[str]:
        thread = self._thread
        return [thread.name] if thread is not None and not thread.is_alive() else []
