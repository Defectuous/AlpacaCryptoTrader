"""
Configurable crypto signal generation. The default mode is breakout rotation;
VWAP pullback and Bollinger/Stochastic scalp modes remain available.

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
    identify_higher_timeframe_trend,
    identify_four_hour_trend,
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
    regime:       str    # "breakout-long" | "trend-long" | "trend-short" | "mean-reversion"
    risk_profile: str    # profile name used
    reason:       str
    target1:      float | None = None
    breakout_score: float = 0.0
    exit_on_trend_flip: bool = False


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
    use_closed_candle: bool | None = None,
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

    if config.STRATEGY_MODE == "4h_trend_momentum" and symbol not in config.SYMBOLS:
        logger.debug(f"{symbol}: Symbol is outside the configured trend-strategy allowlist")
        return None

    required = {"ema9", "ema20", "vwap", "atr", "avg_volume", "rsi"}
    if config.STRATEGY_MODE == "bb_stoch_volume_scalp":
        required.update({"bb_upper", "bb_lower", "stoch_k", "stoch_d", "volume_ma"})

    if not required.issubset(df.columns):
        df = add_all_indicators(df)

    df = df.dropna(subset=list(required))

    if len(df) < min_bars:
        logger.debug(f"{symbol}: Not enough valid bars after indicator calc")
        return None

    # Streamed bars are already complete; polling data may include a forming bar.
    evaluate_closed_candle = (
        config.USE_CLOSED_CANDLE
        if use_closed_candle is None
        else use_closed_candle
    )
    if evaluate_closed_candle:
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
    # Strategy-specific liquidity / sideways checks
    # -----------------------------------------------------------------------
    if config.STRATEGY_MODE != "bb_stoch_volume_scalp":
        avg_vol = float(last.get("avg_volume", 0) or 0)
        avg_notional = avg_vol * close_price
        if avg_notional < config.MIN_LIQUIDITY_VOLUME_USD:
            logger.debug(
                f"{symbol}: Avg notional ${avg_notional:.2f} < "
                f"min ${config.MIN_LIQUIDITY_VOLUME_USD:.2f}"
            )
            return None

        if (
            config.STRATEGY_MODE not in {"breakout_rotation", "4h_trend_momentum"}
            and is_sideways_market(eval_df)
        ):
            logger.debug(f"{symbol}: Market is sideways — skipping")
            return None

        if config.STRATEGY_MODE == "4h_trend_momentum":
            atr_pct = float(last["atr"]) / close_price if close_price > 0 else 0.0
            if atr_pct < config.MIN_ATR_PCT or atr_pct > config.TREND_MAX_ATR_PCT:
                logger.debug(f"{symbol}: Volatility out of range for trend entry ({atr_pct:.3%})")
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

    if config.STRATEGY_MODE == "4h_trend_momentum":
        four_hour_trend = identify_four_hour_trend(eval_df)
        if four_hour_trend not in {"uptrend", "downtrend"}:
            logger.debug(f"{symbol}: 4-hour trend is mixed; no trade")
            return None
        return _four_hour_trend_signal(
            eval_df,
            symbol,
            ask_price,
            bid_price,
            spread_pct,
            slippage_pct,
            four_hour_trend,
            profile,
        )

    if config.STRATEGY_MODE == "breakout_rotation":
        return _breakout_signal(
            eval_df,
            symbol,
            ask_price,
            bid_price,
            spread_pct,
            slippage_pct,
            profile,
        )

    if config.STRATEGY_MODE == "bb_stoch_volume_scalp":
        return _mean_reversion_scalp_signal(eval_df, symbol, ask_price, bid_price, spread_pct, profile)

    if config.STRATEGY_MODE == "htf_vwap_pullback":
        higher_trend = identify_higher_timeframe_trend(eval_df)
        if higher_trend != trend:
            logger.debug(
                f"{symbol}: Higher-timeframe trend disagrees "
                f"(15m={trend}, 1h={higher_trend}) — no trade"
            )
            return None

    if trend == "uptrend":
        return _long_signal(eval_df, symbol, ask_price, spread_pct, trend, profile)

    if trend == "downtrend" and config.ENABLE_SHORT_SELLING:
        return _short_signal(eval_df, symbol, bid_price, spread_pct, trend, profile)

    logger.debug(f"{symbol}: Trend={trend}, short_selling={config.ENABLE_SHORT_SELLING} — no trade")
    return None


def _four_hour_trend_signal(
    df: pd.DataFrame,
    symbol: str,
    ask_price: float,
    bid_price: float,
    spread_pct: float,
    slippage_pct: float,
    trend: str,
    profile: RiskProfile,
) -> Optional[TradeSignal]:
    """Enter with the 4-hour regime only when hourly RSI confirms momentum."""
    last = df.iloc[-1]
    momentum = float(last["rsi"])
    is_long = trend == "uptrend"
    if is_long and momentum < config.TREND_MOMENTUM_RSI_LONG:
        logger.debug(
            f"{symbol}: Long momentum not confirmed (RSI {momentum:.1f} < "
            f"{config.TREND_MOMENTUM_RSI_LONG:.1f})"
        )
        return None
    if not is_long and momentum > config.TREND_MOMENTUM_RSI_SHORT:
        logger.debug(
            f"{symbol}: Short momentum not confirmed (RSI {momentum:.1f} > "
            f"{config.TREND_MOMENTUM_RSI_SHORT:.1f})"
        )
        return None
    if not is_long and not config.ENABLE_SHORT_SELLING:
        logger.debug(f"{symbol}: 4-hour downtrend but short selling is disabled")
        return None

    entry = round(ask_price if is_long else bid_price, 8)
    lookback = df.tail(config.TREND_STOP_LOOKBACK_BARS)
    if is_long:
        stop = round(float(lookback["low"].min()) * (1 - config.TREND_STOP_BUFFER_PCT), 8)
        if stop <= 0 or stop >= entry:
            return None
        target = calculate_take_profit(entry, stop)
        side = "long"
    else:
        stop = round(float(lookback["high"].max()) * (1 + config.TREND_STOP_BUFFER_PCT), 8)
        if stop <= entry:
            return None
        target = calculate_short_take_profit(entry, stop)
        side = "short"

    risk = abs(entry - stop)
    rr = round(abs(target - entry) / risk, 2) if risk > 0 else 0.0
    reason = (
        f"{side.title()} {symbol}: completed 4h EMA trend={trend}, "
        f"hourly RSI={momentum:.1f} confirms momentum, stop={stop:.8f} "
        f"(distance={risk:.8f}), projected target={target:.8f} "
        f"(distance={abs(target - entry):.8f}), "
        f"spread={spread_pct:.4f}% passed (max {config.MAX_SPREAD_PCT:.4f}%), "
        f"estimated slippage={slippage_pct:.4%} passed "
        f"(max {config.MAX_SLIPPAGE_PCT:.4%}); "
        f"exit when 4h trend changes [{profile.name}]"
    )
    logger.info(
        f"4H TREND {side.upper()} ▶ {symbol} | RSI={momentum:.1f} "
        f"entry={entry:.6f} stop={stop:.6f}"
    )
    return TradeSignal(
        symbol=symbol,
        side=side,
        entry=entry,
        stop=stop,
        target=target,
        rr=rr,
        trend=trend,
        regime=f"4h-trend-{side}",
        risk_profile=profile.name,
        reason=reason,
        exit_on_trend_flip=True,
    )


def _breakout_signal(
    df: pd.DataFrame,
    symbol: str,
    ask_price: float,
    bid_price: float,
    spread_pct: float,
    slippage_pct: float,
    profile: RiskProfile,
) -> Optional[TradeSignal]:
    """Return a confirmed long breakout from the most recent closed bar."""
    if (
        config.BREAKOUT_STOP_ATR_BUFFER <= 0
        or config.BREAKOUT_TP1_R <= 0
        or config.BREAKOUT_TP2_R <= config.BREAKOUT_TP1_R
    ):
        logger.error("Breakout configuration invalid: require stop buffer > 0 and 0 < TP1 R < TP2 R")
        return None

    range_bars = config.BREAKOUT_RANGE_BARS
    if len(df) < range_bars + 1:
        return None

    prior_range = df.iloc[-(range_bars + 1):-1]
    last = df.iloc[-1]
    prior_high = float(prior_range["high"].max())
    close = float(last["close"])
    atr = float(last["atr"])
    avg_volume = float(last["avg_volume"])
    volume = float(last["volume"])

    if prior_high <= 0 or atr <= 0 or close <= 0 or avg_volume <= 0:
        return None

    atr_pct = atr / close
    if atr_pct < config.MIN_ATR_PCT or atr_pct > config.BREAKOUT_MAX_ATR_PCT:
        logger.debug(f"{symbol}: Breakout volatility out of range ({atr_pct:.3%})")
        return None

    volume_ratio = volume / avg_volume
    if volume_ratio < config.BREAKOUT_VOLUME_MULTIPLIER:
        logger.debug(
            f"{symbol}: Breakout volume {volume_ratio:.2f}x is below "
            f"{config.BREAKOUT_VOLUME_MULTIPLIER:.2f}x average"
        )
        return None

    # The closed candle confirms the break; a live bid above the range high
    # confirms that price has held above it for at least one subsequent tick.
    if close <= prior_high or bid_price <= prior_high:
        logger.debug(f"{symbol}: No confirmed range break or live hold")
        return None

    extension_atr = (ask_price - prior_high) / atr
    if extension_atr > config.BREAKOUT_MAX_EXTENSION_ATR:
        logger.debug(f"{symbol}: Breakout is extended ({extension_atr:.2f} ATR)")
        return None

    entry = round(ask_price, 8)
    stop = round(prior_high - atr * config.BREAKOUT_STOP_ATR_BUFFER, 8)
    if stop <= 0 or stop >= entry:
        logger.debug(f"{symbol}: Invalid breakout invalidation level {stop:.8f}")
        return None

    risk = entry - stop
    target1 = round(entry + risk * config.BREAKOUT_TP1_R, 8)
    target2 = round(entry + risk * config.BREAKOUT_TP2_R, 8)
    if target1 <= entry or target2 <= target1:
        logger.debug(f"{symbol}: Breakout target prices are not strictly increasing")
        return None
    rr = round((target2 - entry) / risk, 2)
    breakout_score = ((close - prior_high) / atr) * volume_ratio
    reason = (
        f"Fresh range breakout: close={close:.8f} above prior high={prior_high:.8f}, "
        f"bid held above range, volume={volume_ratio:.2f}x, ATR={atr_pct:.2%}, "
        f"score={breakout_score:.4f}, invalidation={stop:.8f}, "
        f"risk distance={risk:.8f}, target distances={target1 - entry:.8f}/{target2 - entry:.8f}, "
        f"TP1={target1:.8f} ({config.BREAKOUT_TP1_R:.2f}R), "
        f"TP2={target2:.8f} ({config.BREAKOUT_TP2_R:.2f}R), "
        f"spread={spread_pct:.4f}% (max {config.MAX_SPREAD_PCT:.4f}%), "
        f"estimated slippage={slippage_pct*100:.4f}% "
        f"(max {config.MAX_SLIPPAGE_PCT*100:.4f}%) [checks passed, {profile.name}]"
    )
    logger.info(
        f"BREAKOUT SIGNAL | {symbol} entry={entry:.8f} stop={stop:.8f} "
        f"TP1={target1:.8f} TP2={target2:.8f} score={breakout_score:.4f}"
    )
    return TradeSignal(
        symbol=symbol,
        side="long",
        entry=entry,
        stop=stop,
        target=target2,
        rr=rr,
        trend="uptrend",
        regime="breakout-long",
        risk_profile=profile.name,
        reason=reason,
        target1=target1,
        breakout_score=breakout_score,
    )


def _mean_reversion_scalp_signal(
    df: pd.DataFrame,
    symbol: str,
    ask_price: float,
    bid_price: float,
    spread_pct: float,
    profile: RiskProfile,
) -> Optional[TradeSignal]:
    """Bollinger Bands + Stochastic RSI + volume mean-reversion scalp.

    Long: price touches/breaks below lower band, %K > %D, %K rising from oversold,
    bar closes green, volume is above its moving average.

    Short: price touches/breaks above upper band, %K < %D, %K falling from overbought,
    bar closes red, volume is above its moving average.
    """
    last = df.iloc[-1]
    prev = df.iloc[-2]

    required_cols = {"bb_lower", "bb_upper", "stoch_k", "stoch_d", "volume_ma", "open", "close", "low", "high"}
    if not required_cols.issubset(df.columns):
        logger.debug(f"{symbol}: Scalper indicators not available")
        return None

    volume = float(last["volume"])
    volume_ma = float(last["volume_ma"])
    min_volume = volume_ma * config.SCALP_VOLUME_MIN_RATIO
    if pd.isna(volume_ma) or volume_ma <= 0 or volume < min_volume:
        logger.debug(
            f"{symbol}: Volume MA filter failed ({volume:.2f} < {min_volume:.2f} = {volume_ma:.2f} * {config.SCALP_VOLUME_MIN_RATIO:.2f})"
        )
        return None

    curr_k = float(last["stoch_k"])
    curr_d = float(last["stoch_d"])
    prev_k = float(prev["stoch_k"])
    prev_d = float(prev["stoch_d"])
    lower_band = float(last["bb_lower"])
    upper_band = float(last["bb_upper"])
    bar_green = float(last["close"]) > float(last["open"])
    bar_red = float(last["close"]) < float(last["open"])

    long_ok = (
        float(last["low"]) <= lower_band
        and curr_k > curr_d
        and curr_k >= prev_k
        and (prev_k <= config.SCALP_OVERSOLD_LEVEL or curr_k >= 50.0)
        and bar_green
    )

    short_ok = (
        float(last["high"]) >= upper_band
        and curr_k < curr_d
        and curr_k <= prev_k
        and (prev_k >= config.SCALP_OVERBOUGHT_LEVEL or curr_k <= 50.0)
        and bar_red
    )

    if not (long_ok or short_ok):
        logger.debug(
            f"{symbol}: Scalper filter failed | volume={volume:.2f}/{volume_ma:.2f} "
            f"K={curr_k:.1f} D={curr_d:.1f} prevK={prev_k:.1f} prevD={prev_d:.1f} "
            f"low={float(last['low']):.4f} lower={lower_band:.4f} high={float(last['high']):.4f} upper={upper_band:.4f}"
        )
        return None

    if long_ok:
        entry = round(ask_price, 8)
        stop = round(lower_band * (1.0 - config.SCALP_STOP_BUFFER), 8)
        if stop >= entry:
            logger.debug(f"{symbol}: Long scalp stop {stop:.6f} >= entry {entry:.6f}")
            return None
        target = calculate_take_profit(entry, stop, rr_ratio=config.SCALP_RR_RATIO)
        risk = entry - stop
        reward = target - entry
        rr = round(reward / risk, 2) if risk > 0 else 0.0
        reason = (
            f"Long {symbol}: Bollinger lower-band touch, %K {curr_k:.1f} > %D {curr_d:.1f}, "
            f"recovery from oversold ({prev_k:.1f} -> {curr_k:.1f}), volume {volume:.2f} >= MA {volume_ma:.2f}, "
            f"green bar, stop={stop:.6f}, R:R={rr:.2f} [{profile.name}]"
        )
        logger.info(f"LONG SCALP SIGNAL ▶ {symbol} | entry={entry:.6f} stop={stop:.6f} target={target:.6f} R:R={rr:.2f}")
        return TradeSignal(
            symbol=symbol, side="long", entry=entry, stop=stop, target=target,
            rr=rr, trend="mean-reversion", regime="mean-reversion", risk_profile=profile.name, reason=reason,
        )

    entry = round(bid_price, 8)
    stop = round(upper_band * (1.0 + config.SCALP_STOP_BUFFER), 8)
    if stop <= entry:
        logger.debug(f"{symbol}: Short scalp stop {stop:.6f} <= entry {entry:.6f}")
        return None
    target = calculate_short_take_profit(entry, stop, rr_ratio=config.SCALP_RR_RATIO)
    risk = stop - entry
    reward = entry - target
    rr = round(reward / risk, 2) if risk > 0 else 0.0
    reason = (
        f"Short {symbol}: Bollinger upper-band touch, %K {curr_k:.1f} < %D {curr_d:.1f}, "
        f"drop from overbought ({prev_k:.1f} -> {curr_k:.1f}), volume {volume:.2f} >= MA {volume_ma:.2f}, "
        f"red bar, stop={stop:.6f}, R:R={rr:.2f} [{profile.name}]"
    )
    logger.info(f"SHORT SCALP SIGNAL ▶ {symbol} | entry={entry:.6f} stop={stop:.6f} target={target:.6f} R:R={rr:.2f}")
    return TradeSignal(
        symbol=symbol, side="short", entry=entry, stop=stop, target=target,
        rr=rr, trend="mean-reversion", regime="mean-reversion", risk_profile=profile.name, reason=reason,
    )


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

    if config.STRATEGY_MODE == "rsi_vwap_pullback":
        reset_rsi = float(prev["rsi"])
        confirm_rsi = float(last["rsi"])
        if not (
            reset_rsi <= config.RSI_LONG_RESET_MAX
            and confirm_rsi >= config.RSI_LONG_CONFIRM_MIN
        ):
            logger.debug(
                f"{symbol}: RSI reset/reclaim missing "
                f"(reset={reset_rsi:.1f}, confirm={confirm_rsi:.1f})"
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

    rsi_reason = (
        f"RSI reset/reclaim ({prev['rsi']:.1f}->{last['rsi']:.1f}), "
        if config.STRATEGY_MODE == "rsi_vwap_pullback" else ""
    )
    reason = (
        f"Long {symbol}: price above VWAP ({vwap:.4f}), "
        f"EMA9 ({last['ema9']:.4f}) > EMA20 ({last['ema20']:.4f}), "
        f"VWAP pullback+bounce, volume confirmed, "
        f"{rsi_reason}"
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
