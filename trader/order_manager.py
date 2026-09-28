"""
Order placement and position monitoring through the configured broker.

Supports bracket orders (limit or market entry + automatic TP + SL).
Supports both long and short entries; shorts are refused unless
ENABLE_SHORT_SELLING is on and the broker supports them.
Falls back gracefully when the API rejects a request.
"""
from __future__ import annotations

from typing import Optional
from uuid import uuid4

from loguru import logger

import config
from brokers import get_broker
from brokers.base import Order, OrderRequest
from trader.risk_manager import RiskProfile, calculate_position_qty
from trader.strategy import TradeSignal


# ---------------------------------------------------------------------------
# Account helpers
# ---------------------------------------------------------------------------

def get_account_info() -> dict:
    """Return key account fields as a plain dict."""
    return get_broker().get_account().as_dict()


def get_open_positions() -> dict[str, dict]:
    """
    Return a dict keyed by symbol for all currently open positions.

    Example:
        {"BTC/USD": {"qty": 0.001, "avg_entry": 60000.0, "unrealized_pl": 12.5, "side": "long"}}
    """
    try:
        return {symbol: pos.as_dict() for symbol, pos in get_broker().get_positions().items()}
    except Exception as exc:
        logger.error(f"Error fetching positions: {exc}")
        return {}


def get_open_orders(symbol: str | None = None, nested: bool = False) -> list[Order]:
    """Return all open/pending orders, optionally filtered by symbol."""
    try:
        return get_broker().get_open_orders(symbol, nested=nested)
    except Exception as exc:
        logger.error(f"Error fetching open orders: {exc}")
        return []


# ---------------------------------------------------------------------------
# Order placement
# ---------------------------------------------------------------------------

def _check_buying_power(qty: float, entry: float, symbol: str, buying_power: float | None = None) -> bool:
    """Return True if the account has enough available buying power for the trade.

    Pass *buying_power* to reuse an already-fetched value and avoid a second
    API call.  If omitted it is fetched from the broker.
    """
    notional = qty * entry
    bp = buying_power if buying_power is not None else get_account_info()["buying_power"]

    if notional > bp:
        logger.warning(
            f"{symbol}: Insufficient buying power "
            f"(need ${notional:.2f}, have ${bp:.2f})"
        )
        return False

    return True


def _has_open_position(symbol: str) -> bool:
    """Return True if the broker already has an open position in this symbol."""
    positions = get_open_positions()
    if symbol in positions:
        logger.debug(f"{symbol}: Live position already open — skipping")
        return True
    return False


def _result_dict(order, signal: TradeSignal, qty: float) -> dict:
    return {
        "order_id":     str(order.id),
        "symbol":       signal.symbol,
        "side":         signal.side.upper(),
        "qty":          qty,
        "entry":        signal.entry,
        "stop":         signal.stop,
        "target":       signal.target,
        "target1":      signal.target1,
        "rr":           signal.rr,
        "status":       order.status,
        "reason":       signal.reason,
        "regime":       signal.regime,
        "risk_profile": signal.risk_profile,
        "exit_on_trend_flip": signal.exit_on_trend_flip,
    }


def _entry_side(signal: TradeSignal) -> str | None:
    """Order side for the entry, or None when a short is not allowed."""
    if signal.side != "short":
        return "buy"
    if not (config.ENABLE_SHORT_SELLING and get_broker().supports_short):
        logger.warning(f"{signal.symbol}: Short entry refused — short selling unavailable on {config.BROKER}")
        return None
    return "sell"


# ---------------------------------------------------------------------------
# Order placement
# ---------------------------------------------------------------------------

