"""
Risk management.

Handles:
  - Position sizing (risk-based, capped to position limits)
  - Daily trade / loss limit enforcement
  - Trade setup validation (R:R ratio check)
"""
from __future__ import annotations

import config


def calculate_position_qty(entry: float, stop: float) -> float:
    """
    Calculate order quantity (in coin units) based on a fixed dollar risk.

    qty = MAX_RISK_PER_TRADE / |entry - stop|

    The resulting notional (qty * entry) is then clamped to
    [MIN_POSITION_SIZE, MAX_POSITION_SIZE].

    Returns 0.0 if the setup is geometrically invalid.
    """
    if entry <= 0 or stop <= 0 or stop >= entry:
        return 0.0

    stop_distance = entry - stop
    if stop_distance <= 0:
        return 0.0

    raw_qty = config.MAX_RISK_PER_TRADE / stop_distance
    notional = raw_qty * entry

    # Clamp notional to position size limits
    notional = max(config.MIN_POSITION_SIZE, min(notional, config.MAX_POSITION_SIZE))

    # Recalculate qty from clamped notional
    qty = notional / entry
    return round(qty, 8)


def calculate_take_profit(entry: float, stop: float, rr_ratio: float = None) -> float:
    """
    Calculate take-profit price using the configured R:R ratio.

      target = entry + (entry - stop) * rr_ratio
    """
    ratio = rr_ratio if rr_ratio is not None else config.REWARD_RISK_TARGET
    risk = entry - stop
    return round(entry + risk * ratio, 8)


def validate_setup(
    entry: float, stop: float, target: float
) -> tuple[bool, str]:
    """
    Confirm the trade meets minimum risk / reward requirements.

    Returns (is_valid: bool, message: str).
    """
    if entry <= 0 or stop <= 0 or target <= 0:
        return False, "One or more prices are zero or negative"

    if stop >= entry:
        return False, f"Stop {stop:.6f} must be below entry {entry:.6f}"

    if target <= entry:
        return False, f"Target {target:.6f} must be above entry {entry:.6f}"

    risk   = entry - stop
    reward = target - entry

    if risk <= 0:
        return False, "Calculated risk is zero or negative"

    actual_rr = reward / risk

    if actual_rr < config.REWARD_RISK_MIN:
        return (
            False,
            f"R:R {actual_rr:.2f} is below minimum {config.REWARD_RISK_MIN}",
        )

    return True, f"R:R {actual_rr:.2f} ✓"


def check_daily_limits(trades_today: int, daily_pnl: float) -> tuple[bool, str]:
    """
    Return (can_trade: bool, reason: str).

    Blocks trading when:
      - trades_today >= MAX_TRADES_PER_DAY
      - daily_pnl   <= -MAX_DAILY_LOSS
    """
    if trades_today >= config.MAX_TRADES_PER_DAY:
        return (
            False,
            f"Daily trade limit reached ({trades_today}/{config.MAX_TRADES_PER_DAY})",
        )

    if daily_pnl <= -config.MAX_DAILY_LOSS:
        return (
            False,
            f"Daily loss limit reached (${daily_pnl:.2f} / -${config.MAX_DAILY_LOSS:.2f})",
        )

    return True, "Daily limits OK"
