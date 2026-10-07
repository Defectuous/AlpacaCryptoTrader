"""
AlpacaCryptoTrader — main entry point.

Trades the configured symbols on the exchange chosen by BROKER in .env.
Paper trading by default (set ALPACA_PAPER=false in .env to go live).

Run:
    python main.py
"""
from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

import pandas as pd
from loguru import logger

import config
from brokers import get_broker, resolve_symbols
from data.market_data import get_bars, get_latest_quote
from trader.discord_notifier import (
    format_account_line,
    has_been_notified as discord_has_been_notified,
    mark_notified as discord_mark_notified,
    send_buy_submitted as discord_send_buy_submitted,
    send_sell_submitted as discord_send_sell_submitted,
    send_fill_update as discord_send_fill_update,
    send_profit_alert as discord_send_profit_alert,
)
from trader.telegram_notifier import (
    has_been_notified as telegram_has_been_notified,
    mark_notified as telegram_mark_notified,
    send_buy_submitted as telegram_send_buy_submitted,
    send_sell_submitted as telegram_send_sell_submitted,
    send_fill_update as telegram_send_fill_update,
    send_profit_alert as telegram_send_profit_alert,
)
from trader.journal import (
    ensure_journal,
    get_open_trade_symbols,
    get_open_trade,
    get_today_stats,
    log_trade,
    note_exit,
    record_orders,
    tracks_orders,
    update_trade,
)
from trader.order_manager import (
    cancel_open_buy_orders,
    cancel_order,
    close_position,
    get_account_info,
    get_open_orders,
    get_open_positions,
    place_order,
)
from trader.profit_targets import load_targets, trail_settings
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
# Rotate at local midnight so each file holds exactly one calendar day.
# ("1 day" would rotate 24 h after startup, mixing two days in one file.)
# A restart mid-day appends to that day's existing file.
logger.add(
    "logs/trader_{time:YYYY-MM-DD}.log",
    rotation="00:00",
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

# False until the first sync has recorded the fills that existed at startup.
_fill_baseline_done = False
_logged_fill_ids: set[str] = set()
# order id -> _order_version() at the last sync; empty at startup, so the
# first sync re-records every order the broker returns.
_order_versions: dict[str, tuple] = {}


def _order_version(order) -> tuple:
    return (order.status, order.filled_qty, order.filled_avg_price, order.updated_at)


def _is_exit_fill(order, open_trade: dict) -> bool:
    """True when a filled standalone order closes the journal's open trade.

    Older fills for the same symbol (a previous position's buy or sell) and
    same-side fills must not be booked as this trade's exit.
    """
    if order.id == open_trade["order_id"]:
        return False
    closing_side = "BUY" if open_trade["side"] == "SHORT" else "SELL"
    if order.side.upper() != closing_side:
        return False
    opened_at = open_trade["opened_at"]
    if opened_at and order.submitted_at and order.submitted_at < opened_at:
        return False
    return True


# The stream delivers order events on several threads at once. Unserialized,
# a sync holding an older snapshot (stop "new") could write after the one
# holding the fill, and the version cache then skipped the fill for good.
_sync_lock = threading.Lock()


def sync_open_positions_to_journal() -> None:
    with _sync_lock:
        _sync_open_positions_to_journal()


def _sync_open_positions_to_journal() -> None:
    """
    Cross-reference the broker's orders against the journal and update any
    rows whose status has changed.

    Strategy:
      - Fetch all orders that are NOT open (i.e., filled, cancelled, expired),
        plus the open ones when the order ledger is on (MySQL journal).
      - Skip orders unchanged since the last sync.
      - With the ledger, mirror every changed order into it; it links each to
        its trade and books exits and P&L from the real fills.
      - For each journal order_id, if the broker reports it filled/cancelled, update.
    """
    global _fill_baseline_done
    try:
        broker = get_broker()
        closed_orders = broker.get_closed_orders(limit=500)
        ledger = tracks_orders()
        open_orders = broker.get_open_orders(nested=True) if ledger else []
        closed_ids = {o.id for o in closed_orders} | {leg.id for o in closed_orders for leg in o.legs}

        orders_to_sync = []
        for parent_order in [*closed_orders, *open_orders]:
            orders_to_sync.append((parent_order, None))
            orders_to_sync.extend((leg, parent_order.id) for leg in parent_order.legs)

        # Only orders that changed since the last sync need any work.
        orders_to_sync = [
            (order, parent_id) for order, parent_id in orders_to_sync
            if _order_versions.get(order.id) != _order_version(order)
        ]
        if ledger:
            record_orders([order for order, _ in orders_to_sync])

        for order, parent_order_id in orders_to_sync:
            order_id = order.id
            status   = order.status
            side     = order.side.upper()
            symbol   = order.symbol
            journal_order_id = parent_order_id or order_id

            filled_price: float | None = None
            filled_qty = order.filled_qty

            if status == "filled" and order.filled_avg_price:
                filled_price = float(order.filled_avg_price)
            if filled_price is not None and order_id not in _logged_fill_ids:
                _logged_fill_ids.add(order_id)
                logger.success(
                    f"{side} filled ✓ {symbol} | "
                    f"qty={filled_qty:.8f} price={filled_price:.8f} order_id={order_id}"
                )

            # Entry fills are not realized exits; child fills link P&L to the parent row.
            # With the order ledger, exits and P&L come from record_orders() instead.
            if ledger:
                if order_id in closed_ids:  # open orders only feed the ledger
                    update_trade(journal_order_id, status)
            elif parent_order_id:
                update_trade(
                    journal_order_id,
                    status,
                    filled_price,
                    exit_order_id=order_id,
                    symbol=symbol,
                )
            elif status == "filled":
                open_trade = get_open_trade(symbol)
                if open_trade and _is_exit_fill(order, open_trade):
                    update_trade(
                        open_trade["order_id"],
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
            # Fills that already existed at startup are marked without sending,
            # and a failed send is not retried, so a broken notifier can't
            # trigger a resend of the whole order history on every sync.
            if status == "filled":
                pending = [
                    (has, send, mark)
                    for has, send, mark in (
                        (discord_has_been_notified, discord_send_fill_update, discord_mark_notified),
                        (telegram_has_been_notified, telegram_send_fill_update, telegram_mark_notified),
                    )
                    if not has(order_id)
                ]
                if pending and _fill_baseline_done:
                    account_line = _build_account_line()
                    for _, send, _ in pending:
                        send(order, account_line)
                for _, _, mark in pending:
                    mark(order_id)

            _order_versions[order_id] = _order_version(order)

        _fill_baseline_done = True

    except Exception as exc:
        logger.error(f"Position sync error: {exc}")

    _ensure_protection()


_PROTECTION_CHECK_SECONDS = 60


def _ensure_protection() -> None:
    """Have the broker (re)place protective exits for every open bot position."""
    try:
        get_broker().ensure_protection()
    except Exception as exc:
        logger.error(f"Protective-exit check failed: {exc}")


def _sellable_gain(symbol: str, position) -> tuple[float, float] | None:
    """
    Return (price, gain %) at the price a close would get now, or None.

    Uses the bid for longs and the ask for shorts, not the last-trade mark.
    """
    if position.avg_entry <= 0 or position.qty <= 0:
        return None
    quote_symbol = symbol if "/" in symbol or not symbol.endswith("USD") else f"{symbol[:-3]}/USD"
    try:
        quote = get_broker().get_latest_quote(quote_symbol)
    except Exception as exc:
        logger.warning(f"{symbol}: Sellable price quote failed: {exc}")
        return None
    is_long = position.side == "long"
    price = quote["bid"] if is_long else quote["ask"]
    if price <= 0:
        return None
    move = price - position.avg_entry if is_long else position.avg_entry - price
    return price, move / position.avg_entry * 100.0


def _check_profit_alerts() -> None:
    """
    Alert once per position when its sellable price is PROFIT_ALERT_PCT past entry.

    Uses the bid for longs and the ask for shorts (what a close would actually
    get), not the broker's last-trade mark. Alert-only: exits are unchanged.
    Keyed by symbol and average entry, so a new position re-arms the alert.
    """
    if config.PROFIT_ALERT_PCT <= 0:
        return
    try:
        positions = get_broker().get_positions()
    except Exception as exc:
        logger.error(f"Profit alert check failed: {exc}")
        return
    for symbol, position in positions.items():
        key = f"profit-alert:{symbol.replace('/', '')}:{position.avg_entry:.10g}"
        if discord_has_been_notified(key) or telegram_has_been_notified(key):
            continue
        sellable = _sellable_gain(symbol, position)
        if sellable is None:
            continue
        price, gain_pct = sellable
        if gain_pct < config.PROFIT_ALERT_PCT:
            continue
        is_long = position.side == "long"
        move = price - position.avg_entry if is_long else position.avg_entry - price
        alert = {
            "symbol": symbol,
            "side": position.side,
            "entry": position.avg_entry,
            "price": price,
            "price_label": "bid" if is_long else "ask",
            "gain_pct": gain_pct,
            "gain_usd": move * position.qty,
            "qty": position.qty,
        }
        logger.success(
            f"PROFIT ALERT ▶ {symbol} {gain_pct:+.2f}% (+${alert['gain_usd']:.2f}) | "
            f"entry={position.avg_entry:.8g} {alert['price_label']}={price:.8g}"
        )
        sent = discord_send_profit_alert(alert) | telegram_send_profit_alert(alert)
        notifiers = config.DISCORD_NOTIFICATIONS_ENABLED or config.TELEGRAM_NOTIFICATIONS_ENABLED
        # Retry next check if every configured notifier failed.
        if sent or not notifiers:
            discord_mark_notified(key)
            telegram_mark_notified(key)


_PROFIT_TRAIL_FILE = Path("logs") / "profit_trail.json"


def _load_trail_peaks() -> dict[str, float]:
    try:
        if _PROFIT_TRAIL_FILE.exists():
            return {k: float(v) for k, v in json.loads(_PROFIT_TRAIL_FILE.read_text(encoding="utf-8")).items()}
    except Exception as exc:
        logger.warning(f"Could not read profit trail file: {exc}")
    return {}


def _save_trail_peaks(peaks: dict[str, float]) -> None:
    try:
        _PROFIT_TRAIL_FILE.parent.mkdir(exist_ok=True)
        _PROFIT_TRAIL_FILE.write_text(json.dumps(peaks), encoding="utf-8")
    except Exception as exc:
        logger.warning(f"Could not persist profit trail: {exc}")


_REENTRY_LOCK_FILE = Path("logs") / "reentry_locks.json"


def _coin_key(symbol: str) -> str:
    """'BTC/USD' and 'BTCUSD' name the same coin."""
    return symbol.replace("/", "").upper()


def _load_reentry_state() -> dict | None:
    try:
        if _REENTRY_LOCK_FILE.exists():
            state = json.loads(_REENTRY_LOCK_FILE.read_text(encoding="utf-8"))
            return {"held": list(state.get("held", [])), "locked": dict(state.get("locked", {}))}
    except Exception as exc:
        logger.warning(f"Could not read re-entry lock file: {exc}")
    return None


def _save_reentry_state(state: dict) -> None:
    try:
        _REENTRY_LOCK_FILE.parent.mkdir(exist_ok=True)
        _REENTRY_LOCK_FILE.write_text(json.dumps(state), encoding="utf-8")
    except Exception as exc:
        logger.warning(f"Could not persist re-entry locks: {exc}")


def _update_reentry_locks(live_positions: dict[str, dict]) -> dict:
    """
    Lock every coin whose position closed since the last check, by any exit.

    Positions held last time are kept in the lock file, so a close that
    happens while the bot is down is still caught on the next start.
    """
    held_now = sorted({_coin_key(s) for s in live_positions})
    state = _load_reentry_state()
    if state is None:               # first run: nothing to compare against yet
        state = {"held": held_now, "locked": {}}
        _save_reentry_state(state)
        return state

    closed = set(state["held"]) - set(held_now)
    now = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d %H:%M:%S")
    for key in sorted(closed):
        state["locked"][key] = now
        logger.info(f"{key}: Position closed; no new entry until its 4h trend leaves uptrend")
    if closed or state["held"] != held_now:
        state["held"] = held_now
        _save_reentry_state(state)
    return state


def _run_profit_trail() -> None:
    """
    Sell a winner once it turns down after reaching its arm level.

    From the first check at or past the coin's arm %, track the best sellable
    price and close at market when the price gives back the coin's trail %
    from it. Both come from profit_targets.json, else the .env defaults. Best
    prices are keyed by symbol and average entry and saved to disk, so a
    restart keeps an armed trail and a new position starts fresh.
    """
    try:
        positions = get_broker().get_positions()
    except Exception as exc:
        logger.error(f"Profit trail check failed: {exc}")
        return
    targets = load_targets()
    peaks = _load_trail_peaks()
    live_keys: set[str] = set()
    changed = False
    for symbol, position in positions.items():
        arm_pct, trail_pct = trail_settings(symbol, targets)
        if arm_pct <= 0 or trail_pct <= 0:
            continue
        key = f"{symbol.replace('/', '')}:{position.avg_entry:.10g}"
        live_keys.add(key)
        sellable = _sellable_gain(symbol, position)
        if sellable is None:
            continue
        price, gain_pct = sellable
        is_long = position.side == "long"
        best = peaks.get(key)
        if best is None:
            if gain_pct < arm_pct:
                continue
            logger.success(
                f"PROFIT TRAIL armed ▶ {symbol} {gain_pct:+.2f}% | entry={position.avg_entry:.8g} "
                f"price={price:.8g}; selling on a {trail_pct:g}% pullback from the best"
            )
            best = price
        best = max(best, price) if is_long else min(best, price)
        if peaks.get(key) != best:
            peaks[key] = best
            changed = True
        trigger = (
            best * (1 - trail_pct / 100.0)
            if is_long
            else best * (1 + trail_pct / 100.0)
        )
        if (price > trigger) if is_long else (price < trigger):
            continue
        logger.warning(
            f"PROFIT TRAIL hit ▶ {symbol} {gain_pct:+.2f}% | best={best:.8g} price={price:.8g} "
            f"trigger={trigger:.8g}; closing position"
        )
        order = close_position(symbol, "profit_trail")
        if order is not None:
            logger.success(f"{symbol}: Profit-trail close submitted — id={order.id}")
            # Leave the peak until the position is gone, so a failed fill retries.
    for key in [k for k in peaks if k not in live_keys]:
        del peaks[key]
        changed = True
    if changed:
        _save_trail_peaks(peaks)


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
    """Reconcile the journal when the broker publishes an order update.

    Polling runners pass None after each pass to trigger a REST reconcile.
    """
    if update is not None:
        order = getattr(update, "order", None)
        logger.info(
            f"Trade update: {getattr(update, 'event', 'unknown')} "
            f"order_id={getattr(order, 'id', order)}"
        )
    sync_open_positions_to_journal()


def _run_trend_flip_exits(
    streamed_data: dict[str, tuple[pd.DataFrame, dict[str, float]]] | None,
    live_positions: dict[str, dict],
) -> None:
    """Close trend-mode positions when their completed 4-hour regime changes."""
    # A streamed scan carries only the coins whose bar just closed; the others
    # are checked when their own bars arrive.
    symbols = config.SYMBOLS if streamed_data is None else list(streamed_data)
    # One request for every open entry order instead of one per coin.
    open_entries: dict[str, list] = {}
    for order in get_open_orders():
        open_entries.setdefault(order.symbol.replace("/", ""), []).append(order)

    for symbol in symbols:
        position_symbol = next(
            (key for key in live_positions if key.replace("/", "") == symbol.replace("/", "")),
            None,
        )
        pending_entries = (
            open_entries.get(symbol.replace("/", ""), []) if position_symbol is None else []
        )
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
        if position_symbol is not None:
            position = live_positions[position_symbol]
            side = position.get("side", "long")
            expected_trend = "downtrend" if side == "short" else "uptrend"
            if trend != expected_trend:
                logger.warning(
                    f"{symbol}: 4-hour trend changed from {expected_trend} to {trend}; "
                    "canceling attached exits and closing position"
                )
                for order in get_open_orders(position_symbol, nested=True):
                    if not cancel_order(order):
                        logger.error(f"{symbol}: Exit order still open; not closing position")
                        break
                else:
                    order = close_position(position_symbol, "trend_flip")
                    if order is not None:
                        logger.success(
                            f"{symbol}: Trend-flip close submitted — id={order.id} "
                            f"old-side={side} new-trend={trend}"
                        )

        if pending_entries:
            momentum = float(calculate_rsi(evaluation_bars["close"], config.RSI_PERIOD).iloc[-1])
            for order in pending_entries:
                side = "short" if order.side == "sell" else "long"
                expected_trend = "downtrend" if side == "short" else "uptrend"
                momentum_aligned = (
                    momentum <= config.TREND_MOMENTUM_RSI_SHORT
                    if side == "short"
                    else momentum >= config.TREND_MOMENTUM_RSI_LONG
                )
                if trend == expected_trend and momentum_aligned:
                    continue
                if cancel_order(order):
                    logger.info(
                        f"{symbol}: Canceled pending {side} entry; "
                        f"4h trend={trend}, hourly RSI={momentum:.1f}"
                    )


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
    _ensure_protection()

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

    reentry = _update_reentry_locks(live_positions) if config.REENTRY_TREND_RESET else None

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

        # ---- Trend reset after an exit ----
        if reentry is not None and _coin_key(symbol) in reentry["locked"]:
            completed = bars.iloc[:-1] if streamed_data is None else bars
            if len(completed) < config.TREND_HTF_EMA_SLOW * 4:
                continue
            if identify_four_hour_trend(completed) == "uptrend":
                logger.debug(
                    f"{symbol}: Closed {reentry['locked'][_coin_key(symbol)]} UTC; "
                    "waiting for the 4h uptrend to reset — skipping"
                )
                continue
            del reentry["locked"][_coin_key(symbol)]
            _save_reentry_state(reentry)
            logger.info(f"{symbol}: 4h trend has left uptrend; re-entry lock cleared")

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
    try:
        broker = get_broker()
    except Exception as exc:
        logger.error(f"Cannot initialise broker {config.BROKER!r}: {exc}")
        sys.exit(1)
    broker.exit_listener = note_exit
    if not broker.supports_trading:
        logger.error(
            f"Broker {broker.name!r} is market-data only; trading is not implemented yet. "
            "Set BROKER=alpaca in .env."
        )
        sys.exit(1)
    try:
        resolve_symbols()
    except Exception as exc:
        logger.error(f"Cannot load the symbol list from {broker.name}: {exc}")
        sys.exit(1)
    if config.ENABLE_SHORT_SELLING and not broker.supports_short:
        logger.warning(f"ENABLE_SHORT_SELLING is on but {broker.name} cannot short — running long-only")
        config.ENABLE_SHORT_SELLING = False

    logger.info(f"  Broker    : {broker.name}")
    logger.info(f"  Mode      : {'PAPER TRADING' if broker.is_paper else '⚠  LIVE TRADING'}")
    logger.info(f"  Symbols   : {len(config.SYMBOLS)} — {', '.join(config.SYMBOLS)}")
    logger.info(f"  Short sell: {'ENABLED' if config.ENABLE_SHORT_SELLING else 'disabled'}")
    logger.info(f"  Max trades/day  : {config.MAX_TRADES_PER_DAY}")
    logger.info(f"  Std risk/trade  : {config.STANDARD_RISK_PCT_PER_TRADE*100:.1f}% of equity")
    logger.info(f"  High risk/trade : {config.HIGH_RISK_PCT_PER_TRADE*100:.1f}% of equity (ATR>={config.HIGH_RISK_ATR_THRESHOLD*100:.1f}%)")
    logger.info(f"  R:R target      : {config.REWARD_RISK_MIN} – {config.REWARD_RISK_TARGET}")
    logger.info(f"  Bar timeframe   : {config.BAR_TIMEFRAME}")
    logger.info(f"  Closed candle   : {config.USE_CLOSED_CANDLE}")
    logger.info(f"  Runtime         : {broker.name} market-data stream")
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
            f"Connected to {broker.name} ✓ | status={account['status']} | "
            f"portfolio=${account['portfolio_value']:.2f}"
        )
    except Exception as exc:
        logger.error(f"Cannot connect to {broker.name}: {exc}")
        logger.error("Check the API keys in your .env file.")
        sys.exit(1)

    sync_open_positions_to_journal()
    log_position_summary()

    runner = broker.create_stream(
        config.SYMBOLS,
        on_bar_close=_handle_stream_bar,
        on_trade_update=_handle_stream_trade_update,
    )

    exit_code = 0
    try:
        runner.start()
        logger.info("Waiting for streamed bars; scans run at completed bar boundaries.")
        next_protection_check = time.monotonic() + _PROTECTION_CHECK_SECONDS
        while _running:
            time.sleep(1)
            # Scans run hourly; check stops more often so a partly filled entry
            # is resolved within ENTRY_FILL_TIMEOUT_SECONDS, not at the next bar.
            if time.monotonic() >= next_protection_check:
                _ensure_protection()
                _run_profit_trail()
                _check_profit_alerts()
                next_protection_check = time.monotonic() + _PROTECTION_CHECK_SECONDS
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
    if exit_code:
        # A scan stuck in a worker thread would block a normal interpreter exit
        # (and so the systemd restart); flush the logs and exit immediately.
        logger.remove()
        os._exit(exit_code)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
