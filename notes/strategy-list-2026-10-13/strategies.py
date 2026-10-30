"""
Backtests of the user's strategy list (2026-10-13). See ../strategy-list-2026-10-13.md.

"Tick" = one completed hourly bar. Decisions use the close; market orders fill at
the next bar's open with fee + slippage against us. Take-profit limits fill
intrabar at the TP price when the bar's high reaches it.

Run from the repo root:  PYTHONPATH=. python notes/strategy-list-2026-10-13/strategies.py
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd
from loguru import logger

from data import history_store

logger.remove()
logger.add(sys.stderr, level="WARNING")

START_EQUITY = 500.0
SLIP = 0.0005
LIQUID_TOP_N = 15          # "liquid" = top 15 coins by trailing 7-day dollar volume
MIN_NOTIONAL_24H = 50.0    # the bot's own floor, applied to "all allowed coins"
H24, H72, H168 = 24, 72, 168


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def _ts(x) -> pd.Timestamp:
    x = pd.Timestamp(x)
    return x if x.tzinfo else x.tz_localize("UTC")


def load_panel() -> dict:
    frames = {s: history_store.load(s) for s in history_store.cached_symbols()}
    index = pd.DatetimeIndex(sorted(set().union(*(f.index for f in frames.values()))))
    raw = {k: pd.DataFrame({s: f[k] for s, f in frames.items()}).reindex(index)
           for k in ("open", "high", "low", "close", "volume")}
    close = raw["close"].ffill()
    open_ = raw["open"].fillna(close.shift(1))
    high = raw["high"].fillna(close)
    low = raw["low"].fillna(close)
    vol = raw["volume"].fillna(0)
    dollar = vol * close

    vol24 = vol.rolling(H24, min_periods=1).sum()
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()]).groupby(level=0).max()
    atr = tr.reindex(index).rolling(14, min_periods=14).mean()
    path = close.diff().abs().rolling(H72).sum()
    p = {
        "index": index, "cols": list(close.columns),
        "O": open_.values, "H": high.values, "L": low.values, "C": close.values,
        "traded": (vol > 0).values,
        "liquid": (dollar.rolling(H168, min_periods=H24).mean()
                   .rank(axis=1, ascending=False) <= LIQUID_TOP_N).values,
        "allowed": (dollar.rolling(H24, min_periods=1).mean() >= MIN_NOTIONAL_24H).values,
        "ret24": (close / close.shift(H24) - 1).values,
        "ret48": (close / close.shift(48) - 1).values,
        "ret72": (close / close.shift(H72) - 1).values,
        "ret168": (close / close.shift(H168) - 1).values,
        "vol24": vol24.values,
        "vol24_prev": vol24.shift(H24).values,
        "vol24_avg7d": vol24.rolling(H168, min_periods=H24).mean().values,
        "atr": atr.values,
        # 4h EMA20/50 ~= hourly EMA80/200 (same span the bot's 4h trend uses)
        "ema_fast": close.ewm(span=80, adjust=False).mean().values,
        "ema_slow": close.ewm(span=200, adjust=False).mean().values,
        # efficiency ratio: 1 = straight line, ~0 = chop
        "er72": ((close - close.shift(H72)).abs() / path).values,
    }
    for lb in (24, 48, 72):
        p[f"hi{lb}"] = high.rolling(lb).max().shift(1).values   # prior range, excludes this bar
        p[f"lo{lb}"] = low.rolling(lb).min().shift(1).values
    return p


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

@dataclass
class Pos:
    side: int
    qty: float = 0.0
    units: int = 1
    last_add: float = 0.0
    entry: float = 0.0
    stage: int = 0          # 0 = no TP hit, 1 = TP1 hit, ...
    extra: dict = field(default_factory=dict)
    flow: float = 0.0       # cash in/out for this round trip


class Strategy:
    max_positions = 1
    max_units = 1
    unit_frac = 1.0          # fraction of equity per unit
    def entries(self, p, t, held, locked) -> list[tuple[int, int]]: ...
    def signal(self, p, t, j) -> bool: return False          # for "until signals return" locks
    def manage(self, p, t, j, pos) -> tuple | None: ...       # ("exit",) | ("add",) | None
    def take_profits(self, p, t, j, pos) -> list[tuple[float, float]]: return []  # [(fraction, price)]
    def on_entry(self, p, t, j, pos): ...


def run(p: dict, strat: Strategy, fee: float, start=None, end=None, log=None) -> dict:
    idx = p["index"]
    lo = max(idx.searchsorted(_ts(start)) if start else 0, H168 + 1)
    hi = idx.searchsorted(_ts(end)) if end else len(idx) - 1
    O, H, L, C = p["O"], p["H"], p["L"], p["C"]

    cash = START_EQUITY
    pos: dict[int, Pos] = {}
    locked: set[int] = set()
    pending: list[tuple] = []
    peak, max_dd, fees = START_EQUITY, 0.0, 0.0
    trips: list[float] = []
    entries = 0

    def fill(j, q, px, ps: Pos):
        nonlocal cash, fees
        cost = q * px
        f = abs(cost) * fee
        cash -= cost + f
        fees += f
        ps.qty += q
        ps.flow -= cost + f

    def close_out(j, px):
        ps = pos.pop(j)
        fill(j, -ps.qty, px, ps)
        trips.append(ps.flow)
        if log is not None:
            log.append((p['cols'][j], ps.side, ps.extra.get('t0'), idx[t], ps.entry, px, ps.flow))
        locked.add(j)

    for t in range(lo, hi):
        equity_open = cash + sum(ps.qty * O[t, j] for j, ps in pos.items())
        for kind, j, side in pending:
            px_mkt = lambda s: O[t, j] * (1 + SLIP * s)
            if kind == "exit" and j in pos:
                close_out(j, px_mkt(-pos[j].side))
            elif kind == "enter" and j not in pos and len(pos) < strat.max_positions:
                gross = sum(abs(ps.qty) * O[t, k] for k, ps in pos.items())
                notional = min(strat.unit_frac * equity_open, equity_open - gross)
                if notional <= 1:
                    continue
                ps = pos[j] = Pos(side=side)
                fill(j, side * notional / px_mkt(side), px_mkt(side), ps)
                ps.entry = ps.last_add = O[t, j]
                ps.extra['t0'] = idx[t]
                strat.on_entry(p, t, j, ps)
                entries += 1
            elif kind == "add" and j in pos:
                ps = pos[j]
                gross = sum(abs(q.qty) * O[t, k] for k, q in pos.items())
                notional = min(strat.unit_frac * equity_open, equity_open - gross)
                if notional > 1:
                    fill(j, ps.side * notional / px_mkt(ps.side), px_mkt(ps.side), ps)
                    ps.units += 1
                    ps.last_add = O[t, j]
        pending = []

        # intrabar take-profit limits
        for j in list(pos):
            ps = pos[j]
            for frac, tp in strat.take_profits(p, t, j, ps):
                hit = H[t, j] >= tp if ps.side > 0 else L[t, j] <= tp
                if not hit:
                    break
                px = max(tp, O[t, j]) if ps.side > 0 else min(tp, O[t, j])  # gap through = better fill
                ps.stage += 1
                if frac >= 1 - 1e-9:
                    close_out(j, px)
                    break
                fill(j, -ps.qty * frac, px, ps)

        equity = cash + sum(ps.qty * C[t, j] for j, ps in pos.items())
        peak = max(peak, equity)
        max_dd = max(max_dd, 1 - equity / peak)
        if equity <= 0:
            break

        # unlock coins whose entry signal has switched off since the exit
        locked -= {j for j in locked if not strat.signal(p, t, j)}

        for j, ps in pos.items():
            act = strat.manage(p, t, j, ps)
            if act:
                pending.append((act[0], j, ps.side))
        free = strat.max_positions - len(pos) + sum(1 for a in pending if a[0] == "exit")
        if free > 0:
            for j, side in strat.entries(p, t, set(pos), locked)[:free]:
                pending.append(("enter", j, side))

    for j in list(pos):
        close_out(j, C[hi - 1, j])
    wins = sum(1 for x in trips if x > 0)
    return {
        "ret%": (cash / START_EQUITY - 1) * 100,
        "trades": len(trips),
        "win%": 100 * wins / len(trips) if trips else 0.0,
        "maxDD%": max_dd * 100,
        "fees$": fees,
    }


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

def _ok(p, t, j, universe):
    return p["traded"][t, j] and p[universe][t, j]


class Rules(Strategy):
    """Preamble: sustained trend + convincing volume, confirmation, sit out chop,
    staged TPs, exit on trend break, never add to a loser (no adds at all)."""
    max_positions = 3
    unit_frac = 1 / 3

    def __init__(self, shorts=True, confirm=3, er_min=0.30, vol_mult=1.2, tp_atr=(4, 8)):
        self.shorts, self.confirm, self.er_min, self.vol_mult, self.tp_atr = shorts, confirm, er_min, vol_mult, tp_atr

    def _trend(self, p, t, j):
        f, s = p["ema_fast"][t, j], p["ema_slow"][t, j]
        c = p["C"][t, j]
        if f > s and c > f:
            return 1
        if f < s and c < f:
            return -1
        return 0

    def _setup(self, p, t, j):
        if p["er72"][t, j] < self.er_min or not (p["vol24"][t, j] >= self.vol_mult * p["vol24_avg7d"][t, j]):
            return 0
        sides = {self._trend(p, t - k, j) for k in range(self.confirm)}
        side = sides.pop() if len(sides) == 1 else 0
        return side if (side > 0 or self.shorts) else 0

    def signal(self, p, t, j):
        return self._setup(p, t, j) != 0

    def entries(self, p, t, held, locked):
        c = [(abs(p["ret72"][t, j]), j, self._setup(p, t, j)) for j in range(len(p["cols"]))
             if j not in held and j not in locked and _ok(p, t, j, "liquid")]
        return [(j, s) for _, j, s in sorted(c, reverse=True) if s]

    def on_entry(self, p, t, j, ps):
        ps.extra["atr"] = p["atr"][t - 1, j]

    def take_profits(self, p, t, j, ps):
        a = ps.extra["atr"]
        levels = [(1 / 3, ps.entry + ps.side * self.tp_atr[0] * a),
                  (1 / 2, ps.entry + ps.side * self.tp_atr[1] * a)]  # 1/2 of what's left = 1/3 of original
        return levels[ps.stage:]

    def manage(self, p, t, j, ps):
        f, s = p["ema_fast"][t, j], p["ema_slow"][t, j]
        if (f - s) * ps.side <= 0:          # trend break: 4h EMA20/50 cross back
            return ("exit",)


class MomentumRotation(Strategy):
    """#1 / #2 / #3: long the strongest N-day momentum coin while price and volume rise."""

    def __init__(self, lookback=168, universe="liquid", top=1, must_be_leader=False,
                 exit_mode="literal", add_step=0.03, max_units=3):
        self.lb = {168: "ret168", 72: "ret72"}[lookback]
        self.universe, self.top, self.must_be_leader = universe, top, must_be_leader
        self.exit_mode, self.add_step = exit_mode, add_step
        self.max_positions = top
        self.max_units = max_units
        self.unit_frac = 1 / (top * max_units)

    def _rising(self, p, t, j):
        return (p[self.lb][t, j] > 0 and p["ret24"][t, j] > 0
                and p["vol24"][t, j] > p["vol24_avg7d"][t, j])

    def signal(self, p, t, j):
        return self._rising(p, t, j)

    def entries(self, p, t, held, locked):
        ranked = sorted((p[self.lb][t, j], j) for j in range(len(p["cols"]))
                        if _ok(p, t, j, self.universe) and not np.isnan(p[self.lb][t, j]))[::-1]
        leaders = ranked[: self.top]
        if self.must_be_leader:     # #3: only the single strongest coin, and only if it qualifies
            pool = leaders
        else:                       # #1/#2: strongest coins that qualify (#2 restricted to top 3)
            pool = leaders if self.top > 1 else ranked
        return [(j, 1) for _, j in pool
                if j not in held and j not in locked and self._rising(p, t, j)]

    def manage(self, p, t, j, ps):
        if self.exit_mode == "literal":      # any fade in 24h price or volume
            faded = p["ret24"][t, j] <= 0 or p["vol24"][t, j] < p["vol24_avg7d"][t, j]
        else:                                # buffered: needs a real loss of momentum/volume
            faded = p["ret48"][t, j] <= 0 or p["vol24"][t, j] < 0.7 * p["vol24_avg7d"][t, j]
        if faded:
            return ("exit",)
        if (ps.units < self.max_units and p["C"][t, j] / ps.last_add - 1 >= self.add_step
                and self._rising(p, t, j)):
            return ("add",)


