"""
Risk management.

Handles:
  - Dual risk profile selection (auto-selected by volatility regime)
  - Position sizing (percentage-of-equity risk, notional clamped, true risk enforced)
  - Daily trade / loss limit enforcement
  - Drawdown high-water-mark tracking and pause enforcement
  - Trade setup validation (R:R ratio check)
"""
from __future__ import annotations

from dataclasses import dataclass
from loguru import logger

import config

# ---------------------------------------------------------------------------
# High-water mark tracking (in-memory; resets on process restart)
# ---------------------------------------------------------------------------
_equity_hwm: float = 0.0


def update_hwm(portfolio_value: float) -> None:
    """Update the equity high-water mark if current value is a new high."""
    global _equity_hwm
    if portfolio_value > _equity_hwm:
        _equity_hwm = portfolio_value


# ---------------------------------------------------------------------------
# Risk profile
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RiskProfile:
    name: str
    risk_pct_per_trade: float
    max_daily_loss_pct: float
    max_drawdown_pct: float
    max_open_positions: int


STANDARD_PROFILE = RiskProfile(
    name="standard",
    risk_pct_per_trade=config.STANDARD_RISK_PCT_PER_TRADE,
    max_daily_loss_pct=config.STANDARD_MAX_DAILY_LOSS_PCT,
    max_drawdown_pct=config.STANDARD_MAX_DRAWDOWN_PCT,
    max_open_positions=config.STANDARD_MAX_OPEN_POSITIONS,
)

HIGH_RISK_PROFILE = RiskProfile(
    name="higher-risk",
    risk_pct_per_trade=config.HIGH_RISK_PCT_PER_TRADE,
    max_daily_loss_pct=config.HIGH_RISK_MAX_DAILY_LOSS_PCT,
    max_drawdown_pct=config.HIGH_RISK_MAX_DRAWDOWN_PCT,
    max_open_positions=config.HIGH_RISK_MAX_OPEN_POSITIONS,
)


def select_risk_profile(atr_pct: float) -> RiskProfile:
    """
    Auto-select the risk profile based on current market ATR as % of price.

    When volatility is elevated (ATR% > HIGH_RISK_ATR_THRESHOLD) the
    higher-risk profile is chosen because wide swings allow larger stop
    distances and larger position sizing relative to equity.

    Logs which profile is active and why.
    """
    if atr_pct >= config.HIGH_RISK_ATR_THRESHOLD:
        logger.warning(
            f"HIGH-RISK profile selected: ATR% {atr_pct*100:.2f}% >= threshold "
            f"{config.HIGH_RISK_ATR_THRESHOLD*100:.2f}%. Wider limits, stricter kill-switches."
        )
        return HIGH_RISK_PROFILE
    logger.debug(
        f"Standard profile selected: ATR% {atr_pct*100:.2f}% < threshold "
        f"{config.HIGH_RISK_ATR_THRESHOLD*100:.2f}%"
    )
    return STANDARD_PROFILE


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------

def calculate_position_qty(
    entry: float,
    stop: float,
    portfolio_value: float,
    profile: RiskProfile,
) -> float:
    """
    Calculate order quantity (in coin units) using percentage-of-equity risk.

    Steps:
      1. Derive max risk in USD from profile.risk_pct_per_trade * portfolio.
      2. Compute raw qty = risk_usd / stop_distance.
      3. Clamp resulting notional to [MIN_POSITION_SIZE, MAX_POSITION_SIZE].
      4. Recompute final qty and verify effective risk does not exceed cap.

    Returns 0.0 if the setup is geometrically invalid or risk cap breached.
    """
    if entry <= 0 or stop <= 0 or stop >= entry or portfolio_value <= 0:
        return 0.0

    stop_distance = entry - stop
    if stop_distance <= 0:
        return 0.0

    max_risk_usd = portfolio_value * profile.risk_pct_per_trade
    raw_qty = max_risk_usd / stop_distance
    notional = raw_qty * entry

    # Clamp notional to configured position size range
    notional = max(config.MIN_POSITION_SIZE, min(notional, config.MAX_POSITION_SIZE))
    qty = notional / entry

    # Enforce the true risk cap after clamping — upward clamp can increase risk
    effective_risk_usd = qty * stop_distance
    if effective_risk_usd > max_risk_usd * 1.05:   # 5 % tolerance for rounding
        logger.warning(
            f"Position sizing: effective risk ${effective_risk_usd:.4f} exceeds "
            f"cap ${max_risk_usd:.4f} after notional clamp — rejecting"
        )
        return 0.0

    return round(qty, 8)


