# AlpacaCryptoTrader

A beginner-friendly crypto trading bot built with the [Alpaca Markets](https://alpaca.markets) API and Python.

**Strategy:** 4-hour trend with hourly momentum confirmation
**Coins:** BTC, ETH
**Account size:** Designed for a $100 starting balance  
**Risk per trade:** $1 max | Daily loss limit: $2  
**R:R target:** 1.5 – 2.1  
**Platforms:** Windows, Linux, Raspberry Pi 5 (ARM64)

---

## Project Layout

```
AlpacaCryptoTrader/
├── main.py                 ← run this
├── config.py               ← all tunable settings
├── requirements.txt
├── .env.example            ← copy to .env and fill in your keys
├── data/
│   └── market_data.py      ← Alpaca bar + quote fetching
├── trader/
│   ├── alpaca_client.py    ← API client singleton
│   ├── discord_notifier.py ← Discord webhook alerts
│   ├── telegram_notifier.py← Telegram bot alerts
│   ├── indicators.py       ← VWAP, EMA9/20, ATR, volume
│   ├── strategy.py         ← signal detection (VWAP pullback)
│   ├── risk_manager.py     ← position sizing, R:R validation, daily limits
│   ├── order_manager.py    ← bracket order placement (limit + TP + SL)
│   └── journal.py          ← CSV trade journal in logs/
└── logs/
    ├── trade_journal.csv   ← auto-created on first trade
    └── trader_YYYY-MM-DD.log
```

---

## Setup

### 1. Get Alpaca API Keys

1. Sign up at <https://alpaca.markets> (free paper-trading account).
2. In the dashboard, go to **Paper Trading → API Keys → Generate New Key**.
3. Copy both the **API Key ID** and **Secret Key**.

### 2. Configure Environment

```bash
# Copy the template
cp .env.example .env
```

Windows PowerShell alternative:

```powershell
Copy-Item .env.example .env
```

Edit `.env`:

```
ALPACA_API_KEY=PKXXXXXXXXXXXXXXXX
ALPACA_SECRET_KEY=XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
ALPACA_PAPER=true       # keep true until you are consistently profitable
DISCORD_WEBHOOK_URL=    # optional: Discord trade alerts webhook URL
TELEGRAM_BOT_TOKEN=     # optional: Telegram BotFather token
TELEGRAM_CHAT_ID=       # optional: Telegram chat/channel ID
```

If you set `DISCORD_WEBHOOK_URL`, the bot posts trade alerts to Discord for:

- BUY order submitted
- BUY filled
- SELL filled

Each alert includes an explanation of what was bought/sold plus the same
`Account: ...` summary line shown in logs.

If you set both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, the same alerts
are also sent to Telegram.

### 3. Install Dependencies

#### Windows / Linux x86-64

```bash
pip install -r requirements.txt
```

#### Raspberry Pi 5 (ARM64)

```bash
# numpy and pandas compile from source on ARM — install system dependencies first
sudo apt update && sudo apt install -y python3-dev libatlas-base-dev gfortran

pip install -r requirements.txt
```

> **Tip (Raspberry Pi):** Use a virtual environment to avoid conflicts with
> system Python packages.
>
> ```bash
> python3 -m venv .venv
> source .venv/bin/activate
> pip install -r requirements.txt
> ```

### 4. Run

```bash
python main.py
```

The bot starts in **paper mode** by default. It connects to Alpaca WebSockets,
streams quotes and minute bars, aggregates them into the configured timeframe,
and evaluates the strategy when each completed bar arrives. Order updates are
also streamed through Alpaca's trading WebSocket. Watch the console output and
the `logs/trade_journal.csv` file.

The bot still performs REST reconciliation at startup and after streamed trade
updates. This protects the journal if a WebSocket disconnects or an update is
missed.

---

## How the Strategy Works

The default `4h_trend_momentum` mode trades BTC and ETH. It classifies the
completed 4-hour trend from 20/50 EMAs, then requires hourly RSI momentum to
agree before entry. Mixed 4-hour regimes do not open positions. A protective
stop is attached at entry; there is no take-profit order, so the position stays
open until the 4-hour trend changes or the stop is hit.

```
Trend            → 4-hour EMA20 above/below EMA50; insufficient separation is mixed
Momentum         → Hourly RSI >= 55 for longs or <= 45 for shorts
No-trade filters → Mixed trend, insufficient liquidity, abnormal ATR, wide spread, excess slippage
Entry            → Risk-sized limit order in the 4-hour trend direction
Invalidation     → Stop beyond the recent 12-hour swing, buffered by 0.15%
Exit             → Close when completed 4-hour trend changes, or when the protective stop is hit
```

Short entries require short-selling permission and support from the connected
Alpaca account. Verify both in paper trading before enabling live trading. The
VWAP pullback, breakout, and Bollinger/Stochastic scalp modes remain available
by setting `STRATEGY_MODE` in `.env`.

### The trade in one sentence

> "I entered in the direction of the completed 4-hour EMA trend after hourly
> RSI confirmed momentum; I will hold until that trend changes, unless the
> protective swing stop is hit first."

---

## Configuration Reference (`config.py`)

| Setting | Default | Description |
|---|---|---|
| `SYMBOLS` | `["BTC/USD","ETH/USD"]` | Coins to watch |
| `STRATEGY_MODE` | `4h_trend_momentum` | Active signal mode |
| `TREND_HTF_EMA_FAST` / `TREND_HTF_EMA_SLOW` | `20` / `50` | Four-hour trend EMAs |
| `TREND_MOMENTUM_RSI_LONG` / `TREND_MOMENTUM_RSI_SHORT` | `55` / `45` | Hourly RSI confirmation thresholds |
| `TREND_STOP_LOOKBACK_BARS` | `12` | Hourly bars used for swing-stop placement |
| `BREAKOUT_RANGE_BARS` | `20` | Completed bars in the prior range |
| `BREAKOUT_VOLUME_MULTIPLIER` | `1.2` | Minimum breakout volume vs average |
| `BREAKOUT_STOP_ATR_BUFFER` | `0.5` | Stop buffer below the prior range high, in ATRs |
| `BREAKOUT_TP1_R` / `BREAKOUT_TP2_R` | `1.0` / `2.0` | Reward-to-risk levels for the two exit halves |
| `BREAKOUT_MAX_ATR_PCT` | `0.05` | Maximum ATR as a fraction of price |
| `BREAKOUT_MAX_EXTENSION_ATR` | `1.5` | Maximum entry extension above range, in ATRs |
| `MAX_TRADES_PER_DAY` | `2` | Hard cap on entries |
| `MAX_RISK_PER_TRADE` | `$1.00` | USD risked per trade |
| `MAX_DAILY_LOSS` | `$2.00` | Trading halts after this loss |
| `MIN_POSITION_SIZE` | `$25` | Minimum notional per order |
| `MAX_POSITION_SIZE` | `$50` | Maximum notional per order |
| `MAX_ACCOUNT_USAGE_PCT` | `0.75` | Max share of portfolio the bot can deploy (keeps reserve cash) |
| `REWARD_RISK_MIN` | `1.5` | Minimum R:R to take a trade |
| `REWARD_RISK_TARGET` | `2.1` | R:R used for take-profit calculation |
| `BAR_TIMEFRAME` | `"1Hour"` | Input timeframe; bars are aggregated into 4-hour trend candles |
| `POLL_INTERVAL_SECONDS` | `60` | Legacy polling setting; streaming runtime scans on completed bars |
| `USE_LIMIT_ORDERS` | `True` | Limit entry (recommended); `False` → market |
| `VWAP_PULLBACK_THRESHOLD` | `0.005` | Max distance from VWAP to qualify (0.5%) |
| `VOLUME_MULTIPLIER` | `1.2` | Bounce bar volume vs average volume |
| `MAX_SPREAD_PCT` | `0.5` | Skip symbol if spread exceeds this % |
| `ALPACA_PAPER` | `true` | Paper trading mode |

Notification settings are configured from `.env`:

- `DISCORD_WEBHOOK_URL`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

---

## Trade Journal

Every order is logged to `logs/trade_journal.csv` with:

- Date, time (UTC), symbol, order ID
- Entry, stop, and target prices
- Position size, risk in USD, R:R ratio
- Status (pending → filled / cancelled)
- Exit price and P&L (updated when order closes)
- The one-sentence reason for the trade

Review this after every session. After 20+ journaled trades you will have
real data to evaluate your strategy.

---

## No-Trade Rules (automated)

The bot **will not trade** when:

- The spread is too wide (> 0.5%)
- The completed 4-hour trend is mixed
- Hourly RSI does not confirm trend direction
- Volatility, liquidity, spread, or estimated slippage is outside configured limits
- The daily trade limit has been reached (2 trades)
- The daily loss limit has been hit (-$2)
- The symbol already has an open position today

---

## Running as a Service on Raspberry Pi

```bash
# Create a systemd service
sudo nano /etc/systemd/system/alpacacryptotrader.service
```

```ini
[Unit]
Description=AlpacaCryptoTrader
After=network-online.target
Wants=network-online.target

[Service]
User=pi
WorkingDirectory=/home/pi/AlpacaCryptoTrader
ExecStart=/home/pi/AlpacaCryptoTrader/.venv/bin/python main.py
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable alpacacryptotrader
sudo systemctl start alpacacryptotrader
sudo journalctl -u alpacacryptotrader -f
```

---

## Disclaimer

This software is for educational purposes only. Crypto trading carries
significant risk of loss. Paper trade for at least one month before using real
money. Never risk more than you can afford to lose.
