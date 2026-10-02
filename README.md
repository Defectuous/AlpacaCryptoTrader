# AlpacaCryptoTrader

A beginner-friendly crypto trading bot in Python. It trades on
[Alpaca Markets](https://alpaca.markets) today, with an exchange-neutral broker
layer and a Coinbase adapter in progress for long **and** short trading later.

**Strategy:** 4-hour trend with hourly momentum confirmation  
**Coins:** BTC and ETH by default, or every Alpaca USD coin with `SYMBOLS=all`  
**Exchange:** Alpaca, paper trading by default (long only; see [Brokers](#brokers))  
**Risk per trade:** 1% of equity (2% when volatility is high)  
**Daily limits:** 5 entries; pause at a 3% daily loss (5% in high volatility)  
**R:R:** minimum 1.5 to take a trade  
**Platforms:** Windows, Linux, Raspberry Pi 5 (ARM64)

---

## Project Layout

```
AlpacaCryptoTrader/
├── main.py                 ← run this
├── config.py               ← all tunable settings
├── backtest_runner.py      ← replay the strategy on historical bars
├── requirements.txt
├── .env.example            ← copy to .env and fill in your keys
├── brokers/                ← one adapter per exchange (see "Brokers" below)
│   ├── base.py             ← exchange-neutral Broker interface + data types
│   ├── alpaca.py           ← Alpaca (paper/live, long only, managed stops)
│   ├── alpaca_stream.py    ← Alpaca WebSocket bars/quotes/order updates
│   ├── coinbase.py         ← Coinbase (market data now; trading to finish)
│   └── polling_stream.py   ← REST polling runner for brokers without WebSockets
├── backtest/
│   ├── engine.py           ← bar-by-bar strategy simulator
│   ├── report.py           ← performance summary
│   ├── summarise.py        ← quick summary of the latest results CSV
│   └── results/            ← backtest output CSVs (not committed)
├── data/
│   └── market_data.py      ← bar + quote fetching via the active broker
├── deploy/                 ← systemd service + install/uninstall scripts
├── trader/
│   ├── strategy.py         ← signal detection for every strategy mode
│   ├── indicators.py       ← EMA, RSI, ATR, VWAP, Bollinger, Stochastic RSI, 4h trend
│   ├── risk_manager.py     ← position sizing, R:R validation, daily limits
│   ├── order_manager.py    ← risk-sized entries with protective exits
│   ├── journal.py          ← CSV trade journal in logs/
│   ├── discord_notifier.py ← Discord webhook alerts
│   └── telegram_notifier.py← Telegram bot alerts
└── logs/
    ├── trade_journal.csv   ← auto-created on first trade
    └── trader_YYYY-MM-DD.log
```

---

## Setup

### 1. Get Alpaca API Keys

1. Sign up at <https://alpaca.markets> (free paper-trading account).
2. In the dashboard, switch to your **paper** account and generate API keys.
3. Copy both the **API Key** and the **Secret Key** straight away; the secret
   is only shown once. Regenerating keys invalidates the old pair.

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
BROKER=alpaca              # exchange adapter: alpaca or coinbase
SYMBOLS=BTC/USD,ETH/USD    # or "all" for every tradable USD coin on the broker
ENABLE_SHORT_SELLING=false # shorts need a broker that can short (Alpaca can't)

ALPACA_API_KEY=PKXXXXXXXXXXXXXXXX
ALPACA_SECRET_KEY=XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
ALPACA_PAPER=true          # keep true until you are consistently profitable

COINBASE_MARKET=spot       # only used with BROKER=coinbase

DISCORD_WEBHOOK_URL=       # optional: Discord trade alerts webhook URL
TELEGRAM_BOT_TOKEN=        # optional: Telegram BotFather token
TELEGRAM_CHAT_ID=          # optional: Telegram chat/channel ID
```

The variable names must match exactly (for example `ALPACA_SECRET_KEY`, not
`ALPACA_API_SECRET`), or the bot starts without keys.

If you set `DISCORD_WEBHOOK_URL`, the bot posts alerts to Discord when:

- an entry order is submitted
- any order fills (entries, stop exits and trend-flip closes)

Each alert includes what was bought or sold plus the same `Account: ...`
summary line shown in the logs. Fills that already existed when the bot
started are not re-announced.

If you set both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, the same alerts
are also sent to Telegram.

### 3. Install Dependencies

Use a virtual environment so the bot's packages don't clash with system Python:

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

numpy and pandas ship prebuilt wheels for Windows, Linux x86-64 and Raspberry
Pi 5 (ARM64), so nothing needs compiling. On the Pi, the service installer
below creates the virtual environment for you.

### 4. Run

```bash
python main.py
```

The bot starts in **paper mode** by default. On Alpaca it connects to
WebSockets, streams quotes and minute bars, aggregates them into the configured
timeframe, and evaluates the strategy when each completed bar arrives. Order
updates are streamed through Alpaca's trading WebSocket. Watch the console
output and `logs/trade_journal.csv`.

The bot also reconciles over REST at startup and after every streamed order
update, so the journal stays correct if a WebSocket disconnects or an update is
missed. If a stream dies for good, the bot exits with an error so the service
restarts it.

---

## How the Strategy Works

The default `4h_trend_momentum` mode trades BTC and ETH. It classifies the
completed 4-hour trend from 20/50 EMAs, then requires hourly RSI momentum to
agree before entry. Mixed 4-hour regimes do not open positions. A protective
stop is placed as soon as the entry fills; there is no take-profit order, so
the position stays open until the 4-hour trend changes or the stop is hit.

```
Trend            → 4-hour EMA20 above/below EMA50; insufficient separation is mixed
Momentum         → Hourly RSI >= 55 for longs or <= 45 for shorts
No-trade filters → Mixed trend, insufficient liquidity, abnormal ATR, wide spread, excess slippage
Entry            → Risk-sized limit order in the 4-hour trend direction
Invalidation     → Stop beyond the recent 12-hour swing, buffered by 0.15%
Exit             → Close when completed 4-hour trend changes, or when the protective stop is hit
```

**Risk profiles.** Each signal is sized with one of two profiles, chosen by
volatility (ATR as a share of price):

| Profile | When | Risk per trade | Daily loss pause | Drawdown pause | Max open positions |
|---|---|---|---|---|---|
| standard | ATR < 2.5% | 1% of equity | 3% | 12% from peak | 3 |
| higher-risk | ATR ≥ 2.5% | 2% of equity | 5% | 18% from peak | 5 |

Order size is also clamped to between $12 and $300 of notional; a trade is
skipped if the clamp would push its risk above the profile's cap. (Alpaca
rejects crypto orders under $10; the extra headroom keeps the protective stop,
which sells slightly fewer coins at a lower price, above that minimum.)

**Trading every coin.** `SYMBOLS=all` loads every tradable USD-quoted coin from
the broker at startup (33 on Alpaca in September 2026), skipping the USD
stablecoins listed in `EXCLUDED_SYMBOLS`. The strategy was tuned on BTC and
ETH: a 180-day backtest across all 33 coins was net negative on closed trades
(profit factor 0.85, 10 of 33 coins profitable), so treat `all` as a paper
experiment and backtest before trading it live.

Short entries need `ENABLE_SHORT_SELLING=true` in `.env` **and** a broker that
can short. Alpaca crypto cannot, so on Alpaca the bot runs long-only and simply
stays in cash during downtrends.

**Other strategy modes** can be selected with `STRATEGY_MODE` in `.env`:
`breakout_rotation`, `bb_stoch_volume_scalp`, `htf_vwap_pullback` and
`rsi_vwap_pullback`.

### The trade in one sentence

> "I entered in the direction of the completed 4-hour EMA trend after hourly
> RSI confirmed momentum; I will hold until that trend changes, unless the
> protective swing stop is hit first."

---

## Configuration Reference

Settings marked `.env` are read from your `.env` file; everything else is
edited in `config.py`.

| Setting | Default | Description |
|---|---|---|
| `BROKER` | `alpaca` | Exchange adapter: `alpaca` or `coinbase` (`.env`) |
| `ALPACA_PAPER` | `true` | Paper trading mode (`.env`) |
| `ENABLE_SHORT_SELLING` | `false` | Take short entries when the broker supports them (`.env`) |
| `STRATEGY_MODE` | `4h_trend_momentum` | Active signal mode (`.env`) |
| `COINBASE_MARKET` | `spot` | Coinbase: `spot` (long only) or `futures` (long + short) (`.env`) |
| `SYMBOLS` | `BTC/USD,ETH/USD` | Comma-separated coins, or `all` for every broker coin (`.env`) |
| `EXCLUDED_SYMBOLS` | USD stablecoins | Never traded, even with `SYMBOLS=all` |
| `BAR_TIMEFRAME` | `"1Hour"` | Input timeframe; bars are aggregated into 4-hour trend candles |
| `BARS_LOOKBACK` | `500` | Hourly history loaded to warm up the indicators |
| `TREND_HTF_EMA_FAST` / `TREND_HTF_EMA_SLOW` | `20` / `50` | Four-hour trend EMAs |
| `TREND_MOMENTUM_RSI_LONG` / `TREND_MOMENTUM_RSI_SHORT` | `55` / `45` | Hourly RSI confirmation thresholds |
| `TREND_STOP_LOOKBACK_BARS` | `12` | Hourly bars used for swing-stop placement |
| `TREND_STOP_BUFFER_PCT` | `0.0015` | Stop buffer beyond the swing (0.15%) |
| `STANDARD_RISK_PCT_PER_TRADE` / `HIGH_RISK_PCT_PER_TRADE` | `0.01` / `0.02` | Equity risked per trade per profile |
| `STANDARD_MAX_DAILY_LOSS_PCT` / `HIGH_RISK_MAX_DAILY_LOSS_PCT` | `0.03` / `0.05` | Daily loss that pauses trading |
| `STANDARD_MAX_DRAWDOWN_PCT` / `HIGH_RISK_MAX_DRAWDOWN_PCT` | `0.12` / `0.18` | Drawdown from peak equity that pauses trading |
| `HIGH_RISK_ATR_THRESHOLD` | `0.025` | ATR share of price that selects the higher-risk profile |
| `MAX_TRADES_PER_DAY` | `5` | Hard cap on entries |
| `MIN_POSITION_SIZE` / `MAX_POSITION_SIZE` | `$12` / `$300` | Notional clamp per order |
| `MAX_DAILY_LOSS` | `$2.00` | Fallback dollar limit, only used if account value is unavailable |
| `REWARD_RISK_MIN` | `1.5` | Minimum R:R to take a trade |
| `REWARD_RISK_TARGET` | `2.1` | R:R used for take-profit calculation |
| `USE_LIMIT_ORDERS` | `True` | Limit entry (recommended); `False` → market |
| `ENTRY_FILL_TIMEOUT_SECONDS` | `120` | Cancel the rest of a partly filled entry after this long so the filled part gets its stop (`.env`) |
| `USE_CLOSED_CANDLE` | `True` | Evaluate signals on fully closed candles only |
| `MAX_SPREAD_PCT` | `0.5` | Skip symbol if spread exceeds this % |
| `MAX_SLIPPAGE_PCT` | `0.005` | Max estimated entry slippage; also the stop-limit buffer on Alpaca |
| `MIN_LIQUIDITY_VOLUME_USD` | `50` | Minimum average bar volume in USD |
| `BREAKOUT_*`, `SCALP_*`, `VWAP_*` | — | Parameters for the other strategy modes |

Notification settings are configured from `.env`:

- `DISCORD_WEBHOOK_URL`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `PROFIT_ALERT_PCT` (default `5`): alert once when a position is up this many
  percent at its sellable price (bid for longs, ask for shorts), checked every
  60 seconds. Alert only; the bot's exits are unchanged. `0` disables.
- `PROFIT_TRAIL_ARM_PCT` (default `10`) and `PROFIT_TRAIL_PCT` (default `2`):
  once a position is up `PROFIT_TRAIL_ARM_PCT` percent at its sellable price,
  the bot tracks its best price and sells at market when it falls
  `PROFIT_TRAIL_PCT` percent from that best. Checked every 60 seconds; the
  best price is kept in `logs/profit_trail.json` across restarts. The stop and
  4-hour trend-flip exits still apply. These are the defaults for coins not
  in `PROFIT_TARGETS_FILE`; `PROFIT_TRAIL_ARM_PCT=0` disables the default.
- `PROFIT_TARGETS_FILE` (default `profit_targets.json`): per-coin arm and trail
  percentages. Build it from each coin's price history with
  `python calibrate_profit_targets.py` (`--symbols`, `--days`, `--dry-run`).
  It sets `arm_pct` to the median best gain a coin reached within 7 days of a
  random hour, and `trail_pct` to 3x its median hourly ATR (at most half of
  `arm_pct`). Edit entries by hand and add `"locked": true` to keep them on a
  rerun; `"arm_pct": 0` turns the trail off for that coin. Re-read every
  check, so edits apply without a restart.
  To refresh it nightly, add a crontab line on the bot host (`crontab -e`):
  `0 0 * * * cd ~/AlpacaCryptoTrader && .venv/bin/python calibrate_profit_targets.py >> logs/calibrate.log 2>&1`

---

## Brokers

All exchange access goes through the `Broker` interface in
[brokers/base.py](brokers/base.py). Strategy, risk, journal and notifier code
never touch an exchange SDK, so adding an exchange means adding one adapter.
Pick the adapter with `BROKER` in `.env`.

| Broker | Paper trading | Live | Long | Short | Status |
|---|---|---|---|---|---|
| `alpaca` | ✅ free | ✅ | ✅ | ❌ (Alpaca crypto can't short) | Working |
| `coinbase` | ❌ (no real sandbox) | planned | ✅ | ✅ with `COINBASE_MARKET=futures` | Market data only |

**Alpaca protective stops.** Alpaca rejects bracket/OTO orders for crypto, so
the adapter places the entry as a plain order and then keeps a stop-limit exit
on the filled position (`ensure_protection()`), re-checking it after every
order update, every scan and once a minute. While a limit entry is still
partly filled the stop is deferred (Alpaca would reject it as a potential wash
trade); after `ENTRY_FILL_TIMEOUT_SECONDS`, or as soon as the stop price is
breached, the unfilled remainder is cancelled and the filled part is protected. The stop and target are stored in the entry's
`client_order_id`, so this survives restarts without any local state. Alpaca
takes its crypto fee out of the coins received, so the stop covers the actual
position size rather than the ordered quantity.

**Coinbase.** Candles and quotes come from Coinbase's public API (no account
needed) and are polled at each bar close. With `COINBASE_MARKET=futures`,
orders are routed to the US perpetual-style futures (`BIP-20DEC30-CDE` for BTC,
`ETP-20DEC30-CDE` for ETH), which allow shorts. Trading and account methods
are stubs; the bot refuses to start on Coinbase until they are implemented.
The steps are in the docstring of [brokers/coinbase.py](brokers/coinbase.py).

---

## Backtesting

Replay the strategy over historical bars before trusting it with money:

```bash
python backtest_runner.py                                   # SYMBOLS from .env, 90 days
python backtest_runner.py --symbols all --days 180          # every broker coin
python backtest_runner.py --symbols BTC/USD ETH/USD --days 180
python backtest_runner.py --start 2026-02-04 --end 2026-03-06
python backtest_runner.py --days 365 --equity 500 --shorts  # include short trades
python backtest/summarise.py 180                            # summarise latest multi-symbol 180-day run
```

Results are written to `backtest/results/`: one CSV per symbol, plus a
`combined_*` CSV when more than one symbol is tested (that is the file
`summarise.py` reads). Backtests fetch history through the active broker, so
they use the same data source as live trading. The simulator does not model
exchange fees or slippage, so live results will be somewhat worse.

---

## Trade Journal

Every order is logged to `logs/trade_journal.csv` with:

- Date, time (UTC), symbol, order ID
- Entry, stop, and target prices
- Position size, risk in USD, R:R ratio
- Status (pending → filled / cancelled)
- Exit price and P&L (updated when the position closes)
- The one-sentence reason for the trade

Review this after every session. After 20+ journaled trades you will have
real data to evaluate your strategy.

### MySQL / MariaDB journal

Set `DB_HOST`, `DB_USER`, `DB_PASSWORD` and `DB_NAME` in `.env` to keep the
journal in a `trades` table instead of the CSV. On first start the table is
created and an existing `logs/trade_journal.csv` is imported (then renamed).
Closed trades also record `closed_at`, the UTC time the exit was booked.

Every write is a transaction, so a crash cannot corrupt the journal. If the
database is down, writes queue in `logs/journal_spool.jsonl` and are replayed
in order once it is back. Export to CSV with:

```bash
python -m trader.journal_db export trades.csv
```

---

## No-Trade Rules (automated)

The bot **will not trade** when:

- The spread is too wide (> 0.5%)
- The completed 4-hour trend is mixed, or is a downtrend while shorts are off
- Hourly RSI does not confirm trend direction
- Volatility, liquidity, or estimated slippage is outside configured limits
- The daily trade limit has been reached (5 entries)
- The daily loss pause (3% / 5% of equity) or drawdown pause (12% / 18%) is active
- The symbol already has an open position or an open trade in today's journal
- The profile's maximum number of open positions is reached

---

## Running as a Service (Linux / Raspberry Pi)

The bot ships with a systemd unit in [deploy/](deploy/). Clone the repo on the
Pi, create `.env` from `.env.example`, then run the installer **as the user the
bot should run as** (not root; it asks for sudo when needed):

```bash
cd ~/AlpacaCryptoTrader
cp .env.example .env && nano .env      # add your Alpaca keys
./deploy/install_service.sh
```

The installer creates `.venv`, installs `requirements.txt`, writes
`/etc/systemd/system/alpacacryptotrader.service` with your user and path, and
enables it so it starts at boot. Re-run it after pulling updates to the
service file.

```bash
sudo systemctl status alpacacryptotrader     # is it running?
journalctl -u alpacacryptotrader -f          # live console output
sudo systemctl restart alpacacryptotrader    # after git pull or .env changes
sudo systemctl stop alpacacryptotrader       # stop (cancels open entry orders)
./deploy/uninstall_service.sh                # remove the service
```

How the service behaves:

- **Restarts automatically** after a crash or if a market-data stream dies. It
  never gives up: the delay starts at 30 s and backs off to 10 minutes, so an
  internet or exchange outage (or bad API keys) retries quietly until fixed.
- **Stops cleanly**: `systemctl stop` sends SIGTERM, the bot closes its streams
  and cancels open entry orders, with up to 60 s to finish. Protective stops on
  open positions are left in place.
- **Waits for the network** at boot before starting.
- **Can only write to `logs/`**; the rest of the system is read-only to it.
  Daily log files are still written to `logs/trader_YYYY-MM-DD.log`.

---

## Disclaimer

This software is for educational purposes only. Crypto trading carries
significant risk of loss. Paper trade for at least one month before using real
money. Never risk more than you can afford to lose.
