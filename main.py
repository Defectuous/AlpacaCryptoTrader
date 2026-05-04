"""
AlpacaCryptoTrader — main entry point.

VWAP Pullback Strategy for BTC, ETH, SOL
Paper trading by default (set ALPACA_PAPER=false in .env to go live).

Run:
    python main.py
"""
from __future__ import annotations

import signal
import sys
import time
from datetime import datetime, timezone

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
from trader.risk_manager import check_daily_limits, validate_setup, select_risk_profile, update_hwm
from trader.strategy import detect_signal

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logger.remove()
logger.add(
    sys.stdout,
    colorize=True,
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
            GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=50)
        )

        for order in closed_orders:
            order_id = str(order.id)
            status   = str(order.status).lower()
            side     = str(getattr(order, "side", "")).upper()
            symbol   = str(getattr(order, "symbol", ""))

            filled_price: float | None = None
            filled_qty = float(getattr(order, "filled_qty", 0) or 0)

            if status == "filled" and order.filled_avg_price:
                filled_price = float(order.filled_avg_price)
                logger.success(
                    f"{side} filled ✓ {symbol} | "
                    f"qty={filled_qty:.8f} price={filled_price:.8f} order_id={order_id}"
                )

            # update_trade computes PnL from entry_price/qty when exit_price is supplied
            update_trade(order_id, status, filled_price)

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


# ---------------------------------------------------------------------------
# Main scan cycle
# ---------------------------------------------------------------------------

def run_scan_cycle() -> None:
    """
    Scan all configured symbols for a VWAP pullback/rejection setup and place
    orders when conditions are met and daily limits allow.
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
    if not can_trade:
        logger.info(f"Trading paused: {limit_reason}")
        return

    live_positions = get_open_positions()
    open_symbols   = get_open_trade_symbols()

    logger.info(
        format_account_line(account, trades_today, daily_pnl)
    )

    if "ACTIVE" not in str(account["status"]).upper():
        logger.warning(f"Account status is '{account['status']}' — halting scan")
        return

    for symbol in config.SYMBOLS:
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
        bars = get_bars(symbol)
        if bars.empty:
            logger.debug(f"{symbol}: No bar data — skipping")
            continue

        quote = get_latest_quote(symbol)
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
        logger.info(f"  Target: {signal.target:.6f}")
        logger.info(f"  Reason: {signal.reason}")

        # Resolve profile object for order sizing
        from trader.risk_manager import HIGH_RISK_PROFILE
        profile = HIGH_RISK_PROFILE if signal.risk_profile == "higher-risk" else STANDARD_PROFILE

        # ---- Order placement ----
        order_info = place_order(signal, profile)
        if order_info:
            log_trade(order_info)
            side_label = "SHORT" if signal.side == "short" else "BUY"
            logger.success(
                f"{side_label} submitted ✓ {symbol} | order_id={order_info['order_id']}"
            )
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
    logger.info(f"  Poll interval   : {config.POLL_INTERVAL_SECONDS}s")
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

    # -----------------------------------------------------------------------
    # Main loop
    # -----------------------------------------------------------------------
    while _running:
        now = datetime.now(timezone.utc)
        logger.info(f"──── Cycle {now.strftime('%Y-%m-%d %H:%M:%S')} UTC ────")

        try:
            sync_open_positions_to_journal()
            log_position_summary()
            run_scan_cycle()
        except Exception as exc:
            logger.error(f"Unhandled error in main loop: {exc}", exc_info=True)

        if _running:
            logger.debug(f"Sleeping {config.POLL_INTERVAL_SECONDS}s…")
            # Sleep in short chunks so Ctrl+C is responsive
            for _ in range(config.POLL_INTERVAL_SECONDS):
                if not _running:
                    break
                time.sleep(1)

    # Cleanup on exit
    logger.info("Cancelling any open buy orders before exit…")
    cancel_open_buy_orders()
    logger.info("AlpacaCryptoTrader stopped.")


if __name__ == "__main__":
    main()
