# beebots.tech Hive rules, last 7 days — 2026-10-13

**Question:** do the 100 "bees" on beebots.tech's Hive board make money on the past week if their
rules run on Alpaca?

**Source:** `beebots.tech/hive/board` JSON, snapshot 2026-10-14 in
[hive_board.json](beebots-hive-2026-10-13/hive_board.json). The 100 bees have 88 distinct rule texts.
Two have nothing testable: 5 bees didn't share rules, and 3 just say "use the built-in strategy". That
leaves **86 tested rule texts**.

**Setup:** 15-minute bars 2026-10-07 03:30 → 2026-10-14 03:30 UTC (673 ticks), all 33 Alpaca coins.
Decide on the bar close, fill at the next open + 0.05% slippage, with stops and take-profits filled
intrabar. Everything runs at 1x leverage, with shorts allowed where the rule says so. Shorts can't be
done on Alpaca; see [[exchange-strategy]] in memory. Each rule runs at three fee levels: Alpaca 0.25%,
OKX perp ~0.05%, and zero. Over the week, BTC was −0.3%, ETH −3.5% and SOL −8.7%.
Script: [hive.py](beebots-hive-2026-10-13/hive.py). Every rule with its result is in
[results.csv](beebots-hive-2026-10-13/results.csv).
Rerun: `PYTHONPATH=. python notes/beebots-hive-2026-10-13/hive.py`

**Limits (read these before trusting any single number):**
- The bees trade OKX perps. Alpaca has no open interest, funding, `attn_z`, or the site's "score".
  Open interest is replaced by "last hour's volume above its 24h average", and score by "how many of
  the 1h/4h/24h returns agree". About 15 rules use those inputs.
- BNB, SUI, ENA, TAO, ZEC, NEAR, MOODENG, PENGU and PUMP aren't on Alpaca, so they were dropped from
  coin lists.
- The rules are vague English. Each one was pinned to concrete thresholds; the mapping is in `cfg()`.
  Similar rules share one implementation with different parameters, in about 18 families.
- **7 days is a tiny sample.** One coin's pump can decide a rule's result.

## Results

| | Alpaca 0.25% | OKX-like 0.05% | No fees |
|---|---|---|---|
| Rules that traded | 77 | 77 | 77 |
| Made money | **16** | 24 | 27 |
| Median return | −2.1% | −1.2% | −0.9% |
| Mean return | −5.1% | −1.7% | −0.8% |

9 rules never triggered all week; strict trend and volume filters kept them out of a sideways market.

**Best at Alpaca fees** (each trade checked by hand):

| Rule (bee) | 7d Alpaca | Why |
|---|---|---|
| R47 Ticktide (4h + 30m trend, strongest 7d coin) | +15.9% | one BAT trade +23.7%, the other 3 net about −4% |
| R13 Momentum Honey (7d leader, hold ≥1 day) | +9.2% | BAT +17.9% covered 2 losers of about −5% each |
| R5 / R18 Jessi / Hive Momentum (7d leader, price+volume rising) | +4.7% | all 7 trades in BAT |
| R28 Jabu (7d leader + OI-proxy filter) | +3.5% | |
| R15 / R33 Knight Gambit / BidenBee (BTC/ETH multi-day trend) | +3.3% | 1 trade |

**Worst at Alpaca fees:** R63 Zenith scalper −50% (96 trades), R78 Arby long/short pair −34% (112),
R1 / R22 Mayhem "chase the hottest mover" −30% (144), R44 Frenzy −17%, R36 Rumble Rover −16% (165).
The high-turnover rules lose most of their money to fees. Rumble Rover makes +22% with no fees and
+13% at OKX fees, but −16% at Alpaca's.

**Against the board:** Mayhem shows +119.5% lifetime on the board. Its rule loses 30% here at Alpaca
fees and 7.9% at OKX fees, with 144 round trips in a week. The board's own week column is zero or
negative for 68 of the 73 bees that have one (median −3.3%). Lifetime P&L is positive for only 24 of 88 rows
(median −2.8%). The "dramatic P&L" is the top few bees out of 100, which is what luck alone produces
across a hundred random strategies.

## Takeaways
- **The board's leaders don't hold up.** Every rule in the top five by board P&L loses here at Alpaca
  fees except R5 (+4.7%, all BAT). That includes Mayhem, Windward (no trades), Orakelia (−5.7%) and
  Dazzibee (−3.5%).
- **The winners this week are BAT's pump.** BAT went from about $0.10 to $0.19. Any rule that picks
  "the strongest 7-day mover" from all coins ended up in it. Take BAT out and the momentum-leader
  family loses money, consistent with the 13-month tests ([strategy-list](strategy-list-2026-10-13.md),
  [momentum-leader](momentum-leader-2026-10-13.md)).
- **Fees decide the fast rules.** Rules that trade more than about 20 times a week lose at Alpaca's
  0.25% no matter how they do before fees.
- **Nothing here is worth building.** Low-turnover BTC/ETH trend rules (R15, R19, R34) and the RSI(2)
  pullback (R42/R43) were roughly break-even. That matches the earlier finding that slow trend rules
  are survivable, but there's no edge to show from one week.
