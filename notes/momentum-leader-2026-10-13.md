# Momentum-leader chase, top 5 by volume — 2026-10-13

**Question:** does "long the strongest 7-day (or 3-day) momentum coin, add while it leads, switch when
overtaken, exit when it loses momentum" work if it only picks from the 5 highest-volume coins?

**Setup:** Alpaca hourly bars 2025-09-02 → 2026-10-14, $500 start, engine from
[strategy-list-2026-10-13](strategy-list-2026-10-13.md) (decide on the close, fill at the next open +
0.05% slippage). Top 5 = trailing 7-day dollar volume, re-ranked every hour. In practice that's
BTC, ETH, SOL and XRP almost always, plus DOGE/UNI/LINK/AVAX rotating through the 5th slot.
"Loses momentum" = the held coin's N-day return ≤ 0. "Adding" = +⅓ equity per tick while it still
leads, max 3 units. BTC over the span: −26% (H1 −39%, H2 +21%).
Script: [momentum-leader-2026-10-13/leader.py](momentum-leader-2026-10-13/leader.py), table in `results.csv`.

## Results (full period, return %)

| Variant | 7d @ 0.25% | 7d no fees | 3d @ 0.25% | 3d no fees | Trades (7d) |
|---|---|---|---|---|---|
| As written (add each tick) | −98.9 | −71.7 | −99.4 | −87.8 | 949 |
| Add only after +3% | −92.0 | −55.9 | −96.2 | −67.3 | 949 |
| No adds (full size at entry) | −99.8 | −80.9 | −99.8 | −92.7 | 915 |
| Switch only if beaten by 2pp | −95.7 | −55.4 | −98.8 | −85.0 | 644 |
| Switch only if beaten by 5pp | −93.0 | −46.7 | −97.2 | −76.1 | 558 |
| Decide every 4h | −89.5 | −63.7 | −95.9 | −80.8 | 373 |
| 4h + 5pp margin | **−81.2** | −55.5 | −91.4 | −77.5 | 235 |

## Takeaways
- **Every variant loses even with zero fees.** The leader among 5 big coins changes constantly (two
  coins sit within a fraction of a percent of each other), so it switches 600–950 times a year and
  buys each new leader after its move, just before it mean-reverts. Win rate 17–40%.
- **3-day is worse than 7-day** on every variant: a shorter lookback means more leader flips.
- Slowing it down (4h decisions, 5pp switch margin) cuts losses but never gets close to break-even.
- Restricting to top-5 volume made it worse than the earlier #3 test across all coins (−48.7% at fees,
  +5.4% no fees), because the big 4 move together, so the "leader" is mostly noise.

## Verdict
Don't build it. Same conclusion as [momentum-chase](momentum-chase-2026-10-13.md) and
[strategy-list](strategy-list-2026-10-13.md): leader-rotation churns, and this version has no pre-fee edge.

## 1-hour live run (2026-10-14 03:50–04:46 UTC)
`live_sim.py`, 5-minute ticks, live Alpaca quotes, no orders placed, $500 per book.
- **7d:** stayed in cash. All five top coins had a negative 7-day return the whole hour
  (BTC about −0.5%, the least negative).
- **3d:** bought BTC on the first tick and added on the next two, reaching 3 units within 10 minutes.
  The leader never changed. It ended at $498.49 (−0.30%), $1.25 of it buy fees, with no round trips.

## 2-hour live run (2026-10-23 05:21–07:18 UTC)
Same script and settings, `--minutes 120 --tick 5`.
- **7d:** stayed in cash again. All five top coins had a negative 7-day return the whole run
  (BTC −0.3% to −0.7%, the least negative).
- **3d:** bought XRP (3d +1.7%) on the first tick and added on the next two, reaching 3 units within
  10 minutes. XRP stayed the leader and climbed to about +2.4% over 3 days. It ended at $499.17
  (−0.17%), with $1.24 of buy fees and no round trips. Selling at the end would cost another ~$1.25,
  so it would close at about −0.4%.
- Same pattern as the 1-hour run: no switches, so the churn that sinks the backtest never showed up.
  Two hours is too short to test this strategy. The backtest verdict stands.
