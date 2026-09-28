"""
Walk-forward bar-by-bar backtesting engine.

Replays historical OHLCV data through the *same* signal detection and risk
sizing logic used by the live bot, so results closely mirror what would have
been produced in production.

Fill model
----------
- Limit entry (long) : fills if next bar's low  <= signal.entry
- Limit entry (short): fills if next bar's high >= signal.entry
- Take-profit        : exit at signal.target when bar high (long) / low (short) touches it
- Stop-loss          : exit at signal.stop   when bar low  (long) / high (short) touches it
- If SL and TP are both reached in the same bar, SL is taken (conservative).
- Gap opens beyond stop are filled at bar open, not at stop price.
- Positions still open at end of data are closed at last bar's close.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import pandas as pd
from loguru import logger

import config
from trader.indicators import add_all_indicators, identify_four_hour_trend
from trader.strategy import detect_signal
from trader.risk_manager import (
    calculate_position_qty,
    STANDARD_PROFILE,
    HIGH_RISK_PROFILE,
    validate_setup,
)

# Synthetic bid/ask spread used when replaying historical bars (no live quote).
# 0.10 % is conservative for liquid crypto pairs — errs toward less trading.
_BACKTEST_SPREAD_PCT: float = 0.10


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class BacktestTrade:
    symbol:       str
    side:         str        # "long" | "short"
    entry:        float
    stop:         float
    target:       float
    qty:          float
    entry_bar:    int
    entry_time:   datetime
    regime:       str
    risk_profile: str
    reason:       str
    target1:      Optional[float] = None
    exit_on_trend_flip: bool = False
    remaining_qty: Optional[float] = None
    tp1_hit:      bool = False
    exit_time:    Optional[datetime] = None
    exit_price:   Optional[float]    = None
    exit_reason:  str                = ""
    pnl_usd:      float              = 0.0
    equity_after: float              = 0.0

    @property
    def is_win(self) -> bool:
        return self.pnl_usd > 0

    @property
    def r_multiple(self) -> float:
        """PnL expressed as a multiple of the initial dollar risk."""
        risk = abs(self.entry - self.stop) * self.qty
        return self.pnl_usd / risk if risk > 0 else 0.0


# ---------------------------------------------------------------------------
# Fill helpers
# ---------------------------------------------------------------------------

def _simulate_limit_fill(signal, bar: pd.Series) -> bool:
    """Return True if the bar would fill the limit entry order."""
    if signal.side == "long":
        return float(bar["low"]) <= signal.entry
    return float(bar["high"]) >= signal.entry


def _check_exit(trade: BacktestTrade, bar: pd.Series) -> Optional[dict]:
    """
    Determine whether the bar triggers a TP or SL exit.

    Returns a dict with ``exit_price``, ``exit_reason``, and ``pnl_usd``,
    or None if the trade is still open.

    Priority: gap-open through stop → stop-loss → take-profit.
    """
    bar_open  = float(bar["open"])
    bar_low   = float(bar["low"])
    bar_high  = float(bar["high"])

    exit_price: Optional[float] = None
    exit_reason = ""

    remaining_qty = trade.remaining_qty if trade.remaining_qty is not None else trade.qty

    if trade.side == "long":
        if bar_open <= trade.stop:           # gapped down through stop
            exit_price  = bar_open
            exit_reason = "sl-gap"
        elif bar_low <= trade.stop:          # stop touched intrabar
            exit_price  = trade.stop
            exit_reason = "sl"
        elif not trade.exit_on_trend_flip and bar_high >= trade.target:  # final target hit
            if trade.target1 is not None and not trade.tp1_hit:
                first_qty = min(trade.qty / 2.0, remaining_qty)
                pnl = (
                    (trade.target1 - trade.entry) * first_qty
                    + (trade.target - trade.entry) * (remaining_qty - first_qty)
                )
                return {
                    "exit_price": trade.target,
                    "exit_reason": "tp1+tp2",
                    "pnl_usd": pnl,
                    "qty_closed": remaining_qty,
                    "partial": False,
                }
            exit_price  = trade.target
            exit_reason = "tp2" if trade.target1 is not None else "tp"
        elif (
            not trade.exit_on_trend_flip
            and trade.target1 is not None
            and not trade.tp1_hit
            and bar_high >= trade.target1
        ):
            first_qty = min(trade.qty / 2.0, remaining_qty)
            return {
                "exit_price": trade.target1,
                "exit_reason": "tp1",
                "pnl_usd": (trade.target1 - trade.entry) * first_qty,
                "qty_closed": first_qty,
                "partial": True,
            }
    else:  # short
        if bar_open >= trade.stop:           # gapped up through stop
            exit_price  = bar_open
            exit_reason = "sl-gap"
        elif bar_high >= trade.stop:         # stop touched intrabar
            exit_price  = trade.stop
            exit_reason = "sl"
        elif not trade.exit_on_trend_flip and bar_low <= trade.target:  # target hit
            exit_price  = trade.target
            exit_reason = "tp"

    if exit_price is None:
        return None

    if trade.side == "long":
        pnl = (exit_price - trade.entry) * remaining_qty
    else:
        pnl = (trade.entry - exit_price) * remaining_qty

    return {
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "pnl_usd": pnl,
        "qty_closed": remaining_qty,
        "partial": False,
    }


# ---------------------------------------------------------------------------
# Core simulation loop
# ---------------------------------------------------------------------------

def run_backtest(
    symbol: str,
    df: pd.DataFrame,
    initial_equity: float = 10_000.0,
    enable_shorts: bool = False,
) -> list[BacktestTrade]:
    """
    Walk forward bar by bar, detect signals, simulate fills, and track P&L.

    Parameters
    ----------
    symbol         : e.g. ``"BTC/USD"``
    df             : Full OHLCV history DataFrame (UTC-indexed, all columns)
    initial_equity : Starting portfolio value used for position sizing
    enable_shorts  : Override ``config.ENABLE_SHORT_SELLING`` for this run

    Returns a list of completed ``BacktestTrade`` objects (including any trade
    still open at end-of-data, closed at last bar's close).
    """
    if df is None or df.empty:
        logger.warning(f"{symbol}: Empty dataframe — skipping backtest")
        return []

    # Temporarily override short-selling flag for this run
    original_short = config.ENABLE_SHORT_SELLING
    config.ENABLE_SHORT_SELLING = enable_shorts

    # Need enough bars for indicators + one evaluation bar + one fill bar
    min_bars = config.EMA_LONG + config.PULLBACK_LOW_BARS + 10
    if config.STRATEGY_MODE == "4h_trend_momentum":
        min_bars = max(min_bars, config.TREND_HTF_EMA_SLOW * 4 + 10)

    trades: list[BacktestTrade] = []
    equity  = initial_equity
    open_trade: Optional[BacktestTrade] = None

    # Precompute indicators once. Recomputing the full feature set on every
    # historical bar is O(n^2) and makes 1-minute backtests impractical.
    indicator_df = add_all_indicators(df)

    # Synthetic spread split across bid/ask
    spread_frac = _BACKTEST_SPREAD_PCT / 100.0
    half_spread = spread_frac / 2.0

    try:
        for i in range(min_bars, len(indicator_df)):
            bar = indicator_df.iloc[i]

            # ---- Check exit on the currently open position ----
            if open_trade is not None:
                result = _check_exit(open_trade, bar)
                if result is None and open_trade.exit_on_trend_flip:
                    trend = identify_four_hour_trend(indicator_df.iloc[: i + 1])
                    expected_trend = "downtrend" if open_trade.side == "short" else "uptrend"
                    if trend != expected_trend:
                        exit_price = float(bar["close"])
                        result = {
                            "exit_price": exit_price,
                            "exit_reason": "trend-flip",
                            "pnl_usd": (
                                (open_trade.entry - exit_price) * open_trade.qty
                                if open_trade.side == "short"
                                else (exit_price - open_trade.entry) * open_trade.qty
                            ),
                            "qty_closed": open_trade.remaining_qty or open_trade.qty,
                            "partial": False,
                        }
                if result:
                    open_trade.pnl_usd += result["pnl_usd"]
                    if result["partial"]:
                        open_trade.tp1_hit = True
                        open_trade.remaining_qty = max(
                            0.0,
                            (open_trade.remaining_qty or open_trade.qty)
                            - result["qty_closed"],
                        )
                        equity += result["pnl_usd"]
                        logger.debug(
                            f"{symbol}[{i}]: TP1 realized half-position profit "
                            f"${result['pnl_usd']:.2f}; remaining={open_trade.remaining_qty:.8f}"
                        )
                    else:
                        open_trade.exit_time   = df.index[i]
                        open_trade.exit_price  = result["exit_price"]
                        open_trade.exit_reason = result["exit_reason"]
                        equity += result["pnl_usd"]
                        open_trade.equity_after = equity
                        trades.append(open_trade)
                        open_trade = None

            # ---- Look for a new signal (only when flat) ----
            if open_trade is None and i + 1 < len(df):
                window = indicator_df.iloc[: i + 1]
                close  = float(bar["close"])
                ask    = close * (1.0 + half_spread)
                bid    = close * (1.0 - half_spread)

                signal = detect_signal(
                    df=window,
                    symbol=symbol,
                    ask_price=ask,
                    bid_price=bid,
                    spread_pct=_BACKTEST_SPREAD_PCT,
                    use_closed_candle=(
                        False if config.STRATEGY_MODE == "4h_trend_momentum" else None
                    ),
                )

                if signal is None:
                    continue

                # Simulate limit fill on the very next bar
                next_bar = indicator_df.iloc[i + 1]
                if not _simulate_limit_fill(signal, next_bar):
                    logger.debug(
                        f"{symbol}[{i}]: Limit not filled "
                        f"(entry={signal.entry:.4f} "
                        f"next L={float(next_bar['low']):.4f} "
                        f"H={float(next_bar['high']):.4f})"
                    )
                    continue

                profile = (
                    HIGH_RISK_PROFILE
                    if signal.risk_profile == "higher-risk"
                    else STANDARD_PROFILE
                )
                qty = calculate_position_qty(
                    signal.entry, signal.stop, equity, profile, signal.side
                )
                if qty <= 0:
                    logger.debug(f"{symbol}[{i}]: Zero qty — skipping signal")
                    continue

                open_trade = BacktestTrade(
                    symbol       = symbol,
                    side         = signal.side,
                    entry        = signal.entry,
                    stop         = signal.stop,
                    target       = signal.target,
                    qty          = qty,
                    entry_bar    = i + 1,
                    entry_time   = df.index[i + 1],
                    regime       = signal.regime,
                    risk_profile = signal.risk_profile,
                    reason       = signal.reason,
                    target1      = signal.target1,
                    exit_on_trend_flip = signal.exit_on_trend_flip,
                    remaining_qty = qty,
                )
                logger.debug(
                    f"{symbol}[{i + 1}]: {signal.side.upper()} opened "
                    f"@ {signal.entry:.4f} SL={signal.stop:.4f} TP={signal.target:.4f} "
                    f"qty={qty:.6f} [{signal.risk_profile}]"
                )

        # ---- Close any trade still open at end of data ----
        if open_trade is not None:
            last_close = float(df.iloc[-1]["close"])
            remaining_qty = open_trade.remaining_qty or open_trade.qty
            pnl = (
                (last_close - open_trade.entry) * remaining_qty
                if open_trade.side == "long"
                else (open_trade.entry - last_close) * remaining_qty
            )
            open_trade.exit_time    = df.index[-1]
            open_trade.exit_price   = last_close
            open_trade.exit_reason  = "end-of-data"
            open_trade.pnl_usd     += pnl
            equity += pnl
            open_trade.equity_after = equity
            trades.append(open_trade)

    finally:
        config.ENABLE_SHORT_SELLING = original_short

    logger.info(f"{symbol}: Backtest complete — {len(trades)} trades simulated")
    return trades


def run_rotation_backtest(
    data: dict[str, pd.DataFrame],
    initial_equity: float = 10_000.0,
) -> list[BacktestTrade]:
    """Replay breakout candidates as one account rotating through one position.

    Signals are ranked only when their latest completed bars share a timestamp.
    Limit entries are tested against the next available bar for that symbol.
    """
    if config.STRATEGY_MODE != "breakout_rotation":
        raise ValueError("run_rotation_backtest requires STRATEGY_MODE='breakout_rotation'")

    frames = {
        symbol: add_all_indicators(frame.sort_index())
        for symbol, frame in data.items()
        if frame is not None and not frame.empty
    }
    if not frames:
        return []

    timestamps = sorted({stamp for frame in frames.values() for stamp in frame.index})
    positions = {
        symbol: {stamp: index for index, stamp in enumerate(frame.index)}
        for symbol, frame in frames.items()
    }
    window_size = max(
        config.EMA_LONG + config.PULLBACK_LOW_BARS + 5,
        config.BREAKOUT_RANGE_BARS + 1,
    )
    spread_fraction = _BACKTEST_SPREAD_PCT / 100.0
    half_spread = spread_fraction / 2.0

    equity = initial_equity
    equity_high_water = initial_equity
    daily_entries = 0
    daily_pnl = 0.0
    active_day = None
    traded_symbols_today: set[str] = set()
    open_trade: Optional[BacktestTrade] = None
    pending_entry: Optional[dict] = None
    trades: list[BacktestTrade] = []

    for step, timestamp in enumerate(timestamps):
        current_day = timestamp.date()
        if current_day != active_day:
            active_day = current_day
            daily_entries = 0
            daily_pnl = 0.0
            traded_symbols_today.clear()

        bars_now = {
            symbol: frame.iloc[positions[symbol][timestamp]]
            for symbol, frame in frames.items()
            if timestamp in positions[symbol]
        }
        just_filled = False

        if pending_entry is not None:
            symbol = pending_entry["signal"].symbol
            if symbol in bars_now and timestamp > pending_entry["signal_time"]:
                next_bar = bars_now[symbol]
                if _simulate_limit_fill(pending_entry["signal"], next_bar):
                    signal = pending_entry["signal"]
                    qty = pending_entry["qty"]
                    open_trade = BacktestTrade(
                        symbol=signal.symbol,
                        side=signal.side,
                        entry=signal.entry,
                        stop=signal.stop,
                        target=signal.target,
                        qty=qty,
                        entry_bar=step,
                        entry_time=timestamp,
                        regime=signal.regime,
                        risk_profile=signal.risk_profile,
                        reason=signal.reason,
                        target1=signal.target1,
                        remaining_qty=qty,
                    )
                    just_filled = True
                    logger.debug(
                        f"{timestamp}: ROTATION ENTRY {symbol} qty={qty:.8f} "
                        f"TP1={signal.target1:.8f} TP2={signal.target:.8f}"
                    )
                else:
                    traded_symbols_today.discard(symbol)
                    logger.debug(f"{timestamp}: {symbol} limit entry not filled on next bar")
                pending_entry = None

        if open_trade is not None and not just_filled:
            bar = bars_now.get(open_trade.symbol)
            if bar is not None:
                result = _check_exit(open_trade, bar)
                if result:
                    open_trade.pnl_usd += result["pnl_usd"]
                    daily_pnl += result["pnl_usd"]
                    equity += result["pnl_usd"]
                    equity_high_water = max(equity_high_water, equity)
                    if result["partial"]:
                        open_trade.tp1_hit = True
                        remaining_qty = (
                            open_trade.remaining_qty
                            if open_trade.remaining_qty is not None
                            else open_trade.qty
                        )
                        open_trade.remaining_qty = max(
                            0.0, remaining_qty - result["qty_closed"]
                        )
                    else:
                        open_trade.exit_time = timestamp
                        open_trade.exit_price = result["exit_price"]
                        open_trade.exit_reason = result["exit_reason"]
                        open_trade.equity_after = equity
                        trades.append(open_trade)
                        open_trade = None

        if open_trade is not None or pending_entry is not None:
            continue

        drawdown = (
            (equity_high_water - equity) / equity_high_water
            if equity_high_water > 0 else 0.0
        )
        if (
            daily_entries >= config.MAX_TRADES_PER_DAY
            or daily_pnl <= -equity * STANDARD_PROFILE.max_daily_loss_pct
            or drawdown >= STANDARD_PROFILE.max_drawdown_pct
        ):
            continue

        candidates = []
        for symbol, frame in frames.items():
            if symbol in traded_symbols_today or symbol not in bars_now:
                continue
            index = positions[symbol][timestamp]
            window = frame.iloc[max(0, index - window_size + 1):index + 1]
            close = float(bars_now[symbol]["close"])
            signal = detect_signal(
                df=window,
                symbol=symbol,
                ask_price=close * (1.0 + half_spread),
                bid_price=close * (1.0 - half_spread),
                spread_pct=_BACKTEST_SPREAD_PCT,
                use_closed_candle=False,
            )
            if signal is None:
                continue
            valid, _ = validate_setup(signal.entry, signal.stop, signal.target, signal.side)
            if valid:
                candidates.append(signal)

        if not candidates:
            continue

        signal = max(candidates, key=lambda candidate: candidate.breakout_score)
        profile = (
            HIGH_RISK_PROFILE
            if signal.risk_profile == "higher-risk"
            else STANDARD_PROFILE
        )
        qty = calculate_position_qty(signal.entry, signal.stop, equity, profile, signal.side)
        if qty <= 0:
            continue

        pending_entry = {
            "signal": signal,
            "qty": qty,
            "signal_time": timestamp,
        }
        traded_symbols_today.add(signal.symbol)
        daily_entries += 1
        logger.debug(
            f"{timestamp}: strongest breakout {signal.symbol} "
            f"score={signal.breakout_score:.4f} qty={qty:.8f}"
        )

    if open_trade is not None:
        frame = frames[open_trade.symbol]
        last_bar = frame.iloc[-1]
        remaining_qty = (
            open_trade.remaining_qty
            if open_trade.remaining_qty is not None
            else open_trade.qty
        )
        pnl = (float(last_bar["close"]) - open_trade.entry) * remaining_qty
        open_trade.exit_time = frame.index[-1]
        open_trade.exit_price = float(last_bar["close"])
        open_trade.exit_reason = "end-of-data"
        open_trade.pnl_usd += pnl
        equity += pnl
        open_trade.equity_after = equity
        trades.append(open_trade)

    logger.info(
        f"Rotation backtest complete — {len(trades)} trades, "
        f"ending equity=${equity:.2f}"
    )
    return trades
