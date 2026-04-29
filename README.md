# AlpacaCryptoTrader

A beginner-friendly crypto trading bot built with the [Alpaca Markets](https://alpaca.markets) API and Python.

**Strategy:** VWAP Pullback in the direction of trend (EMA9/EMA20 alignment)  
**Coins:** BTC, ETH, SOL  
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

The bot starts in **paper mode** by default. Watch the console output and the
`logs/trade_journal.csv` file.

---

## How the Strategy Works

```
Uptrend filter   → EMA9 > EMA20 (by at least 0.1% of price)
Not sideways     → ATR > 0.5% of price AND EMA separation > 0.1%
Volume filter    → Last completed bar volume ≥ 1.2× 20-bar average
Spread filter    → Bid/ask spread < 0.5%

Pullback trigger → Previous bar low touched or crossed below VWAP
Bounce confirm   → Current close is back above VWAP (within 0.5%)

Entry            → Limit order at current ask price
Stop loss        → 0.1% below the 3-bar swing low before VWAP touch
Take profit      → Entry + (Risk × 2.1)  → gives ~2.1:1 R:R

Daily limits     → Max 2 trades, max $2 loss — bot halts for the day
```

### The trade in one sentence

> "I bought ETH because price was above VWAP, EMA9 was above EMA20, ETH
> pulled back to VWAP, volume increased on the bounce, my stop was below the
> pullback low, and my target gave me at least 1.5:1 reward-to-risk."

---

## Configuration Reference (`config.py`)

| Setting | Default | Description |
|---|---|---|
| `SYMBOLS` | `["BTC/USD","ETH/USD","SOL/USD"]` | Coins to watch |
| `MAX_TRADES_PER_DAY` | `2` | Hard cap on entries |
| `MAX_RISK_PER_TRADE` | `$1.00` | USD risked per trade |
| `MAX_DAILY_LOSS` | `$2.00` | Trading halts after this loss |
| `MIN_POSITION_SIZE` | `$25` | Minimum notional per order |
| `MAX_POSITION_SIZE` | `$50` | Maximum notional per order |
| `MAX_ACCOUNT_USAGE_PCT` | `0.75` | Max share of portfolio the bot can deploy (keeps reserve cash) |
| `REWARD_RISK_MIN` | `1.5` | Minimum R:R to take a trade |
| `REWARD_RISK_TARGET` | `2.1` | R:R used for take-profit calculation |
| `BAR_TIMEFRAME` | `"15Min"` | Chart timeframe for signal detection |
| `POLL_INTERVAL_SECONDS` | `60` | How often the bot scans (seconds) |
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
- The market is chopping sideways (low ATR or EMA separation)
- The trend is not clearly up
- Volume is too low on the bounce bar
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
