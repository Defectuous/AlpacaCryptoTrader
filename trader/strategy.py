"""
VWAP Pullback Strategy — signal generation.

Setup criteria (long only, spot crypto):
  1. Trend     : EMA9 > EMA20 (uptrend)
  2. Not sideways: ATR and EMA separation both above thresholds
  3. Volume    : bounce bar volume >= avg_volume * VOLUME_MULTIPLIER
  4. Spread    : bid/ask spread within MAX_SPREAD_PCT
  5. Pullback  : most recent bars show price touching or briefly crossing VWAP
  6. Bounce    : current close is back above VWAP

Trade in one sentence:
  "Price is above VWAP in an uptrend (EMA9 > EMA20), pulled back to VWAP,
   volume increased on the bounce, stop below the pullback low, target at
   least 1.5x the risk."
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd
from loguru import logger

import config
from trader.indicators import (
    add_all_indicators,
    identify_trend,
    is_sideways_market,
    is_volume_sufficient,
    find_swing_low,
)
from trader.risk_manager import calculate_take_profit


@dataclass
class TradeSignal:
    symbol:  str
    entry:   float   # limit order price (at ask)
    stop:    float   # stop-loss price
    target:  float   # take-profit price
    rr:      float   # actual reward-to-risk ratio
    trend:   str
    reason:  str     # one-sentence human-readable justification


def detect_signal(
    df: pd.DataFrame,
    symbol: str,
    ask_price: float,
    spread_pct: float,
) -> Optional[TradeSignal]:
    """
    Scan *df* for a VWAP pullback setup and return a TradeSignal or None.

    Parameters
    ----------
    df         : Raw OHLCV DataFrame from market_data.get_bars()
    symbol     : e.g. "BTC/USD"
    ask_price  : Current ask (used as limit entry price)
    spread_pct : Current bid/ask spread as a percentage of mid price
    """
    min_bars = config.EMA_LONG + config.PULLBACK_LOW_BARS + 5

    if df is None or len(df) < min_bars:
        logger.debug(f"{symbol}: Not enough bars ({len(df) if df is not None else 0} < {min_bars})")
        return None

    # -----------------------------------------------------------------------
    # Add all indicators
    # -----------------------------------------------------------------------
    df = add_all_indicators(df)
    df = df.dropna(subset=["ema9", "ema20", "vwap", "atr", "avg_volume"])

    if len(df) < min_bars:
        logger.debug(f"{symbol}: Not enough valid bars after indicator calc")
        return None

    last = df.iloc[-1]
    prev = df.iloc[-2]

    # -----------------------------------------------------------------------
    # No-trade rule checks
    # -----------------------------------------------------------------------

    # 1. Spread
    if spread_pct > config.MAX_SPREAD_PCT:
        logger.debug(f"{symbol}: Spread too wide ({spread_pct:.3f}% > {config.MAX_SPREAD_PCT}%)")
        return None

    # 2. Sideways market
    if is_sideways_market(df):
        logger.debug(f"{symbol}: Market is sideways — skipping")
        return None

    # 3. Trend direction
    trend = identify_trend(df)
    if trend != "uptrend":
        logger.debug(f"{symbol}: Not in uptrend (trend={trend})")
        return None

    # 4. Volume on the previous completed bar (the bounce bar)
    #    Use all rows except the last (which may still be forming)
    bounce_df = df.iloc[:-1]
    if not is_volume_sufficient(bounce_df):
        logger.debug(
            f"{symbol}: Volume insufficient "
            f"({bounce_df.iloc[-1]['volume']:.2f} vs "
            f"avg {bounce_df.iloc[-1]['avg_volume']:.2f})"
        )
        return None

    # -----------------------------------------------------------------------
    # VWAP pullback detection
    # -----------------------------------------------------------------------
    vwap      = float(last["vwap"])
    close     = float(last["close"])
    prev_vwap = float(prev["vwap"])
    prev_low  = float(prev["low"])

    if vwap <= 0:
        logger.debug(f"{symbol}: VWAP is zero or negative")
        return None

    dist_pct = abs(close - vwap) / vwap

    # Previous bar dipped to or below VWAP (the actual pullback)
    prev_touched_vwap = (
        prev_low <= prev_vwap * 1.001        # low was at/below VWAP
        or abs(prev["close"] - prev_vwap) / prev_vwap <= config.VWAP_PULLBACK_THRESHOLD
    )

    # Current bar is now near or back above VWAP (the bounce)
    at_or_near_vwap = dist_pct <= config.VWAP_PULLBACK_THRESHOLD

    if not (prev_touched_vwap or at_or_near_vwap):
        logger.debug(
            f"{symbol}: No VWAP pullback — price {close:.4f} "
            f"vs VWAP {vwap:.4f} ({dist_pct*100:.3f}%)"
        )
        return None

    # Price must be above VWAP — we want a bounce, not a breakdown
    if close < vwap * 0.9985:     # allow a tiny 0.15 % buffer
        logger.debug(f"{symbol}: Close {close:.4f} is below VWAP {vwap:.4f} — breakdown, not bounce")
        return None

    # -----------------------------------------------------------------------
    # Trade parameter calculation
    # -----------------------------------------------------------------------
    entry  = round(ask_price, 8)
    swing_low = find_swing_low(df)
    stop   = round(swing_low * 0.999, 8)   # 0.1 % buffer below pullback low

    if stop >= entry:
        logger.debug(f"{symbol}: Stop {stop:.6f} >= entry {entry:.6f} — invalid geometry")
        return None

    target = calculate_take_profit(entry, stop)
    risk   = entry - stop
    reward = target - entry
    rr     = round(reward / risk, 2) if risk > 0 else 0.0

    reason = (
        f"Bought {symbol} because price was above VWAP ({vwap:.4f}), "
        f"EMA9 ({last['ema9']:.4f}) > EMA20 ({last['ema20']:.4f}), "
        f"price pulled back to VWAP, volume increased on the bounce, "
        f"stop below pullback low ({stop:.6f}), R:R={rr:.2f}"
    )

    logger.info(f"SIGNAL ▶ {symbol} | entry={entry:.6f} stop={stop:.6f} target={target:.6f} R:R={rr:.2f}")

    return TradeSignal(
        symbol=symbol,
        entry=entry,
        stop=stop,
        target=target,
        rr=rr,
        trend=trend,
        reason=reason,
    )
