"""
Order placement and position monitoring.

Supports bracket orders (limit or market entry + automatic TP + SL).
Supports both long and short entries (short only when ENABLE_SHORT_SELLING=True).
Falls back gracefully when the API rejects a request.
"""
from __future__ import annotations

from typing import Optional

from alpaca.trading.requests import (
    LimitOrderRequest,
    MarketOrderRequest,
    TakeProfitRequest,
    StopLossRequest,
    GetOrdersRequest,
)
from alpaca.trading.enums import (
    OrderSide,
    TimeInForce,
    OrderClass,
    QueryOrderStatus,
)
from loguru import logger

import config
from trader.alpaca_client import get_trading_client
from trader.risk_manager import RiskProfile, calculate_position_qty
from trader.strategy import TradeSignal


# ---------------------------------------------------------------------------
# Account helpers
# ---------------------------------------------------------------------------

def get_account_info() -> dict:
    """Return key account fields as a plain dict."""
    account = get_trading_client().get_account()
    return {
        "cash":            float(account.cash),
        "portfolio_value": float(account.portfolio_value),
        "buying_power":    float(account.buying_power),
        "status":          str(account.status),
    }


def get_open_positions() -> dict[str, dict]:
    """
    Return a dict keyed by symbol for all currently open positions.

    Example:
        {"BTC/USD": {"qty": 0.001, "avg_entry": 60000.0, "unrealized_pl": 12.5}}
    """
    positions: dict[str, dict] = {}
    try:
        for pos in get_trading_client().get_all_positions():
            positions[pos.symbol] = {
                "qty":           float(pos.qty),
                "avg_entry":     float(pos.avg_entry_price),
                "market_value":  float(pos.market_value),
                "unrealized_pl": float(pos.unrealized_pl),
                "side":          str(pos.side),
            }
    except Exception as exc:
        logger.error(f"Error fetching positions: {exc}")
    return positions


def get_open_orders(symbol: str | None = None) -> list:
    """Return all open/pending orders, optionally filtered by symbol."""
    try:
        request = GetOrdersRequest(
            status=QueryOrderStatus.OPEN,
            symbols=[symbol] if symbol else None,
        )
        return get_trading_client().get_orders(request)
    except Exception as exc:
        logger.error(f"Error fetching open orders: {exc}")
        return []


# ---------------------------------------------------------------------------
# Order placement
# ---------------------------------------------------------------------------

def _check_buying_power(qty: float, entry: float, symbol: str, buying_power: float | None = None) -> bool:
    """Return True if the account has enough available buying power for the trade.

    Pass *buying_power* to reuse an already-fetched value and avoid a second
    API call.  If omitted it is fetched from Alpaca.
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
    """Return True if Alpaca already has an open position in this symbol."""
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
        "rr":           signal.rr,
        "status":       str(order.status),
        "reason":       signal.reason,
        "regime":       signal.regime,
        "risk_profile": signal.risk_profile,
    }


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

    order_side = OrderSide.SELL if signal.side == "short" else OrderSide.BUY
    logger.info(
        f"Placing LIMIT bracket {signal.side.upper()} — {signal.symbol} "
        f"qty={qty:.8f} entry={signal.entry:.6f} "
        f"stop={signal.stop:.6f} target={signal.target:.6f} "
        f"[{signal.risk_profile}]"
    )

    try:
        order = get_trading_client().submit_order(
            LimitOrderRequest(
                symbol=signal.symbol,
                qty=qty,
                side=order_side,
                time_in_force=TimeInForce.GTC,
                limit_price=round(signal.entry, 8),
                order_class=OrderClass.BRACKET,
                take_profit=TakeProfitRequest(limit_price=round(signal.target, 8)),
                stop_loss=StopLossRequest(stop_price=round(signal.stop, 8)),
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

    order_side = OrderSide.SELL if signal.side == "short" else OrderSide.BUY
    logger.info(
        f"Placing MARKET bracket {signal.side.upper()} — {signal.symbol} "
        f"qty={qty:.8f} stop={signal.stop:.6f} target={signal.target:.6f} "
        f"[{signal.risk_profile}]"
    )

    try:
        order = get_trading_client().submit_order(
            MarketOrderRequest(
                symbol=signal.symbol,
                qty=qty,
                side=order_side,
                time_in_force=TimeInForce.GTC,
                order_class=OrderClass.BRACKET,
                take_profit=TakeProfitRequest(limit_price=round(signal.target, 8)),
                stop_loss=StopLossRequest(stop_price=round(signal.stop, 8)),
            )
        )
        logger.success(f"{signal.symbol}: Market order submitted — id={order.id} status={order.status}")
        return _result_dict(order, signal, qty)
    except Exception as exc:
        logger.error(f"{signal.symbol}: Market bracket order failed — {exc}")
        return None


def place_order(signal: TradeSignal, profile: RiskProfile) -> Optional[dict]:
    """
    Place the appropriate order type based on config.USE_LIMIT_ORDERS.
    Falls back to market order if the limit order fails.
    """
    if config.USE_LIMIT_ORDERS:
        result = place_limit_bracket_order(signal, profile)
        if result is None:
            logger.warning(f"{signal.symbol}: Limit order failed — attempting market order")
            result = place_market_bracket_order(signal, profile)
        return result
    return place_market_bracket_order(signal, profile)


def cancel_open_buy_orders(symbol: str | None = None) -> None:
    """
    Cancel all open BUY orders (optionally scoped to one symbol).

    Call this during shutdown or when a daily limit is hit mid-cycle.
    """
    client = get_trading_client()
    for order in get_open_orders(symbol):
        if str(order.side).lower() == "buy":
            try:
                client.cancel_order_by_id(order.id)
                logger.info(f"Cancelled order {order.id} ({order.symbol})")
            except Exception as exc:
                logger.error(f"Could not cancel order {order.id}: {exc}")
