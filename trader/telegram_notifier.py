"""
Telegram notifications for trade events.

Uses Telegram Bot API sendMessage endpoint and stores a small local cache of
filled order IDs to avoid duplicate notifications across scan cycles.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib import parse, request

from loguru import logger

import config

_NOTIFIED_FILE = Path("logs") / "telegram_notified_orders.json"


def _load_notified_ids() -> set[str]:
    if not _NOTIFIED_FILE.exists():
        return set()
    try:
        data = json.loads(_NOTIFIED_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return {str(x) for x in data}
    except Exception as exc:
        logger.error(f"Could not read Telegram notified cache: {exc}")
    return set()


def _save_notified_ids(order_ids: set[str]) -> None:
    _NOTIFIED_FILE.parent.mkdir(exist_ok=True)
    try:
        _NOTIFIED_FILE.write_text(
            json.dumps(sorted(order_ids), indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.error(f"Could not write Telegram notified cache: {exc}")


def has_been_notified(order_id: str) -> bool:
    return order_id in _load_notified_ids()


def mark_notified(order_id: str) -> None:
    order_ids = _load_notified_ids()
    order_ids.add(order_id)
    _save_notified_ids(order_ids)


def _send_message(message: str) -> bool:
    if not config.TELEGRAM_NOTIFICATIONS_ENABLED:
        return False

    endpoint = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = parse.urlencode(
        {
            "chat_id": config.TELEGRAM_CHAT_ID,
            "text": message[:3900],
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")

    req = request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    try:
        with request.urlopen(req, timeout=10) as response:
            if 200 <= response.status < 300:
                return True
            logger.error(f"Telegram API returned status {response.status}")
            return False
    except Exception as exc:
        logger.error(f"Telegram message send failed: {exc}")
        return False


def send_buy_submitted(order_info: dict[str, Any], account_line: str) -> bool:
    message = (
        "BUY ORDER SUBMITTED\n"
        f"Symbol: {order_info.get('symbol', '')}\n"
        f"Qty: {float(order_info.get('qty', 0)):.8f}\n"
        f"Entry: {float(order_info.get('entry', 0)):.8f}\n"
        f"Stop: {float(order_info.get('stop', 0)):.8f}\n"
        f"Target: {float(order_info.get('target', 0)):.8f}\n"
        f"R:R: {float(order_info.get('rr', 0)):.2f}\n"
        f"Reason: {order_info.get('reason', '')}\n"
        f"{account_line}"
    )
    return _send_message(message)


def send_sell_submitted(order_info: dict[str, Any], account_line: str) -> bool:
    message = (
        "SELL EXIT ORDERS ARMED\n"
        f"Symbol: {order_info.get('symbol', '')}\n"
        f"Qty: {float(order_info.get('qty', 0)):.8f}\n"
        f"Take-profit: {float(order_info.get('target', 0)):.8f}\n"
        f"Stop-loss: {float(order_info.get('stop', 0)):.8f}\n"
        f"Linked BUY order: {order_info.get('order_id', '')}\n"
        f"{account_line}"
    )
    return _send_message(message)


def send_fill_update(order: Any, account_line: str) -> bool:
    side = str(getattr(order, "side", "")).upper()
    status = str(getattr(order, "status", "")).lower()

    if status != "filled":
        return False

    symbol = str(getattr(order, "symbol", ""))
    qty = float(getattr(order, "filled_qty", 0) or 0)
    price = float(getattr(order, "filled_avg_price", 0) or 0)
    order_id = str(getattr(order, "id", ""))

    action = "BUY FILLED" if "BUY" in side else "SELL FILLED"
    explain = (
        f"Executed {action.lower()} for {symbol} "
        f"at {price:.8f} with qty {qty:.8f}."
    )

    message = (
        f"{action}\n"
        f"Symbol: {symbol}\n"
        f"Qty: {qty:.8f}\n"
        f"Price: {price:.8f}\n"
        f"Order ID: {order_id}\n"
        f"Explanation: {explain}\n"
        f"{account_line}"
    )
    return _send_message(message)
