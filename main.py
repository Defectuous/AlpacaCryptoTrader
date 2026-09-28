"""
AlpacaCryptoTrader — main entry point.

Breakout rotation across the configured allowlist by default.
Paper trading by default (set ALPACA_PAPER=false in .env to go live).

Run:
    python main.py
"""
from __future__ import annotations

import signal
import sys
import time

import pandas as pd
from loguru import logger

import config
from data.market_data import get_bars, get_latest_quote
from trader.alpaca_client import get_trading_client
from trader.discord_notifier import (
    format_account_line,
    has_been_notified as discord_has_been_notified,
    mark_notified as discord_mark_notified,
    send_buy_submitted as discord_send_buy_submitted,
    send_sell_submitted as discord_send_sell_submitted,
    send_fill_update as discord_send_fill_update,
)
from trader.telegram_notifier import (
    has_been_notified as telegram_has_been_notified,
    mark_notified as telegram_mark_notified,
    send_buy_submitted as telegram_send_buy_submitted,
    send_sell_submitted as telegram_send_sell_submitted,
    send_fill_update as telegram_send_fill_update,
)
from trader.journal import (
    ensure_journal,
    get_open_trade_symbols,
    get_open_trade_order_id,
    get_today_stats,
    log_trade,
    update_trade,
)
from trader.order_manager import (
    cancel_open_buy_orders,
    get_account_info,
    get_open_orders,
    get_open_positions,
    place_order,
)
from trader.risk_manager import (
    HIGH_RISK_PROFILE,
    STANDARD_PROFILE,
    check_daily_limits,
    validate_setup,
    select_risk_profile,
    update_hwm,
)
from trader.indicators import calculate_rsi, identify_four_hour_trend
from trader.strategy import detect_signal
from trader.streaming import LiveStreamRunner

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logger.remove()
logger.add(
    sys.stdout,
    colorize=None,  # colour on a terminal, plain text under systemd/journald
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level:<8}</level> | {message}",
    level="INFO",
)
logger.add(
    "logs/trader_{time:YYYY-MM-DD}.log",
    rotation="1 day",
    retention="30 days",
    level="DEBUG",
    encoding="utf-8",
)

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
_running = True


def _shutdown(signum, frame):
    global _running
    logger.warning("Shutdown signal received — stopping after current cycle.")
    _running = False


signal.signal(signal.SIGINT,  _shutdown)
signal.signal(signal.SIGTERM, _shutdown)


def _build_account_line() -> str:
    stats = get_today_stats()
    account = get_account_info()
    return format_account_line(account, stats["trades_today"], stats["daily_pnl"])


# ---------------------------------------------------------------------------
# Position monitoring
# ---------------------------------------------------------------------------