def place_limit_bracket_order(signal: TradeSignal, profile: RiskProfile) -> Optional[dict]:
    """Place a bracket order with a LIMIT entry."""
    if _has_open_position(signal.symbol):
        return None

    account = get_account_info()
    qty = calculate_position_qty(signal.entry, signal.stop, account["portfolio_value"], profile, signal.side)
    if qty <= 0:
        logger.error(f"{signal.symbol}: Position qty is zero — check stop distance or risk cap")
        return None

    if not _check_buying_power(qty, signal.entry, signal.symbol, account["buying_power"]):
        return None

    order_side = _entry_side(signal)
    if order_side is None:
        return None
    logger.info(
        f"Placing LIMIT bracket {signal.side.upper()} — {signal.symbol} "
        f"qty={qty:.8f} entry={signal.entry:.6f} "
        f"stop={signal.stop:.6f} target={signal.target:.6f} "
        f"[{signal.risk_profile}]"
    )

    try:
        order = get_broker().submit_order(
            OrderRequest(
                symbol=signal.symbol,
                side=order_side,
                qty=qty,
                order_type="limit",
                limit_price=signal.entry,
                take_profit=signal.target,
                stop_loss=signal.stop,
            )
        )
        logger.success(f"{signal.symbol}: Order submitted — id={order.id} status={order.status}")
        return _result_dict(order, signal, qty)
    except Exception as exc:
        logger.error(f"{signal.symbol}: Limit bracket order failed — {exc}")
        return None


def place_market_bracket_order(signal: TradeSignal, profile: RiskProfile) -> Optional[dict]:
    """Place a bracket order with a MARKET entry."""
    if _has_open_position(signal.symbol):
        return None

    account = get_account_info()
    qty = calculate_position_qty(signal.entry, signal.stop, account["portfolio_value"], profile, signal.side)
    if qty <= 0:
        logger.error(f"{signal.symbol}: Position qty is zero — check stop distance or risk cap")
        return None

    if not _check_buying_power(qty, signal.entry, signal.symbol, account["buying_power"]):
        return None

    order_side = _entry_side(signal)
    if order_side is None:
        return None
    logger.info(
        f"Placing MARKET bracket {signal.side.upper()} — {signal.symbol} "
        f"qty={qty:.8f} stop={signal.stop:.6f} target={signal.target:.6f} "
        f"[{signal.risk_profile}]"
    )

    try:
        order = get_broker().submit_order(
            OrderRequest(
                symbol=signal.symbol,
                side=order_side,
                qty=qty,
                order_type="market",
                take_profit=signal.target,
                stop_loss=signal.stop,
            )
        )
        logger.success(f"{signal.symbol}: Market order submitted — id={order.id} status={order.status}")
        return _result_dict(order, signal, qty)
    except Exception as exc:
        logger.error(f"{signal.symbol}: Market bracket order failed — {exc}")
        return None


def place_trend_order(signal: TradeSignal, profile: RiskProfile) -> Optional[dict]:
    """Place an entry protected by a stop, leaving the trend flip as the target."""
    if _has_open_position(signal.symbol):
        return None

    account = get_account_info()
    qty = calculate_position_qty(
        signal.entry, signal.stop, account["portfolio_value"], profile, signal.side
    )
    if qty <= 0:
        logger.error(f"{signal.symbol}: Position qty is zero — check stop distance or risk cap")
        return None
    if not _check_buying_power(qty, signal.entry, signal.symbol, account["buying_power"]):
        return None

    order_side = _entry_side(signal)
    if order_side is None:
        return None
    order_request = OrderRequest(
        symbol=signal.symbol,
        side=order_side,
        qty=qty,
        order_type="limit" if config.USE_LIMIT_ORDERS else "market",
        limit_price=signal.entry if config.USE_LIMIT_ORDERS else None,
        stop_loss=signal.stop,
    )

    try:
        order = get_broker().submit_order(order_request)
        logger.success(
            f"{signal.symbol}: Trend-following {signal.side.upper()} order submitted "
            f"with stop={signal.stop:.8f}; exit target is the 4h trend flip"
        )
        result = _result_dict(order, signal, qty)
        return result
    except Exception as exc:
        logger.error(f"{signal.symbol}: Trend-following order failed — {exc}")
        return None

