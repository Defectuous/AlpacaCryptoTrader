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
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_NOTIFICATIONS_ENABLED: bool = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

# ---------------------------------------------------------------------------
# Symbols to watch
# ---------------------------------------------------------------------------
# High-liquidity symbols only — DOGE and ADA removed (consistently fail the
# MIN_LIQUIDITY_VOLUME_USD filter at 15-min resolution).
SYMBOLS: list[str] = [
	"BTC/USD",
	"ETH/USD",
	"SOL/USD",
	"LTC/USD",
	"LINK/USD",
	"AVAX/USD",
	"XRP/USD",
]

# ---------------------------------------------------------------------------
# Risk profiles  (auto-selected based on realised volatility regime)
# ---------------------------------------------------------------------------
# Standard — conservative defaults
STANDARD_RISK_PCT_PER_TRADE: float = 0.010   # 1.0% of portfolio equity
STANDARD_MAX_DAILY_LOSS_PCT: float  = 0.030   # 3.0% of portfolio equity
STANDARD_MAX_DRAWDOWN_PCT: float    = 0.120   # 12 % from equity HWM → pause
STANDARD_MAX_OPEN_POSITIONS: int    = 3

# Higher-risk — wider limits, stricter kill-switches
HIGH_RISK_PCT_PER_TRADE: float      = 0.020   # 2.0% of portfolio equity
HIGH_RISK_MAX_DAILY_LOSS_PCT: float = 0.050   # 5.0% of portfolio equity
HIGH_RISK_MAX_DRAWDOWN_PCT: float   = 0.180   # 18 % from equity HWM → pause
HIGH_RISK_MAX_OPEN_POSITIONS: int   = 5

# Volatility threshold that triggers auto-selection of the higher-risk profile.
# When realised ATR% of price exceeds this, the higher-risk profile is chosen.
# Set to a high value to effectively disable auto-escalation.
HIGH_RISK_ATR_THRESHOLD: float = 0.025    # 2.5 % ATR/price

# ---------------------------------------------------------------------------
# Risk management — legacy dollar caps (used as a floor safety net only)
# ---------------------------------------------------------------------------
MAX_TRADES_PER_DAY: int = 5          # hard cap on entries per day
MAX_RISK_PER_TRADE: float = 1.00     # USD minimum risk floor (overridden by pct-based sizing)
MAX_DAILY_LOSS: float = 2.00         # USD minimum floor (overridden by pct-based sizing)

# Minimum 20-bar average volume (in USD notional) for a symbol to be tradable.
# Lowered 10 % from 500 → 450 to allow borderline-liquid alts through.
MIN_LIQUIDITY_VOLUME_USD: float = 450.0

MIN_POSITION_SIZE: float = 10.00     # USD notional minimum
MAX_POSITION_SIZE: float = 100.00    # USD notional maximum

# Percentage of total portfolio value the bot is allowed to deploy.
# 1.0 = use all available buying power (spot only, no leverage).
MAX_ACCOUNT_USAGE_PCT: float = 1.00

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
# Signal: use only closed (completed) candles for entry decisions
# ---------------------------------------------------------------------------
# True  → evaluate signals on the second-to-last bar (candle fully closed)
# False → evaluate on the current forming bar (repaint risk)
USE_CLOSED_CANDLE: bool = True

# ---------------------------------------------------------------------------
# Execution quality filters
# ---------------------------------------------------------------------------
# Maximum estimated entry slippage as fraction of entry price.
MAX_SLIPPAGE_PCT: float = 0.002       # 0.2 %

# ---------------------------------------------------------------------------
# Short-side trading
# ---------------------------------------------------------------------------
# True  → bot can also enter short positions in downtrend / mean-reversion regimes.
# Alpaca crypto supports sell-to-short when the account has no existing position.
ENABLE_SHORT_SELLING: bool = False    # set True when account is margin-enabled for crypto

# ---------------------------------------------------------------------------
# Bar / data settings
# ---------------------------------------------------------------------------
# Supported values: "1Min", "5Min", "15Min", "1Hour", "1Day"
BAR_TIMEFRAME: str = "15Min"
BARS_LOOKBACK: int = 100             # bars fetched per symbol per cycle  (kept for backward compat)

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