def sync_open_positions_to_journal() -> None:
    """
    Cross-reference Alpaca's live positions / closed orders against the
    journal and update any rows whose status has changed.

    Strategy:
      - Fetch all orders that are NOT open (i.e., filled, cancelled, expired).
      - For each journal order_id, if Alpaca reports it filled/cancelled, update.
    """
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus

        client = get_trading_client()
        closed_orders = client.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=500, nested=True)
        )

        orders_to_sync = []
        for parent_order in closed_orders:
            orders_to_sync.append((parent_order, None))
            orders_to_sync.extend(
                (leg, str(parent_order.id))
                for leg in (getattr(parent_order, "legs", None) or [])
            )

        for order, parent_order_id in orders_to_sync:
            order_id = str(order.id)
            status   = str(order.status).lower()
            side     = str(getattr(order, "side", "")).upper()
            symbol   = str(getattr(order, "symbol", ""))
            journal_order_id = str(parent_order_id) if parent_order_id else order_id

            filled_price: float | None = None
            filled_qty = float(getattr(order, "filled_qty", 0) or 0)

            if status == "filled" and order.filled_avg_price:
                filled_price = float(order.filled_avg_price)
                logger.success(
                    f"{side} filled ✓ {symbol} | "
                    f"qty={filled_qty:.8f} price={filled_price:.8f} order_id={order_id}"
                )

            # Entry fills are not realized exits; child fills link P&L to the parent row.
            if parent_order_id:
                update_trade(
                    journal_order_id,
                    status,
                    filled_price,
                    exit_order_id=order_id,
                    symbol=symbol,
                )
            elif status == "filled":
                entry_order_id = get_open_trade_order_id(symbol)
                if entry_order_id and entry_order_id != order_id:
                    update_trade(
                        entry_order_id,
                        status,
                        filled_price,
                        exit_order_id=order_id,
                        symbol=symbol,
                    )
                else:
                    update_trade(journal_order_id, status)
            else:
                update_trade(journal_order_id, status)

            # Notify once per filled order (covers both BUY and SELL fills).
            if status == "filled" and (
                (not discord_has_been_notified(order_id))
                or (not telegram_has_been_notified(order_id))
            ):
                account_line = _build_account_line()
                if not discord_has_been_notified(order_id):
                    if discord_send_fill_update(order, account_line):
                        discord_mark_notified(order_id)

                if not telegram_has_been_notified(order_id):
                    if telegram_send_fill_update(order, account_line):
                        telegram_mark_notified(order_id)

    except Exception as exc:
        logger.error(f"Position sync error: {exc}")


def log_position_summary() -> None:
    """Print a one-line summary of each open position."""
    positions = get_open_positions()
    if not positions:
        logger.info("No open positions")
        return
    for sym, pos in positions.items():
        logger.info(
            f"  POSITION {sym}: "
            f"qty={pos['qty']:.6f} | "
            f"avg_entry={pos['avg_entry']:.4f} | "
            f"unrealized_pl=${pos['unrealized_pl']:.2f}"
        )


def _handle_stream_bar(
    symbol: str,
    bars: pd.DataFrame,
    quote: dict[str, float],
    snapshot: dict[str, tuple[pd.DataFrame, dict[str, float]]],
) -> None:
    """Evaluate one completed streamed strategy bar."""
    try:
        if config.STRATEGY_MODE == "breakout_rotation":
            run_scan_cycle(snapshot)
        else:
            run_scan_cycle({symbol: (bars, quote)})
    except Exception as exc:
        logger.error(f"Stream scan error for {symbol}: {exc}", exc_info=True)


def _handle_stream_trade_update(update) -> None:
    """Reconcile the journal when Alpaca publishes an order update."""
    logger.info(
        f"Trade update: {getattr(update, 'event', 'unknown')} "
        f"order_id={getattr(update, 'order', update)}"
    )
    sync_open_positions_to_journal()