class Breakout(Strategy):
    """#4: one coin; enter after a close above the prior range holds for one more bar;
    sell half at TP1, rest at TP2; cash until the next clean breakout."""

    def __init__(self, range_h=48, max_range=0.08, tp=(1.0, 2.0), stop="mid"):
        self.r, self.max_range, self.tp, self.stop = range_h, max_range, tp, stop

    def _level(self, p, t, j):
        return p[f"hi{self.r}"][t, j], p[f"lo{self.r}"][t, j]

    def _broke(self, p, t, j):
        hi, lo = self._level(p, t - 1, j)       # range as of the breakout bar
        if np.isnan(hi) or hi <= 0 or (hi - lo) / hi > self.max_range:
            return False
        return p["C"][t - 1, j] > hi and p["C"][t, j] > hi   # broke out, then held a tick

    def signal(self, p, t, j):
        return self._broke(p, t, j)

    def entries(self, p, t, held, locked):
        c = []
        for j in range(len(p["cols"])):
            if j in held or j in locked or not _ok(p, t, j, "liquid") or not self._broke(p, t, j):
                continue
            hi, _ = self._level(p, t - 1, j)
            c.append((p["C"][t, j] / hi - 1, j))
        return [(j, 1) for _, j in sorted(c, reverse=True)]

    def on_entry(self, p, t, j, ps):
        hi, lo = self._level(p, t - 2, j)
        ps.extra.update(hi=hi, lo=lo, h=hi - lo)

    def take_profits(self, p, t, j, ps):
        e = ps.extra
        levels = [(0.5, e["hi"] + self.tp[0] * e["h"]), (1.0, e["hi"] + self.tp[1] * e["h"])]
        return levels[ps.stage:]

    def manage(self, p, t, j, ps):
        e = ps.extra
        floor = (e["lo"] + e["hi"]) / 2 if self.stop == "mid" else e["hi"]
        if ps.stage >= 1:
            floor = max(floor, ps.entry)          # after TP1, stop to breakeven
        if p["C"][t, j] < floor:
            return ("exit",)


