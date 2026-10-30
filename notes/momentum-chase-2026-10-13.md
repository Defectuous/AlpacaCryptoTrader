# Momentum-chase backtest — 2026-10-13

**Question:** does this strategy work? "Chase whichever coin is moving hardest, no favorites. Long when
momentum is strongly up, short when strongly down. Pyramid into winners. Bail the instant momentum
stalls or flips, then jump to the next hot mover."

**Setup:** 33 coins, hourly bars 2025-09-02 → 2026-10-13, $500 start, one coin at a time.
Signal on each completed hourly close, filled at the next bar's open. Fee 0.25%/side (Alpaca taker)
+ 0.05% slippage. Liquidity filter = bot's $50/hour minimum. Shorts simulated with no borrow cost
(optimistic; Alpaca can't short crypto). Script: [momentum-chase-2026-10-13/chase.py](momentum-chase-2026-10-13/chase.py),
full results in `grid.csv`. Rerun: `PYTHONPATH=. python notes/momentum-chase-2026-10-13/chase.py`

Grid (96 combos): momentum lookback 3/6/12/24h × "strong" threshold 3/5/8% × exit rule
(`bar` = exit on the first hourly bar closing against us; `stall` = exit when momentum drops below
half the threshold) × pyramid on/off (add 25% of equity each +2%, max 4 units) × shorts on/off.

Benchmarks: BTC buy-and-hold −24.4%, equal-weight basket −22.5%. First half (to Mar 24) −35.5%, second +17.5%.

## Result: it doesn't work at Alpaca's fees

| Costs | Profitable combos | Median return | Best |
|---|---|---|---|
| **0.25% fee + 0.05% slip (real)** | **5 / 96** | **−49%** | +10.6% |
| 0.15% fee + 0.05% slip | 13 / 96 | −40% | +16.9% |
| No costs at all | 76 / 96 | +17% | +90% |

- **The signal has some edge, but fees take all of it.** Costs per round trip are about 0.6%. The
  average trade gains less than that, and the faster versions trade 500–2,900 times a year.
  Fees alone came to $150–$525 on a $500 account.
- **Each part of the strategy makes things worse at real costs:**
  - Shorts: −67% median vs −49% long-only.
  - Pyramiding: −65% vs −52%. Each add costs another fee, and the last add usually comes right before the reversal.
  - Bailing on the first red bar: −69% vs −36% for the slower "stall" exit. Crypto has a lot of 1-bar noise.
- **The only versions that held up were the slowest ones:** 3h lookback, an 8%+ move required,
  about 100 trades/yr, no pyramiding, long-only. The best was +10.6% (max drawdown 9.5%), but it
  made +8.6% in the first half and only +1.8% in the second. Only 1 of 96 combos made money in both
  halves. That looks like luck, not an edge.
- Win rates were 16–46%. The strategy depends on rare big runners, and fees on the many small
  losers outweigh them.

## Verdict
Not worth building. If you want to retry it, it would need maker-only (limit) entries, a much higher
threshold, and fewer trades. At that point it's close to what the bot already does with its 4h trend.