def _run_trend_flip_exits(
    streamed_data: dict[str, tuple[pd.DataFrame, dict[str, float]]] | None,
    live_positions: dict[str, dict],
) -> None:
    """Close trend-mode positions when their completed 4-hour regime changes."""
    for symbol in config.SYMBOLS:
        position_symbol = next(
            (key for key in live_positions if key.replace("/", "") == symbol.replace("/", "")),
            None,
        )
        pending_entries = get_open_orders(symbol) if position_symbol is None else []
        if position_symbol is None and not pending_entries:
            continue

        if streamed_data is None:
            bars = get_bars(symbol)
            evaluation_bars = bars.iloc[:-1] if len(bars) else bars
        else:
            pair = streamed_data.get(symbol)
            if pair is None:
                continue
            evaluation_bars = pair[0]

        if len(evaluation_bars) < config.TREND_HTF_EMA_SLOW * 4:
            logger.warning(f"{symbol}: Insufficient history to evaluate 4-hour trend exit")
            continue

        trend = identify_four_hour_trend(evaluation_bars)
        client = get_trading_client()
        if position_symbol is not None:
            position = live_positions[position_symbol]
            side_value = str(position.get("side", "long")).lower()
            side = "short" if side_value.endswith("short") else "long"
            expected_trend = "downtrend" if side == "short" else "uptrend"
            if trend != expected_trend:
                logger.warning(
                    f"{symbol}: 4-hour trend changed from {expected_trend} to {trend}; "
                    "canceling attached exits and closing position"
                )
                for order in get_open_orders(position_symbol, nested=True):
                    try:
                        client.cancel_order_by_id(order.id)
                    except Exception as exc:
                        logger.error(f"{symbol}: Could not cancel open exit order {order.id}: {exc}")
                        break
                else:
                    try:
                        order = client.close_position(position_symbol)
                        logger.success(
                            f"{symbol}: Trend-flip close submitted — id={order.id} "
                            f"old-side={side} new-trend={trend}"
                        )
                    except Exception as exc:
                        logger.error(f"{symbol}: Trend-flip close failed — {exc}")

        if pending_entries:
            momentum = float(calculate_rsi(evaluation_bars["close"], config.RSI_PERIOD).iloc[-1])
            for order in pending_entries:
                side = "short" if str(order.side).lower() == "sell" else "long"
                expected_trend = "downtrend" if side == "short" else "uptrend"
                momentum_aligned = (
                    momentum <= config.TREND_MOMENTUM_RSI_SHORT
                    if side == "short"
                    else momentum >= config.TREND_MOMENTUM_RSI_LONG
                )
                if trend == expected_trend and momentum_aligned:
                    continue
                try:
                    client.cancel_order_by_id(order.id)
                    logger.info(
                        f"{symbol}: Canceled pending {side} entry; "
                        f"4h trend={trend}, hourly RSI={momentum:.1f}"
                    )
                except Exception as exc:
                    logger.error(f"{symbol}: Could not cancel stale entry {order.id}: {exc}")


# ---------------------------------------------------------------------------
# Main scan cycle
# ---------------------------------------------------------------------------