# ---------------------------------------------------------------------------

def variants() -> dict[str, list[tuple[str, Strategy]]]:
    return {
        "Rules (preamble)": [
            ("long+short, as written", Rules(shorts=True)),
            ("long-only", Rules(shorts=False)),
            ("long-only, stricter (ER 0.4, vol 1.5x)", Rules(shorts=False, er_min=0.4, vol_mult=1.5)),
            ("long-only, wider TPs (6/12 ATR)", Rules(shorts=False, tp_atr=(6, 12))),
        ],
        "#1 7d momentum, liquid": [
            ("as written", MomentumRotation(168, "liquid")),
            ("buffered exit", MomentumRotation(168, "liquid", exit_mode="buffered")),
            ("buffered, no adds", MomentumRotation(168, "liquid", exit_mode="buffered", max_units=1)),
        ],
        "#2 3d momentum, top 3": [
            ("as written", MomentumRotation(72, "liquid", top=3)),
            ("buffered exit", MomentumRotation(72, "liquid", top=3, exit_mode="buffered")),
            ("buffered, no adds", MomentumRotation(72, "liquid", top=3, exit_mode="buffered", max_units=1)),
        ],
        "#3 7d leader only, all coins": [
            ("as written", MomentumRotation(168, "allowed", must_be_leader=True)),
            ("buffered exit", MomentumRotation(168, "allowed", must_be_leader=True, exit_mode="buffered")),
            ("buffered, no adds", MomentumRotation(168, "allowed", must_be_leader=True,
                                                   exit_mode="buffered", max_units=1)),
        ],
        "#4 breakout, TP1/TP2": [
            ("48h range, TP 1x/2x range", Breakout()),
            ("24h range", Breakout(range_h=24)),
            ("72h range", Breakout(range_h=72, max_range=0.12)),
            ("48h, TP 0.5x/1.5x", Breakout(tp=(0.5, 1.5))),
            ("48h, stop at breakout level", Breakout(stop="level")),
        ],
    }