def calculate_take_profit(entry: float, stop: float, rr_ratio: float = None) -> float:
    """
    Calculate take-profit price using the configured R:R ratio.

      target = entry + (entry - stop) * rr_ratio
    """
    ratio = rr_ratio if rr_ratio is not None else config.REWARD_RISK_TARGET
    risk = entry - stop
    return round(entry + risk * ratio, 8)


def calculate_short_take_profit(entry: float, stop: float, rr_ratio: float = None) -> float:
    """
    Calculate take-profit price for a short position.

      target = entry - (stop - entry) * rr_ratio
    """
    ratio = rr_ratio if rr_ratio is not None else config.REWARD_RISK_TARGET
    risk = stop - entry
    return round(entry - risk * ratio, 8)


def validate_setup(
    entry: float,
    stop: float,
    target: float,
    side: str = "long",
) -> tuple[bool, str]:
    """
    Confirm the trade meets minimum risk / reward requirements.

    Returns (is_valid: bool, message: str).
    Supports both long and short geometries.
    """
    if entry <= 0 or stop <= 0 or target <= 0:
        return False, "One or more prices are zero or negative"

    if side == "long":
        if stop >= entry:
            return False, f"Long stop {stop:.6f} must be below entry {entry:.6f}"
        if target <= entry:
            return False, f"Long target {target:.6f} must be above entry {entry:.6f}"
        risk   = entry - stop
        reward = target - entry
    else:
        if stop <= entry:
            return False, f"Short stop {stop:.6f} must be above entry {entry:.6f}"
        if target >= entry:
            return False, f"Short target {target:.6f} must be below entry {entry:.6f}"
        risk   = stop - entry
        reward = entry - target

    if risk <= 0:
        return False, "Calculated risk is zero or negative"

    actual_rr = reward / risk

    if actual_rr < config.REWARD_RISK_MIN:
        return (
            False,
            f"R:R {actual_rr:.2f} is below minimum {config.REWARD_RISK_MIN}",
        )

    return True, f"R:R {actual_rr:.2f} ✓"


def check_daily_limits(
    trades_today: int,
    daily_pnl: float,
    portfolio_value: float,
    profile: RiskProfile,
) -> tuple[bool, str]:
    """
    Return (can_trade: bool, reason: str).

    Blocks trading when:
      - trades_today >= MAX_TRADES_PER_DAY
      - daily_pnl is a loss exceeding profile.max_daily_loss_pct of portfolio
      - drawdown from HWM exceeds profile.max_drawdown_pct (if HWM is set)
    """
    if trades_today >= config.MAX_TRADES_PER_DAY:
        return (
            False,
            f"Daily trade limit reached ({trades_today}/{config.MAX_TRADES_PER_DAY})",
        )

    if portfolio_value > 0:
        update_hwm(portfolio_value)

        # Percentage-based daily loss check
        daily_loss_cap = portfolio_value * profile.max_daily_loss_pct
        if daily_pnl <= -daily_loss_cap:
            return (
                False,
                f"[{profile.name}] Daily loss limit: ${daily_pnl:.2f} / "
                f"-${daily_loss_cap:.2f} ({profile.max_daily_loss_pct*100:.1f}% of equity)",
            )

        # Drawdown from high-water mark
        if _equity_hwm > 0:
            drawdown_pct = (_equity_hwm - portfolio_value) / _equity_hwm
            if drawdown_pct >= profile.max_drawdown_pct:
                return (
                    False,
                    f"[{profile.name}] Drawdown pause: {drawdown_pct*100:.1f}% from HWM "
                    f"${_equity_hwm:.2f} (limit {profile.max_drawdown_pct*100:.1f}%)",
                )
    else:
        # Fallback: dollar-based limit when portfolio value is unknown
        if daily_pnl <= -config.MAX_DAILY_LOSS:
            return (
                False,
                f"Daily loss limit reached (${daily_pnl:.2f} / -${config.MAX_DAILY_LOSS:.2f})",
            )

    return True, f"Daily limits OK [{profile.name}]"
