"""
Technical indicator calculations.

All indicators are computed with pandas / numpy only — no external TA library
required, which keeps cross-platform compatibility (including Raspberry Pi ARM).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import config


# ---------------------------------------------------------------------------
# Individual indicator functions
# ---------------------------------------------------------------------------

def calculate_ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average."""
    return series.ewm(span=period, adjust=False).mean()


def calculate_daily_vwap(df: pd.DataFrame) -> pd.Series:
    """
    Daily cumulative VWAP, reset at UTC midnight.

    Formula per bar:
        typical_price = (high + low + close) / 3
        VWAP = cumsum(typical_price * volume) / cumsum(volume)

    The index must be timezone-aware (UTC).
    """
    df = df.copy()

    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")

    date_key = df.index.date  # numpy array of date objects
    df["_date"] = date_key
    df["_tp"] = (df["high"] + df["low"] + df["close"]) / 3.0
    df["_tp_vol"] = df["_tp"] * df["volume"]

    df["_cum_tp_vol"] = df.groupby("_date")["_tp_vol"].cumsum()
    df["_cum_vol"] = df.groupby("_date")["volume"].cumsum()

    vwap = df["_cum_tp_vol"] / df["_cum_vol"]
    return vwap


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range (Wilder smoothing via EWM)."""
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def calculate_avg_volume(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Simple rolling average volume."""
    return df["volume"].rolling(window=period).mean()


# ---------------------------------------------------------------------------
# Composite indicator builder
# ---------------------------------------------------------------------------

def add_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add EMA9, EMA20, daily VWAP, ATR, and average volume columns to *df*.
    Returns a new DataFrame (does not mutate the original).
    """
    df = df.copy()
    df["ema9"]       = calculate_ema(df["close"], config.EMA_SHORT)
    df["ema20"]      = calculate_ema(df["close"], config.EMA_LONG)
    df["vwap"]       = calculate_daily_vwap(df)
    df["atr"]        = calculate_atr(df)
    df["avg_volume"] = calculate_avg_volume(df)
    return df


# ---------------------------------------------------------------------------
# Market condition helpers
# ---------------------------------------------------------------------------

def identify_trend(df: pd.DataFrame) -> str:
    """
    Classify the current trend based on EMA alignment.

    Returns: 'uptrend' | 'downtrend' | 'sideways'
    """
    if len(df) < 2:
        return "sideways"

    last = df.iloc[-1]
    price = last["close"]

    if price <= 0:
        return "sideways"

    separation_pct = abs(last["ema9"] - last["ema20"]) / price

    if separation_pct < config.MIN_EMA_SEPARATION_PCT:
        return "sideways"

    return "uptrend" if last["ema9"] > last["ema20"] else "downtrend"


def is_sideways_market(df: pd.DataFrame) -> bool:
    """
    Return True if the market looks like it is chopping sideways.

    Uses two filters:
      1. ATR as a percentage of price is too small.
      2. EMA9 / EMA20 are too close together.
    """
    last = df.iloc[-1]
    price = last["close"]

    if price <= 0 or pd.isna(last["atr"]):
        return True

    atr_pct = last["atr"] / price
    ema_sep_pct = abs(last["ema9"] - last["ema20"]) / price

    return atr_pct < config.MIN_ATR_PCT or ema_sep_pct < config.MIN_EMA_SEPARATION_PCT


def is_volume_sufficient(df: pd.DataFrame) -> bool:
    """
    Return True if the most recent completed bar has enough volume.

    Compares the bar's volume to avg_volume * VOLUME_MULTIPLIER.
    Expects the DataFrame to have the 'avg_volume' column already set.
    """
    last = df.iloc[-1]
    avg_vol = last.get("avg_volume", np.nan)

    if pd.isna(avg_vol) or avg_vol <= 0:
        return False

    return float(last["volume"]) >= avg_vol * config.VOLUME_MULTIPLIER


def find_swing_low(df: pd.DataFrame, bars: int = None) -> float:
    """
    Return the minimum *low* over the most recent *bars* rows.
    Used for stop-loss placement below the pullback low.
    """
    n = bars or config.PULLBACK_LOW_BARS
    return float(df["low"].tail(n).min())
