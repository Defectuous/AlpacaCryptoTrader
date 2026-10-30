"""
"Chase the momentum leader" among the top 5 coins by volume (2026-10-13).
See ../momentum-leader-2026-10-13.md.

Rule as given: each tick, go long the coin with the strongest N-day momentum. Keep adding while it
stays the leader. Switch when another coin overtakes it. Exit if it loses momentum and no longer leads.

Reuses the engine from ../strategy-list-2026-10-13/strategies.py (hourly ticks, decisions on the close,
market fills at the next open + 0.05% slippage).

Run from the repo root:  PYTHONPATH=. python notes/momentum-leader-2026-10-13/leader.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "strategy-list-2026-10-13"))
import strategies as S  # noqa: E402

S.LIQUID_TOP_N = 5          # "top 5 volume coins" = top 5 by trailing 7-day dollar volume


class Leader(S.Strategy):
    max_positions = 1

    def __init__(self, lookback=168, max_units=3, add_step=None, margin=0.0, every=1):
        self.mom = {168: "ret168", 72: "ret72"}[lookback]
        self.max_units, self.unit_frac = max_units, 1 / max_units
        self.add_step = add_step    # None = add one unit every tick it still leads
        self.margin = margin        # challenger must beat the held coin by this much to switch
        self.every = every          # act only every N hours (1 = every hourly tick)

    def _ranked(self, p, t):
        m = p[self.mom][t]
        return sorted(((m[j], j) for j in range(len(p["cols"]))
                       if S._ok(p, t, j, "liquid") and not np.isnan(m[j])), reverse=True)

    def entries(self, p, t, held, locked):
        if t % self.every:
            return []
        r = self._ranked(p, t)
        if r and r[0][0] > 0 and r[0][1] not in held:
            return [(r[0][1], 1)]
        return []

    def manage(self, p, t, j, ps):
        if t % self.every:
            return None
        r = self._ranked(p, t)
        mine = p[self.mom][t, j]
        lost_momentum = not mine > 0
        best = r[0] if r else (mine, j)
        overtaken = best[1] != j and best[0] > mine + self.margin
        if lost_momentum or overtaken or not S._ok(p, t, j, "liquid"):
            return ("exit",)
        if ps.units < self.max_units and (
                self.add_step is None or p["C"][t, j] / ps.last_add - 1 >= self.add_step):
            return ("add",)


def variants():
    out = []
    for lb in (168, 72):
        tag = "7d" if lb == 168 else "3d"
        out += [
            (f"{tag} as written (add each tick, max 3)", Leader(lb)),
            (f"{tag} add only after +3%", Leader(lb, add_step=0.03)),
            (f"{tag} no adds (full size)", Leader(lb, max_units=1)),
            (f"{tag} switch only if beaten by 2pp", Leader(lb, margin=0.02)),
            (f"{tag} switch only if beaten by 5pp", Leader(lb, margin=0.05)),
            (f"{tag} decide every 4h", Leader(lb, every=4)),
            (f"{tag} 4h + 5pp margin", Leader(lb, margin=0.05, every=4)),
        ]
    return out


def main():
    p = S.load_panel()
    idx = p["index"]
    mid = idx[len(idx) // 2]
    btc = p["C"][:, p["cols"].index("BTC/USD")]
    print(f"Data {idx[0]:%Y-%m-%d} -> {idx[-1]:%Y-%m-%d %H:%M} UTC, top-5 by 7d $volume, split {mid:%Y-%m-%d}")
    print(f"BTC hold: full {(btc[-1] / btc[S.H168 + 1] - 1) * 100:+.1f}%  "
          f"H1 {(btc[len(idx)//2] / btc[S.H168 + 1] - 1) * 100:+.1f}%  H2 {(btc[-1] / btc[len(idx)//2] - 1) * 100:+.1f}%")
    # which coins make the top 5, and how often
    share = pd.Series(p["liquid"].mean(axis=0), index=p["cols"]).sort_values(ascending=False)
    print("Top-5 membership (% of hours):", ", ".join(f"{s.split('/')[0]} {v*100:.0f}" for s, v in share.head(8).items()))
    rows = []
    for name, s in variants():
        for fee_name, fee in (("Alpaca 0.25%", 0.0025), ("0.10%", 0.0010), ("no fees", 0.0)):
            full, h1, h2 = S.run(p, s, fee), S.run(p, s, fee, end=mid), S.run(p, s, fee, start=mid)
            rows.append({"variant": name, "fees": fee_name, **full, "H1%": h1["ret%"], "H2%": h2["ret%"]})
            print(f"{name:40} {fee_name:13} ret {full['ret%']:+7.1f}%  trades {full['trades']:4}  "
                  f"win {full['win%']:3.0f}%  DD {full['maxDD%']:5.1f}%  fees ${full['fees$']:4.0f}  "
                  f"H1 {h1['ret%']:+6.1f}%  H2 {h2['ret%']:+6.1f}%", flush=True)
    pd.DataFrame(rows).to_csv(Path(__file__).parent / "results.csv", index=False)


if __name__ == "__main__":
    main()