def _submit_breakout_leg(signal: TradeSignal, qty: float, target: float):
    """Submit one independently protected breakout exit leg."""
    broker = get_broker()
    if config.USE_LIMIT_ORDERS:
        try:
            return broker.submit_order(
                OrderRequest(
                    symbol=signal.symbol,
                    side="buy",
                    qty=qty,
                    order_type="limit",
                    limit_price=signal.entry,
                    take_profit=target,
                    stop_loss=signal.stop,
                )
            )
        except Exception as exc:
            logger.warning(f"{signal.symbol}: Breakout limit leg failed; trying market — {exc}")

    try:
        return broker.submit_order(
            OrderRequest(
                symbol=signal.symbol,
                side="buy",
                qty=qty,
                order_type="market",
                take_profit=target,
                stop_loss=signal.stop,
            )
        )
    except Exception as exc:
        logger.error(f"{signal.symbol}: Breakout bracket leg failed — {exc}")
        return None


def place_breakout_bracket_orders(
    signal: TradeSignal,
    profile: RiskProfile,
) -> Optional[dict]:
    """Split risk-sized quantity across separate TP1 and TP2 bracket orders."""
    if signal.target1 is None or _has_open_position(signal.symbol):
        return None

    account = get_account_info()
    total_qty = calculate_position_qty(
        signal.entry, signal.stop, account["portfolio_value"], profile, "long"
    )
    if total_qty <= 0 or not _check_buying_power(
        total_qty, signal.entry, signal.symbol, account["buying_power"]
    ):
        logger.error(f"{signal.symbol}: Breakout quantity or buying-power check failed")
        return None

    first_qty = round(total_qty / 2.0, 8)
    second_qty = round(total_qty - first_qty, 8)
    if first_qty <= 0 or second_qty <= 0:
        logger.error(f"{signal.symbol}: Quantity too small to split across both targets")
        return None

    legs = []
    for qty, target in ((first_qty, signal.target1), (second_qty, signal.target)):
        order = _submit_breakout_leg(signal, qty, target)
        if order is None:
            if not legs:
                return None
            logger.error(
                f"{signal.symbol}: TP2 leg was rejected; retaining the accepted "
                "stop-protected TP1 leg at half size"
            )
            break
        leg = _result_dict(order, signal, qty)
        leg["target"] = target
        legs.append(leg)
        logger.success(
            f"{signal.symbol}: Breakout leg submitted qty={qty:.8f} "
            f"target={target:.8f} id={order.id}"
        )

    result = dict(legs[0])
    trade_group_id = str(uuid4())
    for leg in legs:
        leg["entry_group_id"] = trade_group_id
    result.update(
        qty=round(sum(leg["qty"] for leg in legs), 8),
        target=signal.target,
        target1=signal.target1,
        legs=legs,
        entry_group_id=trade_group_id,
        status="accepted" if len(legs) == 2 else "partial-legs",
    )
    return result


def place_order(signal: TradeSignal, profile: RiskProfile) -> Optional[dict]:
    """
    Place the appropriate order type based on config.USE_LIMIT_ORDERS.
    Falls back to market order if the limit order fails.
    """
    if signal.regime == "breakout-long":
        return place_breakout_bracket_orders(signal, profile)

    if signal.exit_on_trend_flip:
        return place_trend_order(signal, profile)

    if config.USE_LIMIT_ORDERS:
        result = place_limit_bracket_order(signal, profile)
        if result is None:
            logger.warning(f"{signal.symbol}: Limit order failed — attempting market order")
            result = place_market_bracket_order(signal, profile)
        return result
    return place_market_bracket_order(signal, profile)


def cancel_open_buy_orders(symbol: str | None = None) -> None:
    """
    Cancel open parent entry orders on either side (optionally scoped to one symbol).

    Nested protective exit orders are excluded so open positions remain protected.
    """
    for order in get_open_orders(symbol):
        cancel_order(order)


def cancel_order(order: Order) -> bool:
    """Cancel one order; return True on success."""
    try:
        get_broker().cancel_order(order.id)
        logger.info(f"Cancelled order {order.id} ({order.symbol}, {order.side})")
        return True
    except Exception as exc:
        logger.error(f"Could not cancel order {order.id}: {exc}")
        return False


def close_position(symbol: str) -> Order | None:
    """Flatten the position in *symbol* at market."""
    try:
        return get_broker().close_position(symbol)
    except Exception as exc:
        logger.error(f"{symbol}: Close position failed — {exc}")
        return None