def _run_breakout_rotation_cycle(
    streamed_data: dict[str, tuple[pd.DataFrame, dict[str, float]]] | None,
    live_positions: dict[str, dict],
    open_symbols: list[str],
) -> None:
    """Enter only the strongest fresh breakout while otherwise remaining in cash."""
    if live_positions:
        logger.info("Breakout rotation: position remains open; waiting for its full exit")
        return

    if get_open_orders():
        logger.info("Breakout rotation: entry/exit orders are still open; waiting")
        return

    data: dict[str, tuple[pd.DataFrame, dict[str, float]]] = {}
    for symbol in config.SYMBOLS:
        if symbol in open_symbols:
            continue
        if streamed_data is not None:
            pair = streamed_data.get(symbol)
            if pair is None:
                continue
            bars, quote = pair
        else:
            bars = get_bars(symbol)
            quote = get_latest_quote(symbol)
        if bars is None or bars.empty or quote.get("ask", 0) <= 0 or quote.get("bid", 0) <= 0:
            continue
        data[symbol] = (bars, quote)

    if not data:
        logger.info("Breakout rotation: no fresh market data; staying in cash")
        return

    def utc_timestamp(value) -> pd.Timestamp:
        timestamp = pd.Timestamp(value)
        return timestamp.tz_localize("UTC") if timestamp.tzinfo is None else timestamp.tz_convert("UTC")

    newest_bar = max(utc_timestamp(frame.index[-1]) for frame, _ in data.values())
    timeframe_minutes = {
        "1Min": 1,
        "5Min": 5,
        "15Min": 15,
        "1Hour": 60,
        "1Day": 1440,
    }.get(config.BAR_TIMEFRAME, 1)

    candidates = []
    for symbol, (bars, quote) in data.items():
        bar_time = utc_timestamp(bars.index[-1])
        if newest_bar - bar_time > pd.Timedelta(minutes=timeframe_minutes):
            logger.debug(f"{symbol}: Cached breakout data is stale; skipping")
            continue
        signal = detect_signal(
            df=bars,
            symbol=symbol,
            ask_price=quote["ask"],
            bid_price=quote["bid"],
            spread_pct=quote.get("spread_pct", 999.0),
            use_closed_candle=streamed_data is None,
        )
        if signal is None:
            continue
        ok, msg = validate_setup(signal.entry, signal.stop, signal.target, side=signal.side)
        if not ok:
            logger.warning(f"{symbol}: Breakout failed risk validation — {msg}")
            continue
        candidates.append((signal, msg))

    if not candidates:
        logger.info("Breakout rotation: no clean breakout; staying in cash")
        return

    signal, validation = max(candidates, key=lambda candidate: candidate[0].breakout_score)
    profile = (
        HIGH_RISK_PROFILE
        if signal.risk_profile == "higher-risk"
        else STANDARD_PROFILE
    )
    logger.info(
        f"Strongest breakout ▶ {signal.symbol} score={signal.breakout_score:.4f} "
        f"[{signal.risk_profile}] {validation}"
    )
    logger.info(
        f"  Entry={signal.entry:.8f} Stop={signal.stop:.8f} "
        f"TP1={signal.target1:.8f} TP2={signal.target:.8f}"
    )
    logger.info(f"  Rationale: {signal.reason}")
    logger.info(
        f"  Guardrails: risk={profile.risk_pct_per_trade:.2%}, "
        f"daily-loss={profile.max_daily_loss_pct:.2%}, "
        f"drawdown-pause={profile.max_drawdown_pct:.2%}, "
        f"max-open={profile.max_open_positions}"
    )
    order_info = place_order(signal, profile)
    if not order_info:
        logger.error(f"Breakout rotation: entry failed for {signal.symbol}; staying in cash")
        return

    legs = order_info.get("legs") or [order_info]
    for leg in legs:
        log_trade(leg)
    if len(legs) == 2:
        logger.success(
            f"Breakout entry submitted for {signal.symbol} | "
            f"score={signal.breakout_score:.4f} | two protected exit legs"
        )
    else:
        logger.warning(
            f"Breakout entry for {signal.symbol} accepted with one protected leg only; "
            "check the order before allowing another entry"
        )
    post_trade_line = _build_account_line()
    discord_send_buy_submitted(order_info, post_trade_line)
    telegram_send_buy_submitted(order_info, post_trade_line)


