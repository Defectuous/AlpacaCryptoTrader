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
from trader.strategy import detect_signal
from trader.risk_manager import (
    calculate_position_qty,
    STANDARD_PROFILE,
    HIGH_RISK_PROFILE,
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

    if trade.side == "long":
        if bar_open <= trade.stop:           # gapped down through stop
            exit_price  = bar_open
            exit_reason = "sl-gap"
        elif bar_low <= trade.stop:          # stop touched intrabar
            exit_price  = trade.stop
            exit_reason = "sl"
        elif bar_high >= trade.target:       # target hit
            exit_price  = trade.target
            exit_reason = "tp"
    else:  # short
        if bar_open >= trade.stop:           # gapped up through stop
            exit_price  = bar_open
            exit_reason = "sl-gap"
        elif bar_high >= trade.stop:         # stop touched intrabar
            exit_price  = trade.stop
            exit_reason = "sl"
        elif bar_low <= trade.target:        # target hit
            exit_price  = trade.target
            exit_reason = "tp"

    if exit_price is None:
        return None

    if trade.side == "long":
        pnl = (exit_price - trade.entry) * trade.qty
    else:
        pnl = (trade.entry - exit_price) * trade.qty

    return {"exit_price": exit_price, "exit_reason": exit_reason, "pnl_usd": pnl}


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

    trades: list[BacktestTrade] = []
    equity  = initial_equity
    open_trade: Optional[BacktestTrade] = None

    # Synthetic spread split across bid/ask
    spread_frac = _BACKTEST_SPREAD_PCT / 100.0
    half_spread = spread_frac / 2.0

    try:
        for i in range(min_bars, len(df)):
            bar = df.iloc[i]

            # ---- Check exit on the currently open position ----
            if open_trade is not None:
                result = _check_exit(open_trade, bar)
                if result:
                    open_trade.exit_time   = df.index[i]
                    open_trade.exit_price  = result["exit_price"]
                    open_trade.exit_reason = result["exit_reason"]
                    open_trade.pnl_usd     = result["pnl_usd"]
                    equity += result["pnl_usd"]
                    open_trade.equity_after = equity
                    trades.append(open_trade)
                    open_trade = None

            # ---- Look for a new signal (only when flat) ----
            if open_trade is None and i + 1 < len(df):
                window = df.iloc[: i + 1]
                close  = float(bar["close"])
                ask    = close * (1.0 + half_spread)
                bid    = close * (1.0 - half_spread)

                signal = detect_signal(
                    df=window,
                    symbol=symbol,
                    ask_price=ask,
                    bid_price=bid,
                    spread_pct=_BACKTEST_SPREAD_PCT,
                )

                if signal is None:
                    continue

                # Simulate limit fill on the very next bar
                next_bar = df.iloc[i + 1]
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
                )
                logger.debug(
                    f"{symbol}[{i + 1}]: {signal.side.upper()} opened "
                    f"@ {signal.entry:.4f} SL={signal.stop:.4f} TP={signal.target:.4f} "
                    f"qty={qty:.6f} [{signal.risk_profile}]"
                )

        # ---- Close any trade still open at end of data ----
        if open_trade is not None:
            last_close = float(df.iloc[-1]["close"])
            pnl = (
                (last_close - open_trade.entry) * open_trade.qty
                if open_trade.side == "long"
                else (open_trade.entry - last_close) * open_trade.qty
            )
            open_trade.exit_time    = df.index[-1]
            open_trade.exit_price   = last_close
            open_trade.exit_reason  = "end-of-data"
            open_trade.pnl_usd      = pnl
            equity += pnl
            open_trade.equity_after = equity
            trades.append(open_trade)

    finally:
        config.ENABLE_SHORT_SELLING = original_short

    logger.info(f"{symbol}: Backtest complete — {len(trades)} trades simulated")
    return trades
