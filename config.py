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
# LTC/USD removed — zero signals over 90 days (liquidity filter blocks it).
# AVAX/USD removed — poor performance (PF 0.25, negative PnL).
SYMBOLS: list[str] = [
	"BTC/USD",
	"ETH/USD",
]

# ---------------------------------------------------------------------------
# Risk profiles  (auto-selected based on realised volatility regime)
# ---------------------------------------------------------------------------
# Standard — conservative defaults
STANDARD_RISK_PCT_PER_TRADE: float = 0.01
STANDARD_MAX_DAILY_LOSS_PCT: float  = 0.03
STANDARD_MAX_DRAWDOWN_PCT: float    = 0.12
STANDARD_MAX_OPEN_POSITIONS: int    = 3

# Higher-risk — wider limits, stricter kill-switches
HIGH_RISK_PCT_PER_TRADE: float      = 0.02
HIGH_RISK_MAX_DAILY_LOSS_PCT: float = 0.05
HIGH_RISK_MAX_DRAWDOWN_PCT: float   = 0.18
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
MIN_LIQUIDITY_VOLUME_USD: float = 50.0

MIN_POSITION_SIZE: float = 10.00     # USD notional minimum
MAX_POSITION_SIZE: float = 300.00    # USD notional maximum

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
STRATEGY_MODE: str = os.getenv("STRATEGY_MODE", "4h_trend_momentum")

# Four-hour trend-following strategy
TREND_TIMEFRAME: str = "4h"
TREND_HTF_EMA_FAST: int = 20
TREND_HTF_EMA_SLOW: int = 50
TREND_MIN_EMA_SEPARATION_PCT: float = 0.001
TREND_MOMENTUM_RSI_LONG: float = 55.0
TREND_MOMENTUM_RSI_SHORT: float = 45.0
TREND_STOP_LOOKBACK_BARS: int = 12
TREND_STOP_BUFFER_PCT: float = 0.0015
TREND_MAX_ATR_PCT: float = 0.05

# Confirmed range-breakout rotation (long-only).
BREAKOUT_RANGE_BARS: int = 20
BREAKOUT_VOLUME_MULTIPLIER: float = 1.2
BREAKOUT_STOP_ATR_BUFFER: float = 0.5
BREAKOUT_TP1_R: float = 1.0
BREAKOUT_TP2_R: float = 2.0
BREAKOUT_MAX_ATR_PCT: float = 0.05
BREAKOUT_MAX_EXTENSION_ATR: float = 1.5

# ---------------------------------------------------------------------------
# Mean-reversion scalp parameters (Bollinger + Stochastic RSI + volume)
# ---------------------------------------------------------------------------
SCALP_BB_LENGTH: int = 20
SCALP_BB_STD: float = 2.0
SCALP_STOCH_K: int = 3
SCALP_STOCH_D: int = 3
SCALP_STOCH_RSI: int = 14
SCALP_STOCH_LENGTH: int = 14
SCALP_VOLUME_MA: int = 20
SCALP_VOLUME_MIN_RATIO: float = 0.70
SCALP_OVERSOLD_LEVEL: float = 40.0
SCALP_OVERBOUGHT_LEVEL: float = 60.0
SCALP_STOP_BUFFER: float = 0.0035         # 0.35% below/above band for stop placement
SCALP_RR_RATIO: float = 1.5

EMA_SHORT: int = 9                   # fast EMA period
EMA_LONG: int = 20                   # slow EMA period
HTF_TIMEFRAME: str = "1h"
HTF_EMA_FAST: int = 20
HTF_EMA_SLOW: int = 50
RSI_PERIOD: int = 14
RSI_LONG_RESET_MAX: float = 55.0      # pullback must reset RSI to/below this
RSI_LONG_CONFIRM_MIN: float = 50.0    # confirmation must reclaim this level

# Price must be within this fraction of VWAP to qualify as a pullback
# Reverted to 0.5% (looser) for better signal capture
VWAP_PULLBACK_THRESHOLD: float = 0.005   # 0.5 %

# Volume on the bounce bar must exceed average volume by this multiplier
VOLUME_MULTIPLIER: float = 1.2

# EMA9/EMA20 must differ by at least this fraction of price to avoid sideways
# Reverted to 0.1% (looser) for better signal capture
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
MAX_SLIPPAGE_PCT: float = 0.005       # 0.5 %

# ---------------------------------------------------------------------------
# Short-side trading
# ---------------------------------------------------------------------------
# True  → enable short entries; the account and Alpaca API must permit crypto shorts.
ENABLE_SHORT_SELLING: bool = True     # required for the downtrend leg of this strategy

# ---------------------------------------------------------------------------
# Bar / data settings
# ---------------------------------------------------------------------------
# Supported values: "1Min", "5Min", "15Min", "1Hour", "1Day"
BAR_TIMEFRAME: str = "1Hour"
BARS_LOOKBACK: int = 500             # enough hourly history to warm up the 4h slow EMA

# ---------------------------------------------------------------------------
# Order type preference
# ---------------------------------------------------------------------------
# True  → limit order for entry (preferred, less slippage)
# False → market order for entry (fills immediately, more slippage)
USE_LIMIT_ORDERS: bool = True

# ---------------------------------------------------------------------------
# Legacy loop setting retained for configuration compatibility. The live bot
# now scans on completed streamed bars instead of sleeping between polls.
# ---------------------------------------------------------------------------
POLL_INTERVAL_SECONDS: int = 60      # unused by the streaming runtime
