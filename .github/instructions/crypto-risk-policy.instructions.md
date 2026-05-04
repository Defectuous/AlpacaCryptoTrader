---
name: Crypto Risk Policy
description: Use when implementing or modifying crypto strategy, risk sizing, order execution, or stop-loss logic. Enforces hard risk limits, profile selection, and circuit-breaker behavior.
applyTo: trader/**/*.py
---
# Crypto Risk Policy

## Objective
Keep the trader survivable under volatility spikes while allowing controlled risk profile selection.

## Required Risk Profiles
Every strategy update must support both profiles:

- Standard profile:
  - max_risk_per_trade: 1.0% of equity
  - max_daily_loss: 3.0% of equity
  - max_total_drawdown_pause: 12.0% from equity high-water mark
  - max_open_positions: 3

- Higher-risk profile:
  - max_risk_per_trade: 2.0% of equity
  - max_daily_loss: 5.0% of equity
  - max_total_drawdown_pause: 18.0% from equity high-water mark
  - max_open_positions: 5

## Hard Safety Rules
- Never increase position size after losses to "recover".
- Never place a new order without a defined invalidation level.
- Always calculate position size from stop distance and risk budget.
- Always reject trades if spread, slippage estimate, or liquidity filter is out of bounds.
- Pause new entries when daily loss or drawdown guardrails are breached.

## Strategy Integration Requirements
- Keep regime logic explicit: trend, mean-reversion, and no-trade states.
- Include no-trade filters for low liquidity and abnormal volatility.
- Ensure all thresholds are configurable through config and not hardcoded in strategy logic.

## Logging Requirements
Every order decision should log:
- timestamp, symbol, side, regime
- entry rationale and signal values
- stop distance, target distance, position size
- selected risk profile and active guardrails
- expected slippage/spread checks and pass/fail reason

## Validation Requirements
- Backtests must include out-of-sample periods and walk-forward checks.
- Report expectancy, hit rate, max drawdown, turnover, and profit factor.
- Flag overfitting risk if performance degrades materially outside training periods.