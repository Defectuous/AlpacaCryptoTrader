# Strategy-list backtests — 2026-10-13

**Question:** do any of the five strategies the user pasted work? (The general rule set plus #1–#4, written for "liquid OKX coins".)

**Setup:** No OKX data, so this uses Alpaca's 33 coins, hourly bars 2025-09-02 → 2026-10-13, $500 start.
"Tick" = one completed hourly bar. Decisions on the close, market fills at the next open + 0.05% slippage.
Take-profit limits fill intrabar at the TP price. Each run at three fee levels: Alpaca 0.25%, 0.10%
(roughly a base-tier OKX spot taker), and zero. "Liquid" = top 15 coins by trailing 7-day dollar volume.
"All allowed" = the bot's $50/hour floor. H1/H2 split at 2026-03-24. Over the same span BTC fell 26%
(H1 −37%, H2 +17.5%).
Script: [strategy-list-2026-10-13/strategies.py](strategy-list-2026-10-13/strategies.py), full table in `results.csv`.
Rerun: `PYTHONPATH=. python notes/strategy-list-2026-10-13/strategies.py`

## How the vague bits were pinned down
- **General rules:** trend = 4h EMA20/50 (as hourly EMA80/200) with price on the right side.
  "Convincing volume" = 24h volume ≥ 1.2× its 7-day average. "Confirmation" = trend held 3 straight
  hours. "Sit out chop" = 72h efficiency ratio ≥ 0.30. Staged TP: ⅓ at +4 ATR, ⅓ at +8 ATR, last ⅓ on
  trend break (EMA cross back). Up to 3 positions at ⅓ equity each, no adds.
- **#1–#3:** "price and volume rising" = 24h return > 0 and 24h volume above its 7-day average.
  "Fades" (as written) = either one turns off. "Buffered" variant = 48h return ≤ 0 or volume < 70% of
  average. Adds: +1 unit each +3% from the last add while still rising, max 3 units (never adds to a
  loser). "Stop trading that coin until signals return" = coin locked after exit until its signal
  switches off and back on.
  - #1 = best *qualifying* coin by 7-day momentum, among the liquid 15.
  - #2 = up to 3 positions, only coins ranked top-3 by 3-day momentum.
  - #3 = only the single #1 coin by 7-day momentum across all allowed coins, and only when it qualifies.
- **#4:** prior range = previous 24/48/72h high–low, at most 8% wide (12% for 72h) to count as
  "clean". Entry after a close above the range high followed by a second close above it. TP1 = +1×
  range height (sell half), TP2 = +2× (sell rest). The user gave no stop, so a failed breakout exits on
  a close back below the range midpoint, and the stop moves to breakeven after TP1.

## Results (full period, return %)

| Strategy | Variant | Alpaca 0.25% | 0.10% fee | No fees | Trades | Max DD (0.25%) |
|---|---|---|---|---|---|---|
| General rules | long+short, as written | **−21.9** | −14.5 | −9.1 | 93 | 41% |
| | long-only | −59.6 | −56.9 | −55.0 | 64 | 66% |
| | long-only, stricter (ER 0.4, vol 1.5×) | **−13.8** | −11.7 | −10.2 | 24 | 29% |
| | long-only, wider TPs | −54.9 | −51.9 | −49.8 | 64 | 64% |
| #1 7d momentum, liquid | as written | −72.7 | −51.7 | −29.5 | 473 | 83% |
| | buffered exit | −76.7 | −62.6 | −48.8 | 391 | 84% |
| #2 3d momentum, top 3 | as written | −53.0 | −35.6 | −20.6 | 775 | 63% |
| | buffered exit | −49.7 | −35.2 | −23.3 | 596 | 64% |
| #3 7d leader only | as written | −48.7 | −21.0 | **+5.4** | 315 | 68% |
| | buffered exit | −69.6 | −57.3 | −46.5 | 250 | 79% |
| #4 breakout | 48h range | −48.5 | −31.3 | −16.7 | 96 | 55% |
| | 24h range | −57.2 | −30.2 | −3.5 | 162 | 68% |
| | 72h range | −52.9 | −42.3 | −33.9 | 67 | 59% |
| | TP 0.5×/1.5× | −73.1 | −56.9 | −41.0 | 156 | 74% |
| | stop at breakout level | −60.3 | −34.8 | −9.2 | 165 | 63% |

"No adds" variants of #1–#3 were all worse (−83% to −97%), because each position starts at full
size instead of ⅓.

## Takeaways
- **None of the 18 variants made money at Alpaca's fees, and none did at 0.10% either.** Only one
  made money even with zero fees (#3 as written, +5.4%), and that came entirely from the second half.
  It lost 29% in the first.
- **These strategies lose before fees, not just after.** This differs from the 2026-10-12 momentum
  chase, which had a pre-fee edge. Here the entry/exit logic itself loses on this data.
- **#1–#3:** "price and volume rising" switches on and off every few hours, so the strategies churn:
  250–775 trades a year with 18–30% win rates. Adds only ever happen on winners, which helps (without
  them, losses are much bigger), but not enough.
- **#4:** half the trades win (TP1 hits), but failed breakouts give back more than TP1 and TP2 earn.
  Fees cost $100–270 on a $500 account.
- **General rules:** the only one that's survivable. Long+short lost 21.9%, which beat BTC's −26%
  thanks to shorting in the crash. The stricter long-only version lost 13.8% on 24 trades, and made
  +10.6% in the up half (H2) against BTC's +17.5%. Its weakness is the exit: waiting for the 4h EMA
  cross means losers give back 10–20% each (median hold ~8 days, median trade −5.5%). That's the same
  flaw the 2026-10-11 exit tests found in the live bot's entries.

## Verdict
Don't build any of these as written. The general rule set is the only one worth iterating on, with a
faster exit than an EMA cross and long-only for Alpaca.
