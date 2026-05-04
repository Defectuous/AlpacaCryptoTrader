"""
VWAP Pullback + Breakdown Strategy — signal generation.

Long setup (uptrend):
  Price pulls back to VWAP in an uptrend (EMA9 > EMA20), bounces with volume,
  stop below pullback swing low, target ≥ REWARD_RISK_TARGET × risk.

Short setup (downtrend, requires ENABLE_SHORT_SELLING=True):
  Price rallies to VWAP in a downtrend (EMA9 < EMA20), rejects with volume,
  stop above pullback swing high, target ≥ REWARD_RISK_TARGET × risk.

No-trade regime:
  Sideways market, spread too wide, volume insufficient, slippage estimate
  exceeds MAX_SLIPPAGE_PCT, or average volume-notional below MIN_LIQUIDITY_VOLUME_USD.

All signals are evaluated on fully closed candles (USE_CLOSED_CANDLE=True).
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
from trader.risk_manager import (
    calculate_take_profit,
    calculate_short_take_profit,
    select_risk_profile,
    RiskProfile,
)


@dataclass
class TradeSignal:
    symbol:       str
    side:         str    # "long" or "short"
    entry:        float
    stop:         float
    target:       float
    rr:           float
    trend:        str
    regime:       str    # "trend-long" | "trend-short" | "mean-reversion"
    risk_profile: str    # profile name used
    reason:       str


def _find_swing_high(df: pd.DataFrame, bars: int = None) -> float:
    """Return the maximum high over the most recent bars (for short stop placement)."""
    n = bars or config.PULLBACK_LOW_BARS
    return float(df["high"].tail(n).max())


def detect_signal(
    df: pd.DataFrame,
    symbol: str,
    ask_price: float,
    bid_price: float,
    spread_pct: float,
) -> Optional[TradeSignal]:
    """
    Scan *df* for a VWAP pullback/rejection setup and return a TradeSignal or None.

    Parameters
    ----------
    df         : Raw OHLCV DataFrame from market_data.get_bars()
    symbol     : e.g. "BTC/USD"
    ask_price  : Current ask (used as limit entry for longs)
    bid_price  : Current bid (used as limit entry for shorts)
    spread_pct : Current bid/ask spread as a percentage of mid price
    """
    min_bars = config.EMA_LONG + config.PULLBACK_LOW_BARS + 5

    if df is None or len(df) < min_bars:
        logger.debug(f"{symbol}: Not enough bars ({len(df) if df is not None else 0} < {min_bars})")
        return None

    df = add_all_indicators(df)
    df = df.dropna(subset=["ema9", "ema20", "vwap", "atr", "avg_volume"])

    if len(df) < min_bars:
        logger.debug(f"{symbol}: Not enough valid bars after indicator calc")
        return None

    # Use only fully closed candles when configured
    if config.USE_CLOSED_CANDLE:
        eval_df = df.iloc[:-1]   # drop the still-forming bar
    else:
        eval_df = df

    if len(eval_df) < min_bars:
        logger.debug(f"{symbol}: Not enough closed bars")
        return None

    last = eval_df.iloc[-1]
    prev = eval_df.iloc[-2]

    # -----------------------------------------------------------------------
    # Spread filter
    # -----------------------------------------------------------------------
    if spread_pct > config.MAX_SPREAD_PCT:
        logger.debug(f"{symbol}: Spread too wide ({spread_pct:.3f}% > {config.MAX_SPREAD_PCT}%)")
        return None

    # -----------------------------------------------------------------------
    # Slippage estimate filter — reject if ask/bid diverges from close too much
    # -----------------------------------------------------------------------
    close_price = float(last["close"])
    slippage_pct = abs(ask_price - close_price) / close_price if close_price > 0 else 1.0
    if slippage_pct > config.MAX_SLIPPAGE_PCT:
        logger.debug(
            f"{symbol}: Estimated slippage {slippage_pct*100:.3f}% > "
            f"max {config.MAX_SLIPPAGE_PCT*100:.3f}%"
        )
        return None

    # -----------------------------------------------------------------------
    # Liquidity filter — average volume × price must meet minimum notional
    # -----------------------------------------------------------------------
    avg_vol = float(last.get("avg_volume", 0) or 0)
    avg_notional = avg_vol * close_price
    if avg_notional < config.MIN_LIQUIDITY_VOLUME_USD:
        logger.debug(
            f"{symbol}: Avg notional ${avg_notional:.2f} < "
            f"min ${config.MIN_LIQUIDITY_VOLUME_USD:.2f}"
        )
        return None

    # -----------------------------------------------------------------------
    # Sideways market filter
    # -----------------------------------------------------------------------
    if is_sideways_market(eval_df):
        logger.debug(f"{symbol}: Market is sideways — skipping")
        return None

    # -----------------------------------------------------------------------
    # Risk profile auto-selection
    # -----------------------------------------------------------------------
    atr_pct = float(last["atr"]) / close_price if close_price > 0 else 0.0
    profile: RiskProfile = select_risk_profile(atr_pct)

    # -----------------------------------------------------------------------
    # Trend detection → route to long or short signal path
    # -----------------------------------------------------------------------
    trend = identify_trend(eval_df)

    if trend == "uptrend":
        return _long_signal(eval_df, symbol, ask_price, spread_pct, trend, profile)

    if trend == "downtrend" and config.ENABLE_SHORT_SELLING:
        return _short_signal(eval_df, symbol, bid_price, spread_pct, trend, profile)

    logger.debug(f"{symbol}: Trend={trend}, short_selling={config.ENABLE_SHORT_SELLING} — no trade")
    return None


# ---------------------------------------------------------------------------
# Long signal path
# ---------------------------------------------------------------------------

def _long_signal(
    df: pd.DataFrame,
    symbol: str,
    ask_price: float,
    spread_pct: float,
    trend: str,
    profile: RiskProfile,
) -> Optional[TradeSignal]:
    last = df.iloc[-1]
    prev = df.iloc[-2]

    # Volume on the completed bounce bar
    bounce_df = df.iloc[:-1]
    if not is_volume_sufficient(bounce_df):
        logger.debug(
            f"{symbol}: Volume insufficient on bounce bar "
            f"({bounce_df.iloc[-1]['volume']:.2f} vs avg {bounce_df.iloc[-1]['avg_volume']:.2f})"
        )
        return None

    vwap      = float(last["vwap"])
    close     = float(last["close"])
    prev_vwap = float(prev["vwap"])
    prev_low  = float(prev["low"])

    if vwap <= 0:
        return None

    dist_pct = abs(close - vwap) / vwap
    prev_touched_vwap = (
        prev_low <= prev_vwap * 1.001
        or abs(prev["close"] - prev_vwap) / prev_vwap <= config.VWAP_PULLBACK_THRESHOLD
    )
    at_or_near_vwap = dist_pct <= config.VWAP_PULLBACK_THRESHOLD

    if not (prev_touched_vwap or at_or_near_vwap):
        logger.debug(
            f"{symbol}: No VWAP pullback — close={close:.4f} vwap={vwap:.4f} ({dist_pct*100:.3f}%)"
        )
        return None

    if close < vwap * 0.9985:
        logger.debug(f"{symbol}: Close {close:.4f} below VWAP {vwap:.4f} — breakdown")
        return None

    entry     = round(ask_price, 8)
    swing_low = find_swing_low(df)
    stop      = round(swing_low * 0.999, 8)

    if stop >= entry:
        logger.debug(f"{symbol}: Long stop {stop:.6f} >= entry {entry:.6f}")
        return None

    target = calculate_take_profit(entry, stop)
    risk   = entry - stop
    reward = target - entry
    rr     = round(reward / risk, 2) if risk > 0 else 0.0

    reason = (
        f"Long {symbol}: price above VWAP ({vwap:.4f}), "
        f"EMA9 ({last['ema9']:.4f}) > EMA20 ({last['ema20']:.4f}), "
        f"VWAP pullback+bounce, volume confirmed, "
        f"stop below pullback low ({stop:.6f}), R:R={rr:.2f} [{profile.name}]"
    )
    logger.info(f"LONG SIGNAL ▶ {symbol} | entry={entry:.6f} stop={stop:.6f} target={target:.6f} R:R={rr:.2f}")

    return TradeSignal(
        symbol=symbol, side="long", entry=entry, stop=stop, target=target,
        rr=rr, trend=trend, regime="trend-long", risk_profile=profile.name, reason=reason,
    )


# ---------------------------------------------------------------------------
# Short signal path
# ---------------------------------------------------------------------------

def _short_signal(
    df: pd.DataFrame,
    symbol: str,
    bid_price: float,
    spread_pct: float,
    trend: str,
    profile: RiskProfile,
) -> Optional[TradeSignal]:
    last = df.iloc[-1]
    prev = df.iloc[-2]

    # Volume on the completed rejection bar
    bounce_df = df.iloc[:-1]
    if not is_volume_sufficient(bounce_df):
        logger.debug(f"{symbol}: Volume insufficient on rejection bar")
        return None

    vwap      = float(last["vwap"])
    close     = float(last["close"])
    prev_vwap = float(prev["vwap"])
    prev_high = float(prev["high"])

    if vwap <= 0:
        return None

    dist_pct = abs(close - vwap) / vwap
    prev_touched_vwap = (
        prev_high >= prev_vwap * 0.999
        or abs(prev["close"] - prev_vwap) / prev_vwap <= config.VWAP_PULLBACK_THRESHOLD
    )
    at_or_near_vwap = dist_pct <= config.VWAP_PULLBACK_THRESHOLD

    if not (prev_touched_vwap or at_or_near_vwap):
        logger.debug(
            f"{symbol}: No VWAP rejection — close={close:.4f} vwap={vwap:.4f} ({dist_pct*100:.3f}%)"
        )
        return None

    # For shorts: close must still be near or below VWAP (rejection confirmed)
    if close > vwap * 1.0015:
        logger.debug(f"{symbol}: Close {close:.4f} above VWAP {vwap:.4f} — breakout, not rejection")
        return None

    entry      = round(bid_price, 8)
    swing_high = _find_swing_high(df)
    stop       = round(swing_high * 1.001, 8)   # 0.1% buffer above swing high

    if stop <= entry:
        logger.debug(f"{symbol}: Short stop {stop:.6f} <= entry {entry:.6f}")
        return None

    target = calculate_short_take_profit(entry, stop)
    risk   = stop - entry
    reward = entry - target
    rr     = round(reward / risk, 2) if risk > 0 else 0.0

    reason = (
        f"Short {symbol}: price below VWAP ({vwap:.4f}), "
        f"EMA9 ({last['ema9']:.4f}) < EMA20 ({last['ema20']:.4f}), "
        f"VWAP rally+rejection, volume confirmed, "
        f"stop above swing high ({stop:.6f}), R:R={rr:.2f} [{profile.name}]"
    )
    logger.info(f"SHORT SIGNAL ▶ {symbol} | entry={entry:.6f} stop={stop:.6f} target={target:.6f} R:R={rr:.2f}")

    return TradeSignal(
        symbol=symbol, side="short", entry=entry, stop=stop, target=target,
        rr=rr, trend=trend, regime="trend-short", risk_profile=profile.name, reason=reason,
    )
