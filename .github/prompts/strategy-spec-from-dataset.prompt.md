---
name: Strategy Spec From Dataset
description: Generate a complete strategy specification from available market data with codable buy/sell rules, risk controls, and module-level implementation steps.
argument-hint: Describe dataset, symbols, timeframe, and constraints
author: GitHub Copilot
agent: Crypto Trading Methodologist
model: GPT-5 (copilot)
---
Create a full strategy specification from the provided dataset context.

Requirements:
- Use only causal signals that could be known at decision time.
- Propose deterministic buy/sell/hold/no-trade rules.
- Include both long and short logic when feasible.
- Provide two risk profiles: standard and higher-risk.
- Include stop-loss, take-profit, time-stop, and circuit-breaker behavior.
- Map the design into implementation steps for this workspace modules:
  - trader/strategy.py
  - trader/risk_manager.py
  - trader/order_manager.py
  - trader/journal.py
- Define a backtest and walk-forward validation plan.

Output sections:
1. Data Understanding
2. Signal Definitions
3. Trading Rules
4. Risk and Position Sizing
5. Implementation Plan by Module
6. Validation Plan
7. Open Questions