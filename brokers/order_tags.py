"""
Order tags shared by brokers that manage protective exits themselves.

An entry's client order id (Alpaca) or order reference (IBKR) carries its stop
and take-profit ("acx-e-<stop>-<target>-<id>"), so the broker can rebuild the
exits from the exchange's own order history after a restart. Exits the bot
places are tagged "acx-x-...".
"""
from __future__ import annotations

from uuid import uuid4

ENTRY_TAG = "acx-e-"            # entry orders placed by the bot
EXIT_TAG = "acx-x-"             # protective exits placed by ensure_protection()


def _plain(value: float | None) -> str:
    """Fixed-point text; '%g' would give '8.6e-06' for PEPE, whose '-' breaks parsing."""
    return f"{value or 0:.12f}".rstrip("0").rstrip(".") or "0"


def entry_client_id(stop: float | None, target: float | None) -> str:
    return f"{ENTRY_TAG}{_plain(stop)}-{_plain(target)}-{uuid4().hex[:12]}"


def parse_entry_client_id(client_id: str) -> tuple[float | None, float | None]:
    """Return (stop, target) encoded by entry_client_id, None when absent."""
    try:
        stop_s, target_s, _ = client_id[len(ENTRY_TAG):].split("-")
        stop, target = float(stop_s), float(target_s)
    except ValueError:
        return None, None
    return (stop or None), (target or None)