def main():
    p = load_panel()
    idx = p["index"]
    mid = idx[len(idx) // 2]
    print(f"Data {idx[0]:%Y-%m-%d} -> {idx[-1]:%Y-%m-%d %H:%M} UTC, {len(p['cols'])} coins, halves split {mid:%Y-%m-%d}")
    btc = p["C"][:, p["cols"].index("BTC/USD")]
    print(f"BTC hold: full {(btc[-1] / btc[H168 + 1] - 1) * 100:+.1f}%  "
          f"H1 {(btc[len(idx)//2] / btc[H168 + 1] - 1) * 100:+.1f}%  H2 {(btc[-1] / btc[len(idx)//2] - 1) * 100:+.1f}%")
    rows = []
    for group, vs in variants().items():
        for name, s in vs:
            for fee_name, fee in (("Alpaca 0.25%", 0.0025), ("0.10% (OKX-tier)", 0.0010), ("no fees", 0.0)):
                full = run(p, s, fee)
                h1 = run(p, s, fee, end=mid)
                h2 = run(p, s, fee, start=mid)
                rows.append({"strategy": group, "variant": name, "fees": fee_name, **full,
                             "H1%": h1["ret%"], "H2%": h2["ret%"]})
                print(f"{group:30} {name:40} {fee_name:17} ret {full['ret%']:+7.1f}%  "
                      f"trades {full['trades']:4}  win {full['win%']:3.0f}%  DD {full['maxDD%']:5.1f}%  "
                      f"fees ${full['fees$']:4.0f}  H1 {h1['ret%']:+6.1f}%  H2 {h2['ret%']:+6.1f}%", flush=True)
    pd.DataFrame(rows).to_csv("notes/strategy-list-2026-10-13/results.csv", index=False)


if __name__ == "__main__":
    main()