def run_scan_cycle(
    streamed_data: dict[str, tuple[pd.DataFrame, dict[str, float]]] | None = None,
) -> None:
    """
    Scan configured symbols for the active strategy and place orders when
    conditions are met and daily limits allow.
    """
    stats        = get_today_stats()
    trades_today = stats["trades_today"]
    daily_pnl    = stats["daily_pnl"]

    account = get_account_info()
    portfolio_value = account["portfolio_value"]

    # Update high-water mark every cycle
    update_hwm(portfolio_value)

    # Use a placeholder profile for the daily limit check
    # (actual profile is determined per-signal via ATR)
    from trader.risk_manager import STANDARD_PROFILE
    can_trade, limit_reason = check_daily_limits(
        trades_today, daily_pnl, portfolio_value, STANDARD_PROFILE
    )
    live_positions = get_open_positions()
    open_symbols   = get_open_trade_symbols()

    logger.info(
        format_account_line(account, trades_today, daily_pnl)
    )

    if "ACTIVE" not in str(account["status"]).upper():
        logger.warning(f"Account status is '{account['status']}' — halting scan")
        return

    if config.STRATEGY_MODE == "4h_trend_momentum":
        _run_trend_flip_exits(streamed_data, live_positions)
        live_positions = get_open_positions()

    if not can_trade:
        logger.info(f"Trading paused: {limit_reason}")
        return

    if config.STRATEGY_MODE == "breakout_rotation":
        _run_breakout_rotation_cycle(
            streamed_data,
            live_positions,
            open_symbols,
        )
        return

    symbols = list(streamed_data) if streamed_data is not None else config.SYMBOLS
    for symbol in symbols:
        if not _running:
            break

        # Skip if a live position already exists for this symbol
        if symbol in live_positions:
            logger.debug(f"{symbol}: Live position exists — skipping")
            continue

        # Skip if journal shows an open/pending trade today
        if symbol in open_symbols:
            logger.debug(f"{symbol}: Journal shows open trade today — skipping")
            continue

        logger.debug(f"Scanning {symbol}…")

        # ---- Data ----
        if streamed_data is None:
            bars = get_bars(symbol)
            quote = get_latest_quote(symbol)
        else:
            bars, quote = streamed_data[symbol]

        if bars.empty:
            logger.debug(f"{symbol}: No bar data — skipping")
            continue

        if quote["ask"] <= 0:
            logger.warning(f"{symbol}: Invalid quote — skipping")
            continue

        # ---- Signal detection (includes profile auto-selection) ----
        signal = detect_signal(
            df=bars,
            symbol=symbol,
            ask_price=quote["ask"],
            bid_price=quote["bid"],
            spread_pct=quote["spread_pct"],
            use_closed_candle=streamed_data is None,
        )

        if signal is None:
            continue

        # ---- Risk validation ----
        ok, msg = validate_setup(signal.entry, signal.stop, signal.target, side=signal.side)
        if not ok:
            logger.warning(f"{symbol}: Setup failed validation — {msg}")
            continue

        logger.info(f"Valid {signal.side.upper()} setup ▶ {symbol} {msg} [{signal.risk_profile}]")
        logger.info(f"  Entry : {signal.entry:.6f}")
        logger.info(f"  Stop  : {signal.stop:.6f}")
        if signal.exit_on_trend_flip:
            logger.info(f"  Projected target (risk validation only): {signal.target:.6f}")
            logger.info("  Exit   : protective stop or 4-hour trend flip")
        else:
            logger.info(f"  Target: {signal.target:.6f}")
        logger.info(f"  Reason: {signal.reason}")

        # Resolve profile object for order sizing
        from trader.risk_manager import HIGH_RISK_PROFILE
        profile = HIGH_RISK_PROFILE if signal.risk_profile == "higher-risk" else STANDARD_PROFILE

        if signal.exit_on_trend_flip:
            logger.info(
                f"  Guardrails [{profile.name}]: risk={profile.risk_pct_per_trade:.2%}, "
                f"daily-loss={profile.max_daily_loss_pct:.2%}, "
                f"drawdown-pause={profile.max_drawdown_pct:.2%}, "
                f"max-open={profile.max_open_positions}"
            )

        # Enforce max open positions for the resolved profile
        if len(live_positions) >= profile.max_open_positions:
            logger.info(
                f"{symbol}: Max open positions for [{profile.name}] profile "
                f"({len(live_positions)}/{profile.max_open_positions}) — skipping"
            )
            continue

        # ---- Order placement ----
        order_info = place_order(signal, profile)
        if order_info:
            log_trade(order_info)
            side_label = "SHORT" if signal.side == "short" else "BUY"
            logger.success(
                f"{side_label} submitted ✓ {symbol} | order_id={order_info['order_id']}"
            )
            logger.info(
                f"  Position size={order_info['qty']:.8f} | "
                f"stop distance={abs(signal.entry - signal.stop):.8f} | "
                f"projected target distance={abs(signal.target - signal.entry):.8f}"
            )
            if signal.exit_on_trend_flip:
                logger.info(
                    f"Exits armed ▶ {symbol} | stop_loss={order_info['stop']:.6f} "
                    "trend_flip=4h"
                )
            else:
                logger.info(
                    f"Exits armed ▶ {symbol} | "
                    f"take_profit={order_info['target']:.6f} stop_loss={order_info['stop']:.6f}"
                )

            post_trade_line = _build_account_line()
            discord_send_buy_submitted(order_info, post_trade_line)
            discord_send_sell_submitted(order_info, post_trade_line)
            telegram_send_buy_submitted(order_info, post_trade_line)
            telegram_send_sell_submitted(order_info, post_trade_line)

            # Refresh state so next symbol uses updated exposure
            open_symbols   = get_open_trade_symbols()
            live_positions = get_open_positions()
        else:
            logger.error(f"Order placement failed for {symbol}")

        # Re-check daily limits after each order attempt
        stats = get_today_stats()
        can_trade, limit_reason = check_daily_limits(
            stats["trades_today"], stats["daily_pnl"],
            portfolio_value, STANDARD_PROFILE
        )
        if not can_trade:
            logger.info(f"Limit reached after order: {limit_reason}")
            break


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    logger.info("=" * 60)
    logger.info("  AlpacaCryptoTrader")
    logger.info(f"  Mode      : {'PAPER TRADING' if config.ALPACA_PAPER else '⚠  LIVE TRADING'}")
    logger.info(f"  Symbols   : {', '.join(config.SYMBOLS)}")
    logger.info(f"  Short sell: {'ENABLED' if config.ENABLE_SHORT_SELLING else 'disabled'}")
    logger.info(f"  Max trades/day  : {config.MAX_TRADES_PER_DAY}")
    logger.info(f"  Std risk/trade  : {config.STANDARD_RISK_PCT_PER_TRADE*100:.1f}% of equity")
    logger.info(f"  High risk/trade : {config.HIGH_RISK_PCT_PER_TRADE*100:.1f}% of equity (ATR>={config.HIGH_RISK_ATR_THRESHOLD*100:.1f}%)")
    logger.info(f"  R:R target      : {config.REWARD_RISK_MIN} – {config.REWARD_RISK_TARGET}")
    logger.info(f"  Bar timeframe   : {config.BAR_TIMEFRAME}")
    logger.info(f"  Closed candle   : {config.USE_CLOSED_CANDLE}")
    logger.info("  Runtime         : Alpaca WebSocket streaming")
    logger.info(f"  Strategy mode   : {config.STRATEGY_MODE}")
    if config.STRATEGY_MODE == "breakout_rotation":
        logger.info(
            f"  Breakout exits  : {config.BREAKOUT_TP1_R:.2f}R half / "
            f"{config.BREAKOUT_TP2_R:.2f}R remainder"
        )
    else:
        logger.info(f"  R:R target      : {config.REWARD_RISK_MIN} – {config.REWARD_RISK_TARGET}")
    logger.info("=" * 60)

    ensure_journal()

    # Verify Alpaca connectivity on startup
    try:
        account = get_account_info()
        logger.info(
            f"Connected to Alpaca ✓ | status={account['status']} | "
            f"portfolio=${account['portfolio_value']:.2f}"
        )
    except Exception as exc:
        logger.error(f"Cannot connect to Alpaca: {exc}")
        logger.error("Check ALPACA_API_KEY and ALPACA_SECRET_KEY in your .env file.")
        sys.exit(1)

    sync_open_positions_to_journal()
    log_position_summary()

    runner = LiveStreamRunner(
        config.SYMBOLS,
        on_bar_close=_handle_stream_bar,
        on_trade_update=_handle_stream_trade_update,
    )

    exit_code = 0
    try:
        runner.start()
        logger.info("Waiting for streamed bars; scans run at completed bar boundaries.")
        while _running:
            time.sleep(1)
            # A dead stream thread would leave the bot idle forever; exit non-zero
            # so a supervisor (systemd) restarts it with fresh connections.
            dead = runner.dead_streams()
            if dead:
                raise RuntimeError(f"stream thread(s) exited: {', '.join(dead)}")
    except Exception as exc:
        logger.error(f"Streaming runtime stopped unexpectedly: {exc}", exc_info=True)
        exit_code = 1
    finally:
        runner.stop()
        logger.info("Cancelling any open entry orders before exit…")
        cancel_open_buy_orders()
        logger.info("AlpacaCryptoTrader stopped.")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
