"""
AlpacaCryptoTrader Configuration
=================================
All tunable parameters are here. Edit to match your risk tolerance.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Alpaca API credentials (loaded from .env)
# ---------------------------------------------------------------------------
ALPACA_API_KEY: str = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY: str = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_PAPER: bool = os.getenv("ALPACA_PAPER", "true").lower() == "true"
DISCORD_WEBHOOK_URL: str = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
DISCORD_NOTIFICATIONS_ENABLED: bool = bool(DISCORD_WEBHOOK_URL)

# ---------------------------------------------------------------------------
# Symbols to watch
# ---------------------------------------------------------------------------
SYMBOLS: list[str] = ["BTC/USD", "ETH/USD", "SOL/USD"]

# ---------------------------------------------------------------------------
# Risk management (beginner plan: $100 account)
# ---------------------------------------------------------------------------
MAX_TRADES_PER_DAY: int = 2          # hard cap on entries per day
MAX_RISK_PER_TRADE: float = 1.00     # USD risked per trade
MAX_DAILY_LOSS: float = 2.00         # USD — halt trading after this loss

MIN_POSITION_SIZE: float = 25.00     # USD notional minimum
MAX_POSITION_SIZE: float = 50.00     # USD notional maximum

# Percentage of total portfolio value the bot is allowed to deploy.
# 0.75 = 75% trading capital, 25% kept as reserve cash.
MAX_ACCOUNT_USAGE_PCT: float = 0.75

# ---------------------------------------------------------------------------
# Reward / risk targets
# ---------------------------------------------------------------------------
REWARD_RISK_MIN: float = 1.5         # minimum R:R to take a trade
REWARD_RISK_TARGET: float = 2.1      # R:R used to set the take-profit price

# ---------------------------------------------------------------------------
# Strategy parameters
# ---------------------------------------------------------------------------
EMA_SHORT: int = 9                   # fast EMA period
EMA_LONG: int = 20                   # slow EMA period

# Price must be within this fraction of VWAP to qualify as a pullback
VWAP_PULLBACK_THRESHOLD: float = 0.005   # 0.5 %

# Volume on the bounce bar must exceed average volume by this multiplier
VOLUME_MULTIPLIER: float = 1.2

# EMA9/EMA20 must differ by at least this fraction of price to avoid sideways
MIN_EMA_SEPARATION_PCT: float = 0.0010  # 0.1 %

# ATR as % of price must exceed this to avoid sideways
MIN_ATR_PCT: float = 0.0050              # 0.5 %

# Maximum bid/ask spread (as % of mid) before skipping a symbol
MAX_SPREAD_PCT: float = 0.50            # 0.5 %

# How many bars back to look for the pullback swing low (stop placement)
PULLBACK_LOW_BARS: int = 3

# ---------------------------------------------------------------------------
# Bar / data settings
# ---------------------------------------------------------------------------
# Supported values: "1Min", "5Min", "15Min", "1Hour", "1Day"
BAR_TIMEFRAME: str = "15Min"
BARS_LOOKBACK: int = 100             # bars fetched per symbol per cycle

# ---------------------------------------------------------------------------
# Order type preference
# ---------------------------------------------------------------------------
# True  → limit order for entry (preferred, less slippage)
# False → market order for entry (fills immediately, more slippage)
USE_LIMIT_ORDERS: bool = True

# ---------------------------------------------------------------------------
# Loop settings
# ---------------------------------------------------------------------------
POLL_INTERVAL_SECONDS: int = 60      # seconds between full scan cycles
