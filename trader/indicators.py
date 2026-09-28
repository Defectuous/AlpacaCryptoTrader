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


def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder-style RSI using exponentially smoothed gains and losses."""
    delta = series.diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)
    average_gain = gains.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    average_loss = losses.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    relative_strength = average_gain / average_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + relative_strength))
    return rsi.fillna(100.0).where(average_loss.ne(0), 100.0)


def calculate_bollinger_bands(
    series: pd.Series,
    length: int = 20,
    std_dev: float = 2.0,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Return (middle, upper, lower) Bollinger Band series."""
    middle = series.rolling(window=length, min_periods=length).mean()
    std = series.rolling(window=length, min_periods=length).std(ddof=0)
    upper = middle + (std_dev * std)
    lower = middle - (std_dev * std)
    return middle, upper, lower


def calculate_stochastic_rsi(
    series: pd.Series,
    k_period: int = 3,
    d_period: int = 3,
    rsi_period: int = 14,
    stoch_period: int = 14,
) -> tuple[pd.Series, pd.Series]:
    """Return Stochastic RSI %K and %D series."""
    rsi = calculate_rsi(series, period=rsi_period)
    rsi_low = rsi.rolling(window=stoch_period, min_periods=stoch_period).min()
    rsi_high = rsi.rolling(window=stoch_period, min_periods=stoch_period).max()
    stoch_rsi = (rsi - rsi_low) / (rsi_high - rsi_low + 1e-9)
    stoch_k = stoch_rsi.rolling(window=k_period, min_periods=k_period).mean() * 100.0
    stoch_d = stoch_k.rolling(window=d_period, min_periods=d_period).mean()
    return stoch_k, stoch_d


def identify_higher_timeframe_trend(df: pd.DataFrame) -> str:
    """Return a causal hourly EMA trend for the latest completed lower-timeframe bar."""
    if not isinstance(df.index, pd.DatetimeIndex) or len(df) < 4:
        return "sideways"

    hourly = (
        df[["open", "high", "low", "close", "volume"]]
        .resample(config.HTF_TIMEFRAME, label="right", closed="right")
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
        )
        .dropna(subset=["close"])
    )
    if len(hourly) <= config.HTF_EMA_SLOW:
        return "sideways"

    hourly["ema_fast"] = calculate_ema(hourly["close"], config.HTF_EMA_FAST)
    hourly["ema_slow"] = calculate_ema(hourly["close"], config.HTF_EMA_SLOW)

    # Shift the regime by one hourly candle so the active candle cannot leak
    # its close into the signal decision.
    regime = hourly[["ema_fast", "ema_slow"]].shift(1).iloc[-1]
    if pd.isna(regime["ema_fast"]) or pd.isna(regime["ema_slow"]):
        return "sideways"
    if regime["ema_fast"] > regime["ema_slow"]:
        return "uptrend"
    if regime["ema_fast"] < regime["ema_slow"]:
        return "downtrend"
    return "sideways"


def identify_four_hour_trend(df: pd.DataFrame) -> str:
    """Classify the latest completed 4-hour EMA regime from lower-timeframe bars."""
    if not isinstance(df.index, pd.DatetimeIndex) or df.empty:
        return "sideways"

    bar_minutes = {
        "1Min": 1,
        "5Min": 5,
        "15Min": 15,
        "1Hour": 60,
    }.get(config.BAR_TIMEFRAME)
    if bar_minutes is None:
        return "sideways"

    four_hour = (
        df[["open", "high", "low", "close", "volume"]]
        .resample(config.TREND_TIMEFRAME, label="right", closed="left")
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
        )
        .dropna(subset=["close"])
    )
    completed_through = df.index[-1] + pd.Timedelta(minutes=bar_minutes)
    four_hour = four_hour.loc[four_hour.index <= completed_through]
    if len(four_hour) < config.TREND_HTF_EMA_SLOW:
        return "sideways"

    fast = calculate_ema(four_hour["close"], config.TREND_HTF_EMA_FAST).iloc[-1]
    slow = calculate_ema(four_hour["close"], config.TREND_HTF_EMA_SLOW).iloc[-1]
    price = float(four_hour["close"].iloc[-1])
    if pd.isna(fast) or pd.isna(slow) or price <= 0:
        return "sideways"
    if abs(fast - slow) / price < config.TREND_MIN_EMA_SEPARATION_PCT:
        return "sideways"
    if fast > slow:
        return "uptrend"
    if fast < slow:
        return "downtrend"
    return "sideways"


# ---------------------------------------------------------------------------
# Composite indicator builder
# ---------------------------------------------------------------------------

def add_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add the standard trend indicators plus the Bollinger/Stochastic/Volume
    columns used by the mean-reversion scalp mode.
    Returns a new DataFrame (does not mutate the original).
    """
    df = df.copy()
    df["ema9"]       = calculate_ema(df["close"], config.EMA_SHORT)
    df["ema20"]      = calculate_ema(df["close"], config.EMA_LONG)
    df["vwap"]       = calculate_daily_vwap(df)
    df["atr"]        = calculate_atr(df)
    df["avg_volume"] = calculate_avg_volume(df)
    df["rsi"]        = calculate_rsi(df["close"], config.RSI_PERIOD)

    bb_middle, bb_upper, bb_lower = calculate_bollinger_bands(
        df["close"],
        length=config.SCALP_BB_LENGTH,
        std_dev=config.SCALP_BB_STD,
    )
    df["bb_middle"] = bb_middle
    df["bb_upper"] = bb_upper
    df["bb_lower"] = bb_lower

    stoch_k, stoch_d = calculate_stochastic_rsi(
        df["close"],
        k_period=config.SCALP_STOCH_K,
        d_period=config.SCALP_STOCH_D,
        rsi_period=config.SCALP_STOCH_RSI,
        stoch_period=config.SCALP_STOCH_LENGTH,
    )
    df["stoch_k"] = stoch_k
    df["stoch_d"] = stoch_d
    df["volume_ma"] = df["volume"].rolling(window=config.SCALP_VOLUME_MA, min_periods=config.SCALP_VOLUME_MA).mean()
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
