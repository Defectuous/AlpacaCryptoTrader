# Exit-rule backtests — 2026-10-11 (to review)

**Question:** should the bot sell at the profit-alert point instead of using the profit trail?

**Setup:** 180 days of hourly bars, 2026-04-10 → 2026-10-07 (local cache ends Oct 7), $500 start,
live rules (`run_portfolio_backtest`), Pi's `profit_targets.json` as of Oct 11.
BTC trail = arm +2.64%, pullback 1.5%. ETH trail = arm +3.09%, pullback 2.0%.
Scripts + targets snapshot are in [exit-tests-2026-10-11/](exit-tests-2026-10-11/).
Rerun from the repo root: `PYTHONPATH=. python notes/exit-tests-2026-10-11/tp_partial.py notes/exit-tests-2026-10-11/profit_targets.json`

## What prompted it
BAT/USD trade Oct 10–11: profit alert at +5.0%, trail armed at +6.87%, sold on the 3.5% pullback
at **+2.95%** (+$2.79 net). Selling at the alert would have made ~$5.30.

## 1. Sell everything at +5% (the profit-alert point)

| Exit rule | BTC | ETH | BTC + ETH |
|---|---|---|---|
| Current trail | −$0.64 | −$15.61 | **−$19.99** |
| +5% sell, trail still on | −$1.64 | −$24.34 | −$34.16 |
| +5% sell only, no trail | +$4.46 | −$24.34 | −$23.59 |

Worse overall. ETH's few winners ran past 5%, so capping them hurt. All versions lost money:
11 of 21 trades were stop-outs while BTC fell ~$110k → $84k. **Entries are the bigger problem.**

## 2. BTC: tighter trail vs. selling at the arm point (+2.64%)

| BTC exit rule | P&L | Avg win | Max DD |
|---|---|---|---|
| Trail 1.5% pullback (current) | −$0.64 | $3.30 | 4.3% |
| Trail 1.0% | +$1.62 | $3.76 | 3.9% |
| **Trail 0.75%** | **+$4.14** | $4.26 | 3.8% |
| Trail 0.5% | +$6.33 | $4.70 | 3.6% |
| Sell all at +2.64% | +$2.40 | $3.92 | 3.5% |

Same 5 wins / 3 losses in every row; only the size of the wins changes.
0.5% looks best but is about one hourly BTC swing (ATR 0.54%), so live it would trip on noise.
The hourly-bar backtest flatters it.

## 3. BTC: sell 50% at +2.64%, trail the rest

| BTC exit rule | P&L | Avg win | Max DD |
|---|---|---|---|
| 50% at +2.64%, rest trails 1.5% | +$0.89 | $3.61 | 3.8% |
| 50% at +2.64%, rest trails 1.0% | +$2.01 | $3.84 | 3.7% |
| 50% at +2.64%, rest trails 0.75% | +$3.27 | $4.09 | 3.6% |
| (whole position, trail 0.75%) | +$4.14 | $4.26 | 3.8% |

The split is basically the average of "sell all at arm" and "trail all". Moving the stop to
break-even after the first half added nothing. BTC rarely runs far past +2.64%, so the trailing
half had little to catch. The split should do better on coins that run hard after arming (BAT, ETH).

## Caveats
- Small samples: 8 BTC trades, 13 ETH trades.
- BTC's 2.64% arm was calibrated on this same 180 days, so results favour it slightly.
- Only BTC and ETH were tested, out of about 33 coins.

## Open decisions
- [ ] Set BTC to arm 2.64% / trail **0.75%** (`locked: true` in the Pi's `profit_targets.json`; no restart needed)
- [ ] Run sell-50%-at-arm + trail vs. whole-position trail across **all coins** before building it
- [ ] If the split wins, build partial sells in the live bot (currently backtest-only, in the scratch script)
- [ ] Look at entry quality: stop-outs dominate the losses
