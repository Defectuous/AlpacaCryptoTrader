# TODO

## Strategy ideas to test

- [ ] **Momentum-leader range swing, 7-day hold** (noted 2026-10-16)
  - Idea: among the strongest coins, buy when price sits in the low part of its
    recent range and hold up to 7 days for a limit sell near the top of it.
    Targets are ~5-15% away, so Alpaca fees are a small slice (unlike the 1%
    scalps and momentum chases rejected on 2026-10-13).
  - Starting rules (all tunable):
    - Rank all coins by 30-day return; keep the top 5 (a 21-day window would
      put most leaders near their range top, so momentum and "low region"
      would rarely agree).
    - Buy when price is in the bottom 25% of its 21-day high-low range.
    - Limit sell at the 75% level of that range.
    - Time exit: market sell at day 7 if the target wasn't hit.
    - Optional hard stop just below the 21-day low.
    - Maker fees, $300 per trade, max 3 open at once.
  - Main risk: a "low region" buy that is really the start of a breakdown
    (FIL's 30-day low was 0.789 while trading ~1.14).
  - Step 1: backtest 6-12 months vs BTC buy-and-hold. Judge on whether the time
    exits and stop-outs eat the wins.
  - Step 2 (only if step 1 is profitable): **news veto**. Skip a buy when the
    coin had serious negative news in the last 48h (hack, exploit, delisting,
    SEC/regulatory action). Alpaca's news API covers crypto and has history,
    so this can be backtested too. News as a *buy* signal is out: prices react
    within minutes and the LLM-gated chase already lost.
  - Step 3 (optional): skip coins with a scheduled token unlock inside the
    7-day hold window. Needs an unlock-calendar data source.

## Low priority (not breaking anything)

- [ ] **Account line logged / fetched 33× per hour** (noted 2026-10-14)
  - In `4h_trend_momentum` mode, `_handle_stream_bar` (main.py) calls
    `run_scan_cycle` once per symbol as each 1h bar closes. Every call fetches
    the account, today's stats, open positions and journal symbols, and logs the
    `Account: cash=… | portfolio=…` line, so that happens 33 times an hour.
  - Result: noisy journalctl and ~33 redundant account/positions API calls per hour.
  - Fix idea: log the account line (and refresh account/positions) once per
    hour, e.g. cache it per bar timestamp, or batch the hourly bar closes into a
    single scan cycle like `breakout_rotation` does with `snapshot`.

- [ ] **4h trend "sideways" cutoff has no buffer** (noted 2026-10-16)
  - FIL/USD was bought and trend-flip sold twice overnight on 2026-10-16. The
    main cause (500-bar lookback leaving the 4h EMA50 unsettled) is fixed in
    9a29135 by raising `BARS_LOOKBACK` to 2000.
  - If a coin still flips in and out within one 4h candle, add a buffer: exit
    only on an actual EMA cross, or when separation falls well below
    `TREND_MIN_EMA_SEPARATION_PCT`, not the moment it touches it.
