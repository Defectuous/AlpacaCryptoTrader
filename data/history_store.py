"""
Local OHLCV store for backtesting: one CSV per symbol and timeframe under
HISTORY_DIR (default data/history/), e.g. data/history/1Hour/BTC-USD.csv.gz.

Downloads use BACKTEST_ALPACA_API_KEY / BACKTEST_ALPACA_SECRET_KEY only, never
the live bot's key, so backtests don't eat into the bot's rate limit. Bars are
stored raw; load() cleans isolated bad prints by default.

    python fetch_history.py --symbols all --days 400      # first run: full download
    python fetch_history.py                               # later runs: only new bars
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from loguru import logger

import config
from brokers.alpaca import _TIMEFRAMES, MINUTES_PER_BAR

COLUMNS = ["open", "high", "low", "close", "volume", "trade_count", "vwap"]


def _path(symbol: str, timeframe: str) -> Path:
    return Path(config.HISTORY_DIR) / timeframe / f"{symbol.replace('/', '-')}.csv.gz"


def cached_symbols(timeframe: str = "1Hour") -> list[str]:
    folder = Path(config.HISTORY_DIR) / timeframe
    return sorted(p.name.removesuffix(".csv.gz").replace("-", "/") for p in folder.glob("*.csv.gz"))


def _read(symbol: str, timeframe: str) -> pd.DataFrame:
    path = _path(symbol, timeframe)
    if not path.exists():
        return pd.DataFrame(columns=COLUMNS)
    df = pd.read_csv(path, index_col="timestamp")
    df.index = pd.to_datetime(df.index, utc=True)
    return df


# ---------------------------------------------------------------------------
# Downloading (backtest key only)
# ---------------------------------------------------------------------------

def _keys() -> tuple[str, str]:
    key, secret = config.BACKTEST_ALPACA_API_KEY, config.BACKTEST_ALPACA_SECRET_KEY
    if not key or not secret:
        raise ValueError(
            "Set BACKTEST_ALPACA_API_KEY and BACKTEST_ALPACA_SECRET_KEY in .env "
            "(a separate Alpaca paper account, not the bot's key)."
        )
    if key == config.ALPACA_API_KEY:
        raise ValueError("BACKTEST_ALPACA_API_KEY is the bot's key; use a separate account's key.")
    return key, secret


def tradable_symbols() -> list[str]:
    """Every active, tradable USD coin, minus EXCLUDED_SYMBOLS, via the backtest key."""
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import AssetClass, AssetStatus
    from alpaca.trading.requests import GetAssetsRequest

    key, secret = _keys()
    client = TradingClient(api_key=key, secret_key=secret, paper=True)
    assets = client.get_all_assets(GetAssetsRequest(asset_class=AssetClass.CRYPTO, status=AssetStatus.ACTIVE))
    return sorted(
        a.symbol for a in assets
        if a.tradable and a.symbol.endswith("/USD") and a.symbol not in config.EXCLUDED_SYMBOLS
    )


def update(symbols: list[str], timeframe: str = "1Hour", days: int = 400) -> dict[str, int]:
    """Backfill to *days* ago and append new completed bars; returns new bars per symbol."""
    from alpaca.data.historical import CryptoHistoricalDataClient
    from alpaca.data.requests import CryptoBarsRequest

    key, secret = _keys()
    client = CryptoHistoricalDataClient(api_key=key, secret_key=secret)
    bar = timedelta(minutes=MINUTES_PER_BAR.get(timeframe, 60))
    now = datetime.now(timezone.utc)
    want_start = now - timedelta(days=days)

    def fetch(symbol: str, start: datetime) -> pd.DataFrame:
        request = CryptoBarsRequest(
            symbol_or_symbols=symbol, timeframe=_TIMEFRAMES[timeframe], start=start, end=now,
        )
        df = client.get_crypto_bars(request).df
        if df.empty:
            return df
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")
        df.index = pd.to_datetime(df.index, utc=True)
        return df[df.index + bar <= now]     # completed bars only

    added: dict[str, int] = {}
    for symbol in symbols:
        old = _read(symbol, timeframe)
        parts = [old]
        if old.empty or old.index.min() > want_start + bar:
            parts.append(fetch(symbol, want_start))          # first run or backfill
        else:
            # Refetch the newest cached bar too, in case it was cached before it closed.
            parts.append(fetch(symbol, old.index.max()))
        df = pd.concat([p for p in parts if not p.empty])
        if df.empty:
            logger.warning(f"{symbol}: no {timeframe} bars returned")
            continue
        df = df[~df.index.duplicated(keep="last")].sort_index()
        added[symbol] = len(df) - len(old)
        path = _path(symbol, timeframe)
        path.parent.mkdir(parents=True, exist_ok=True)
        df.reindex(columns=COLUMNS).to_csv(path, index_label="timestamp")
        logger.info(f"{symbol}: {added[symbol]:+d} bars, {len(df)} cached "
                    f"({df.index.min():%Y-%m-%d} -> {df.index.max():%Y-%m-%d %H:%M})")
    return added


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def clean_bad_prints(df: pd.DataFrame) -> int:
    """Pull prices more than 40% away from the 7-bar median back to it, in place.

    Alpaca's history has isolated zero-volume spikes (e.g. PEPE at ~1100x for one
    hour) that would fake stop, profit-trail and momentum signals in a backtest.
    """
    med = df["close"].rolling(7, center=True, min_periods=3).median()
    fixed = 0
    for col in ("open", "close"):
        bad = (df[col] / med > 1.4) | (df[col] / med < 0.7)
        df.loc[bad, col] = med[bad]
        fixed += int(bad.sum())
    bad_high = df["high"] / med > 1.4
    bad_low = df["low"] / med < 0.7
    df.loc[bad_high, "high"] = df.loc[bad_high, ["open", "close"]].max(axis=1)
    df.loc[bad_low, "low"] = df.loc[bad_low, ["open", "close"]].min(axis=1)
    return fixed + int(bad_high.sum() + bad_low.sum())


def load(
    symbol: str,
    start: datetime | None = None,
    end: datetime | None = None,
    timeframe: str = "1Hour",
    clean: bool = True,
) -> pd.DataFrame:
    """Cached bars for *symbol* in [start, end]; empty if nothing is cached."""
    df = _read(symbol, timeframe)
    if start is not None:
        df = df[df.index >= pd.Timestamp(start)]
    if end is not None:
        df = df[df.index <= pd.Timestamp(end)]
    df = df.copy()
    if clean and not df.empty:
        fixed = clean_bad_prints(df)
        if fixed:
            logger.debug(f"{symbol}: cleaned {fixed} bad price prints")
    return df
