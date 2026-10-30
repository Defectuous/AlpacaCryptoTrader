"""
Backtest every beebots.tech "Hive" bee rule over the last 7 days (snapshot in hive_board.json).
See ../beebots-hive-2026-10-13.md.

Tick = one completed 15-minute bar. Decisions on the close, market orders fill at the next bar's open
+ 0.05% slippage. Stops / take-profits / trailing stops fill intrabar. Each rule runs at Alpaca's 0.25%
taker fee, OKX-perp-like 0.05%, and zero. 1x leverage everywhere (leverage just scales the result).

Alpaca has no open interest, funding, attention (attn_z) or the beebots "score". Stand-ins:
  oi rising  -> last hour's dollar volume above its 24h hourly average
  score      -> sign agreement of 1h / 4h / 24h returns (0..3)
Coins Alpaca doesn't list (BNB, SUI, ENA, TAO, ZEC, NEAR, MOODENG, PENGU, PUMP) are dropped from lists.

Run from the repo root:  PYTHONPATH=. python notes/beebots-hive-2026-10-13/hive.py
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from loguru import logger

from data import history_store

logger.remove()
logger.add(sys.stderr, level="WARNING")

HERE = Path(__file__).parent
B = 4                       # 15-minute bars per hour
DAY = 24 * B
SLIP = 0.0005
START = 100.0
TEST_DAYS = 7
FEES = (("Alpaca 0.25%", 0.0025), ("OKX perp 0.05%", 0.0005), ("no fees", 0.0))
MEMES = ["DOGE", "BONK", "PEPE", "SHIB", "WIF", "TRUMP"]


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def _ema(df, span):
    return df.ewm(span=span, adjust=False).mean()


def _rsi(close, n):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def load_panel() -> dict:
    syms = history_store.cached_symbols("15Min")
    frames = {s.split("/")[0]: history_store.load(s, timeframe="15Min") for s in syms}
    index = pd.DatetimeIndex(sorted(set().union(*(f.index for f in frames.values()))))
    raw = {k: pd.DataFrame({s: f[k] for s, f in frames.items()}).reindex(index)
           for k in ("open", "high", "low", "close", "volume")}
    C = raw["close"].ffill()
    O = raw["open"].fillna(C.shift(1)).fillna(C)
    H = raw["high"].fillna(C)
    L = raw["low"].fillna(C)
    V = raw["volume"].fillna(0)
    dv = V * C
    r = lambda bars: C / C.shift(bars) - 1
    dv1h = dv.rolling(B).sum()
    dv24 = dv.rolling(DAY).sum()
    prev_c = C.shift(1)
    tr = np.maximum(H - L, np.maximum((H - prev_c).abs(), (L - prev_c).abs()))
    c1h = C.iloc[B - 1::B]                                # hourly closes for hourly indicators
    rsi14_1h = _rsi(c1h, 14).reindex(index).ffill()
    bb_mid = c1h.rolling(20).mean().reindex(index).ffill()
    bb_sd = c1h.rolling(20).std().reindex(index).ffill()

    # daily bars (UTC) from the hourly cache for 200-day SMA / RSI(2)
    hourly = {s.split("/")[0]: history_store.load(s)["close"] for s in history_store.cached_symbols()}
    dclose = pd.DataFrame(hourly).resample("1D").last()
    # a daily value is known only after its day closes -> shift by one day before mapping to bars
    sma200d = dclose.rolling(200).mean().shift(1).reindex(index, method="ffill")
    rsi2d = _rsi(dclose, 2).shift(1).reindex(index, method="ffill")
    dclose_prev = dclose.shift(1).reindex(index, method="ffill")

    day = index.floor("1D")
    day_open = O.groupby(day).transform("first")
    day_hi = H.groupby(day).cummax()
    day_lo = L.groupby(day).cummin()
    dhi = H.groupby(day).max()
    dlo = L.groupby(day).min()
    prev_range = (dhi - dlo).shift(1).reindex(day).set_axis(index)
    # opening range = first hour of the UTC day
    first_hour = (index - day) < pd.Timedelta(hours=1)
    fh = np.repeat(np.asarray(first_hour)[:, None], H.shape[1], axis=1)
    or_hi = H.where(fh).groupby(day).transform("max")
    or_lo = L.where(fh).groupby(day).transform("min")

    p = {"index": index, "cols": list(C.columns), "O": O.values, "H": H.values, "L": L.values, "C": C.values,
         "traded": (V > 0).values,
         "minute": (index.hour * 60 + index.minute).values, "hour": index.hour.values,
         "dayno": ((day - day[0]).days).values}
    for name, bars in (("r15m", 1), ("r30m", 2), ("r1h", B), ("r4h", 4 * B), ("r24h", DAY),
                       ("r48h", 2 * DAY), ("r3d", 3 * DAY), ("r7d", 7 * DAY)):
        p[name] = r(bars).values
    p.update({
        "dv1h": dv1h.values, "dv1h_avg": dv1h.rolling(DAY).mean().values,
        "dv24": dv24.values, "dv24_avg7d": dv24.rolling(7 * DAY, min_periods=DAY).mean().values,
        "dv24_prev": dv24.shift(DAY).values,
        "liquid": (dv.rolling(7 * DAY, min_periods=DAY).mean().rank(axis=1, ascending=False) <= 15).values,
        "top5": (dv.rolling(7 * DAY, min_periods=DAY).mean().rank(axis=1, ascending=False) <= 5).values,
        "top20": (dv.rolling(7 * DAY, min_periods=DAY).mean().rank(axis=1, ascending=False) <= 20).values,
        "atr1h": (tr.rolling(14 * B).mean() * 2).values,     # ~1h ATR from 15m true range
        "er72": ((C - C.shift(3 * DAY)).abs() / C.diff().abs().rolling(3 * DAY).sum()).values,
        "ema15_20": _ema(C, 20).values, "ema15_50": _ema(C, 50).values,
        "ema30_20": _ema(C, 40).values, "ema30_50": _ema(C, 100).values,
        "ema1h_20": _ema(C, 20 * B).values, "ema1h_50": _ema(C, 50 * B).values,
        "sma1h_20": C.rolling(20 * B).mean().values, "sma1h_50": C.rolling(50 * B).mean().values,
        "ema4h_20": _ema(C, 20 * 16).values, "ema4h_50": _ema(C, 50 * 16).values,
        "ema4h_200": _ema(C, 200 * 16).values,
        "rsi14_1h": rsi14_1h.values, "bb_mid": bb_mid.values,
        "bb_up": (bb_mid + 2 * bb_sd).values, "bb_lo": (bb_mid - 2 * bb_sd).values,
        "rsi4h_14": _rsi(C.iloc[16 - 1::16], 14).reindex(index).ffill().values,
        "sma200d": sma200d.values, "rsi2d": rsi2d.values, "dclose_prev": dclose_prev.values,
        "day_open": day_open.values, "day_hi": day_hi.values, "day_lo": day_lo.values,
        "prev_range": prev_range.values, "or_hi": or_hi.values, "or_lo": or_lo.values,
        "first_hour": np.asarray(first_hour),
        "hi7d": H.rolling(7 * DAY).max().shift(1).values,
    })
    for h in (24, 48):
        p[f"hi{h}"] = H.rolling(h * B).max().shift(1).values
        p[f"lo{h}"] = L.rolling(h * B).min().shift(1).values
    for name, lb in (("hi20_1h", 20 * B),):
        p[name] = H.rolling(lb).max().shift(1).values
    # Donchian ensemble on 4h bars: 9 lookbacks, each +1 above mid / -1 below -> score -9..+9
    score = 0
    for n in (10, 15, 20, 30, 40, 55, 70, 90, 120):
        hi_, lo_ = H.rolling(n * 16).max(), L.rolling(n * 16).min()
        score = score + np.sign(C - (hi_ + lo_) / 2)
    p["donch"] = score.values
    p["score3"] = (np.sign(p["r1h"]) + np.sign(p["r4h"]) + np.sign(p["r24h"]))
    return p


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

@dataclass
class Pos:
    side: int
    qty: float = 0.0
    entry: float = 0.0
    units: int = 0
    t0: int = 0
    last_add: float = 0.0
    peak: float = 0.0
    trough: float = 1e18
    stop: float | None = None
    trail: float | None = None      # trailing stop distance as a fraction
    tps: list = field(default_factory=list)   # [(price, fraction_of_current)]
    x: dict = field(default_factory=dict)


class Book:
    def __init__(self, p, fee):
        self.p, self.fee = p, fee
        self.cash, self.pos, self.orders = START, {}, []
        self.fees = 0.0
        self.trips: list[float] = []
        self.flow: dict[int, float] = {}
        self.entries_today: dict[int, int] = {}
        self.last_entry_t = -10**9

    # --- queue orders for the next bar's open
    def open(self, j, side, frac, **kw):
        self.orders.append(("open", j, side, frac, kw))

    def add(self, j, frac):
        self.orders.append(("add", j, 0, frac, {}))

    def close(self, j):
        self.orders.append(("close", j, 0, 1.0, {}))

    def reduce(self, j, frac):
        self.orders.append(("reduce", j, 0, frac, {}))

    def pending(self, j):
        return any(o[1] == j for o in self.orders)

    # --- accounting
    def equity(self, t, px=None):
        px = self.p["C"] if px is None else px
        return self.cash + sum(ps.qty * px[t, j] for j, ps in self.pos.items())

    def _fill(self, j, q, px):
        cost = q * px
        f = abs(cost) * self.fee
        self.cash -= cost + f
        self.fees += f
        self.flow[j] = self.flow.get(j, 0.0) - cost - f
        if j in self.pos:
            self.pos[j].qty += q

    def _exit(self, j, px, frac=1.0):
        ps = self.pos[j]
        q = -ps.qty * frac
        self._fill(j, q, px)
        if frac >= 1 - 1e-9 or abs(ps.qty * px) < 0.01:
            if abs(ps.qty) > 0:
                self._fill(j, -ps.qty, px)
            del self.pos[j]
            self.trips.append(self.flow.pop(j, 0.0))

    def execute(self, t):
        O = self.p["O"]
        eq = self.equity(t, O)
        for kind, j, side, frac, kw in self.orders:
            if kind == "open" and j not in self.pos:
                gross = sum(abs(ps.qty) * O[t, k] for k, ps in self.pos.items())
                usd = min(frac * eq, eq - gross)
                if usd <= 0.5:
                    continue
                px = O[t, j] * (1 + SLIP * side)
                ps = self.pos[j] = Pos(side=side, entry=O[t, j], units=1, t0=t, last_add=O[t, j],
                                       peak=O[t, j], trough=O[t, j], **kw)
                self._fill(j, side * usd / px, px)
                self.entries_today[self.p["dayno"][t]] = self.entries_today.get(self.p["dayno"][t], 0) + 1
                self.last_entry_t = t
            elif kind == "add" and j in self.pos:
                ps = self.pos[j]
                gross = sum(abs(q.qty) * O[t, k] for k, q in self.pos.items())
                usd = min(frac * eq, eq - gross)
                if usd > 0.5:
                    px = O[t, j] * (1 + SLIP * ps.side)
                    q = ps.side * usd / px
                    ps.entry = (ps.entry * abs(ps.qty) + O[t, j] * abs(q)) / (abs(ps.qty) + abs(q))
                    self._fill(j, q, px)
                    ps.units += 1
                    ps.last_add = O[t, j]
            elif kind in ("close", "reduce") and j in self.pos:
                self._exit(j, O[t, j] * (1 - SLIP * self.pos[j].side), frac)
        self.orders = []

    def intrabar(self, t):
        O, H, L = self.p["O"], self.p["H"], self.p["L"]
        for j in list(self.pos):
            ps = self.pos[j]
            s = ps.side
            if ps.trail is not None:
                ref = ps.peak if s > 0 else ps.trough
                tstop = ref * (1 - s * ps.trail)
                ps.stop = tstop if ps.stop is None else (max(ps.stop, tstop) if s > 0 else min(ps.stop, tstop))
            if ps.stop is not None and ((s > 0 and L[t, j] <= ps.stop) or (s < 0 and H[t, j] >= ps.stop)):
                px = min(O[t, j], ps.stop) if s > 0 else max(O[t, j], ps.stop)
                self._exit(j, px * (1 - SLIP * s))
                continue
            while ps.tps:
                tp, frac = ps.tps[0]
                if not ((s > 0 and H[t, j] >= tp) or (s < 0 and L[t, j] <= tp)):
                    break
                ps.tps.pop(0)
                ps.x["tp_hit"] = ps.x.get("tp_hit", 0) + 1
                self._exit(j, max(O[t, j], tp) if s > 0 else min(O[t, j], tp), frac)
                if j not in self.pos:
                    break
            if j in self.pos:
                ps.peak, ps.trough = max(ps.peak, H[t, j]), min(ps.trough, L[t, j])


def run(p, strat_cls, kw, fee, start, end):
    book = Book(p, fee)
    s = strat_cls(p, book, **kw)
    peak, mdd = START, 0.0
    for t in range(start, end):
        book.execute(t)
        book.intrabar(t)
        eq = book.equity(t)
        peak, mdd = max(peak, eq), max(mdd, 1 - eq / peak)
        if t < end - 1:
            s.tick(t)
    for j in list(book.pos):
        book._exit(j, p["C"][end - 1, j] * (1 - SLIP * book.pos[j].side))
    wins = sum(1 for x in book.trips if x > 0)
    return {"ret%": (book.cash / START - 1) * 100, "trades": len(book.trips),
            "win%": 100 * wins / len(book.trips) if book.trips else 0.0,
            "maxDD%": mdd * 100, "fees%": book.fees / START * 100}


# ---------------------------------------------------------------------------
# Strategy helpers
# ---------------------------------------------------------------------------

class Strat:
    def __init__(self, p, book, **kw):
        self.p, self.b = p, book
        self.col = {c: i for i, c in enumerate(p["cols"])}
        for k, v in kw.items():
            setattr(self, k, v)
        self.setup()

    def setup(self):
        pass

    def f(self, name, t, j):
        return self.p[name][t, j]

    def universe(self, t, spec):
        if isinstance(spec, (list, tuple)):
            js = [self.col[c] for c in spec if c in self.col]
        else:
            js = range(len(self.p["cols"]))
            if spec in ("liquid", "top5", "top20"):
                js = [j for j in js if self.p[spec][t, j]]
        return [j for j in js if not np.isnan(self.p["r7d"][t, j])]

    def oi_up(self, t, j):          # open-interest stand-in
        return self.f("dv1h", t, j) > self.f("dv1h_avg", t, j)

    def upl(self, t, j):
        ps = self.b.pos[j]
        return ps.side * (self.p["C"][t, j] / ps.entry - 1)

    def trend4h(self, t, j):
        f, s, c = self.f("ema4h_20", t, j), self.f("ema4h_50", t, j), self.p["C"][t, j]
        return 1 if (f > s and c > f) else -1 if (f < s and c < f) else 0


# --- A. Chase the hottest mover (Mayhem family)
class Chase(Strat):
    rank = "r4h"; shorts = True; adds = 3; thresh = 0.02; chop = False; uni = "allowed"
    exit_on = "r1h"; pair = False; flip_on = None

    def tick(self, t):
        b, p = self.b, self.p
        for j, ps in list(b.pos.items()):
            mom = self.f(self.exit_on, t, j) * ps.side
            if mom <= 0:
                b.close(j)
            elif ps.units < self.adds and self.upl(t, j) > 0.01 * ps.units:
                b.add(j, 1 / self.adds)
        if self.pair:                                    # long strongest, short weakest
            u = self.universe(t, self.uni)
            if b.pos or b.orders or not u:
                return
            best = max(u, key=lambda j: self.f(self.rank, t, j))
            worst = min(u, key=lambda j: self.f(self.rank, t, j))
            if self.f(self.rank, t, best) > self.thresh:
                b.open(best, 1, 0.5)
            if self.f(self.rank, t, worst) < -self.thresh:
                b.open(worst, -1, 0.5)
            return
        if b.pos and not b.orders:
            return
        cands = []
        for j in self.universe(t, self.uni):
            m = self.f(self.rank, t, j)
            if np.isnan(m) or abs(m) < self.thresh or (m < 0 and not self.shorts):
                continue
            if np.sign(self.f("r1h", t, j)) != np.sign(m):
                continue
            if self.chop and self.f("er72", t, j) < 0.3:
                continue
            cands.append((abs(m), j, int(np.sign(m))))
        if cands and (not b.pos or all(o[0] == "close" for o in b.orders)):
            _, j, side = max(cands)
            if j not in b.pos:
                b.open(j, side, 1 / self.adds)


# --- B. Trend + volume rule set (Windward family)
class TrendRules(Strat):
    uni = "liquid"; shorts = True; maxpos = 3; confirm = 3 * B; er_min = 0.30; vol_mult = 1.2
    tp_atr = (4, 8); pullback = False; vol_need = True; add_winner = False

    def _setup_side(self, t, j):
        if self.vol_need and not (self.f("dv24", t, j) >= self.vol_mult * self.f("dv24_avg7d", t, j)):
            return 0
        if self.f("er72", t, j) < self.er_min:
            return 0
        sides = {self.trend4h(t - k, j) for k in range(0, self.confirm, B)}
        side = sides.pop() if len(sides) == 1 else 0
        if side < 0 and not self.shorts:
            return 0
        if side and self.pullback:
            c, e = self.p["C"][t, j], self.f("ema1h_20", t, j)
            if side * (c / e - 1) > 0.005:          # wait for price to come back near the 1h EMA20
                return 0
        return side

    def tick(self, t):
        b = self.b
        for j, ps in list(b.pos.items()):
            if (self.f("ema4h_20", t, j) - self.f("ema4h_50", t, j)) * ps.side <= 0:
                b.close(j)
            elif self.add_winner and ps.units < 2 and self.upl(t, j) >= ps.x["r"]:
                b.add(j, 1 / self.maxpos / 2)
        free = self.maxpos - len(b.pos)
        if free <= 0:
            return
        c = [(abs(self.f("r3d", t, j)), j, self._setup_side(t, j)) for j in self.universe(t, self.uni)
             if j not in b.pos]
        for _, j, side in sorted(c, reverse=True):
            if side and free > 0:
                a = self.f("atr1h", t, j)
                e = self.p["C"][t, j]
                tps = [(e + side * self.tp_atr[0] * a, 1 / 3), (e + side * self.tp_atr[1] * a, 1 / 2)]
                b.open(j, side, 1 / self.maxpos, tps=tps, x={"r": 2 * a / e})
                free -= 1


# --- C/D. Momentum leader / rotation family (Orakelia, Boozy, APE rules...)
class Leader(Strat):
    rank = "r7d"; uni = "allowed"; n = 1
    pick = "leader"            # leader = only the #1 coin; best = best coin that qualifies
    enter = None               # callable(self, t, j) -> bool
    unit = 1 / 3; max_units = 3
    add = None                 # None | "tick" | "profit" | "newhigh" | "rising"
    add_profit = 0.03
    exit = None                # callable(self, t, j, ps, is_leader) -> bool
    switch = True; margin = 0.0; min_hold = 0
    lock = "signal"            # "signal" | "newleader" | None
    shorts = False

    def setup(self):
        self.locked = {}
        self.enter = self.enter or (lambda s, t, j: s.f(s.rank, t, j) > 0)
        self.exit = self.exit or (lambda s, t, j, ps, lead: not lead or s.f(s.rank, t, j) <= 0)

    def _ranked(self, t):
        u = self.universe(t, self.uni)
        return sorted(u, key=lambda j: self.f(self.rank, t, j), reverse=True)

    def tick(self, t):
        b = self.b
        ranked = self._ranked(t)
        if not ranked:
            return
        leader = ranked[0]
        top = set(ranked[: self.n])
        # unlock
        for j in list(self.locked):
            if self.lock == "signal" and not self.enter(self, t, j):
                del self.locked[j]
            elif self.lock == "newleader" and leader != self.locked[j]:
                del self.locked[j]
        for j, ps in list(b.pos.items()):
            held = t - ps.t0
            lead = j in top
            if self.switch and not lead and self.margin and j in ranked:
                lead = self.f(self.rank, t, leader) <= self.f(self.rank, t, j) + self.margin
            if held >= self.min_hold and self.exit(self, t, j, ps, lead):
                b.close(j)
                if self.lock:
                    self.locked[j] = leader
                continue
            if ps.units < self.max_units and self.add and lead:
                c = self.p["C"][t, j]
                ok = {"tick": True,
                      "profit": c / ps.last_add - 1 >= self.add_profit,
                      "newhigh": c >= ps.peak,
                      "rising": c / ps.last_add - 1 >= self.add_profit and self.f("r1h", t, j) > 0
                                and self.oi_up(t, j)}[self.add]
                if ok:
                    b.add(j, self.unit)
        closing = {o[1] for o in b.orders if o[0] == "close"}
        open_n = len([j for j in b.pos if j not in closing])
        pool = ranked[: self.n] if self.pick == "leader" else ranked
        for j in pool:
            if open_n >= self.n:
                break
            if j in b.pos or j in self.locked or not self.enter(self, t, j):
                continue
            b.open(j, 1, self.unit)
            open_n += 1


def rising(s, t, j):          # "price and volume rising"
    return s.f("r24h", t, j) > 0 and s.f("dv24", t, j) > s.f("dv24_avg7d", t, j)


def faded(s, t, j):
    return s.f("r24h", t, j) <= 0 or s.f("dv24", t, j) < s.f("dv24_avg7d", t, j)


# --- E. Range breakout with TP1/TP2 (Range Splitter family)
class RangeBreak(Strat):
    uni = "allowed"; rng = 48; max_rng = 0.08; tp = (1.0, 2.0); stop = "mid"; rank = "breakout"
    shorts = False; need_trend = False; exit_fade = False

    def tick(self, t):
        b = self.b
        for j, ps in list(b.pos.items()):
            hi, lo = ps.x["hi"], ps.x["lo"]
            c = self.p["C"][t, j]
            floor = (hi + lo) / 2 if self.stop == "mid" else hi
            if ps.side < 0:
                if c > (hi + lo) / 2 if self.stop == "mid" else c > lo:
                    b.close(j)
                continue
            if ps.x.get("tp_hit"):
                floor = max(floor, ps.entry)
            if c < floor or (self.exit_fade and self.f("r1h", t, j) < 0):
                b.close(j)
        if b.pos:
            return
        cands = []
        for j in self.universe(t, self.uni):
            hi, lo = self.f(f"hi{self.rng}", t - 1, j), self.f(f"lo{self.rng}", t - 1, j)
            if np.isnan(hi) or hi <= 0 or (hi - lo) / hi > self.max_rng:
                continue
            c0, c1 = self.p["C"][t - 1, j], self.p["C"][t, j]
            if c0 > hi and c1 > hi and (not self.need_trend or (self.trend4h(t, j) > 0
                                                                 and self.f("ema30_20", t, j) > self.f("ema30_50", t, j))):
                score = c1 / hi - 1 if self.rank == "breakout" else self.f("r7d", t, j)
                cands.append((score, j, 1, hi, lo))
            elif self.shorts and c0 < lo and c1 < lo:
                cands.append((lo / c1 - 1, j, -1, hi, lo))
        if cands:
            _, j, side, hi, lo = max(cands)
            h = hi - lo
            tps = [] if self.exit_fade or self.tp is None else (
                [(hi + self.tp[0] * h, 0.5), (hi + self.tp[1] * h, 1.0)] if side > 0
                else [(lo - self.tp[0] * h, 0.5), (lo - self.tp[1] * h, 1.0)])
            b.open(j, side, 1.0, tps=tps, x={"hi": hi, "lo": lo})


# --- F. One daily volatility breakout, hold to the UTC close (Honey Breaker family)
class DailyBreak(Strat):
    coins = ("BTC", "ETH", "SOL", "HYPE"); k = 0.5; exit_rule = "open"; confirm_mom = False
    confirm_vol = False; shorts = False; orb = False; pick = "strongest"; window = None; exit_hour = None

    def setup(self):
        self.done_day = -1

    def trigger(self, t, j, side):
        if self.orb:
            return self.f("or_hi" if side > 0 else "or_lo", t, j)
        return self.f("day_open", t, j) + side * self.k * self.f("prev_range", t, j)

    def tick(self, t):
        b, p = self.b, self.p
        d = p["dayno"][t]
        end_of_day = p["minute"][t] == 24 * 60 - 15
        for j, ps in list(b.pos.items()):
            c = p["C"][t, j]
            out = end_of_day or (self.exit_hour is not None and p["hour"][t] >= self.exit_hour)
            if self.exit_rule == "open":
                out |= (c - self.f("day_open", t, j)) * ps.side < 0
            elif self.exit_rule == "range":
                out |= (c - ps.x["trig"]) * ps.side < 0
            elif self.exit_rule == "stall":
                out |= self.f("r1h", t, j) * ps.side < 0
            elif self.exit_rule == "cutR":
                out |= self.upl(t, j) <= -0.5 * ps.x["R"] and self.f("r1h", t, j) * ps.side < 0
            if out:
                b.close(j)
        if b.pos or self.done_day == d or end_of_day:
            return
        if self.orb and p["first_hour"][t]:
            return
        if self.window and not (self.window[0] <= p["hour"][t] < self.window[1]):
            return
        cands = []
        for j in self.universe(t, list(self.coins)):
            for side in ((1, -1) if self.shorts else (1,)):
                trig = self.trigger(t, j, side)
                c = p["C"][t, j]
                if np.isnan(trig) or (c - trig) * side <= 0:
                    continue
                if self.confirm_mom and (self.f("r1h", t, j) * side < 0 or not self.oi_up(t, j)):
                    continue
                if self.confirm_vol and not self.oi_up(t, j):
                    continue
                score = (side * (c / trig - 1) if self.pick == "strongest"
                         else -self.f("prev_range", t, j) / c)
                cands.append((score, j, side, trig))
        if cands:
            _, j, side, trig = max(cands)
            R = abs(self.p["C"][t, j] - trig) / self.p["C"][t, j] + 0.005
            b.open(j, side, 1.0, x={"trig": trig, "R": R})
            self.done_day = d


# --- G. Short the fade after a pump (PeakSting family)
class Fade(Strat):
    variant = "peak"

    def tick(self, t):
        b, p = self.b, self.p
        for j, ps in list(b.pos.items()):      # "let the code manage the exit": 5% trail, -10% target, 24h max
            if t - ps.t0 >= DAY:
                b.close(j)
        if b.pos or b.orders:
            return
        cands = []
        for j in self.universe(t, "allowed"):
            c = p["C"][t, j]
            pump = self.f("r24h", t, j)
            hi24 = max(self.f("hi24", t, j), c)
            off24 = 1 - c / hi24
            if pump < 0.08 or self.f("dv24", t, j) < 1.5 * self.f("dv24_avg7d", t, j):
                continue
            if self.variant == "peak" and off24 >= 0.03:
                cands.append((pump, j))
            elif self.variant == "dry" and off24 >= 0.04 and self.f("dv1h", t, j) < 0.5 * self.f("dv1h_avg", t, j) \
                    and self.f("r1h", t, j) <= 0:
                cands.append((pump, j))
        if cands:
            _, j = max(cands)
            c = p["C"][t, j]
            b.open(j, -1, 1.0, trail=0.05, tps=[(c * 0.90, 1.0)])



# --- H. Single-signal trend rules on fixed coins
class SimpleTrend(Strat):
    coins = ("BTC",); kind = "ema4h"; shorts = False; pick = "first"

    def sig(self, t, j):
        f = lambda n: self.f(n, t, j)
        c = self.p["C"][t, j]
        k = self.kind
        if k == "ema4h":            # 4h EMA20 vs EMA50
            return 1 if f("ema4h_20") > f("ema4h_50") else -1
        if k == "ema4h_50_200":     # 4h EMA50/200 cross + RSI > 55 to enter
            return 1 if f("ema4h_50") > f("ema4h_200") else -1
        if k == "tf4h_30m":         # 4h and 30m trends both up
            up = f("ema4h_20") > f("ema4h_50") and f("ema30_20") > f("ema30_50")
            dn = f("ema4h_20") < f("ema4h_50") or f("ema30_20") < f("ema30_50")
            return 1 if up else -1 if dn else 0
        if k == "tf15_1h":          # 15m call confirmed by 1h, exit when 1h flips
            m15 = np.sign(f("ema15_20") - f("ema15_50"))
            h1 = np.sign(f("ema1h_20") - f("ema1h_50"))
            return int(h1) if m15 == h1 else 0
        if k == "ema1h":
            return 1 if f("ema1h_20") > f("ema1h_50") else -1
        if k == "swing":            # above recent swing trend + positive momentum
            up = c > f("ema1h_50") and f("r24h") > 0
            dn = c < f("ema1h_50") and f("r24h") < 0
            return 1 if up else -1 if dn else 0
        if k == "agree":            # momentum & direction agree (r4h, r24h, trend)
            s3 = f("score3")
            tr = 1 if f("ema1h_20") > f("ema1h_50") else -1
            return tr if s3 == 3 * tr else 0
        if k == "pullback":         # trend up, momentum cooled after small pullback
            tr = self.trend4h(t, j)
            return tr if tr and f("r4h") * tr < 0 and abs(f("r4h")) < 0.02 else 0
        if k == "higher_highs":     # multi-day uptrend with higher highs, exit only on clear break
            up = f("r3d") > 0 and c > f("ema4h_20") > f("ema4h_50")
            dn = f("r3d") < 0 and c < f("ema4h_20") < f("ema4h_50")
            return 1 if up else -1 if dn else 0
        raise ValueError(k)

    def keep(self, t, j, ps):
        k = self.kind
        if k == "tf15_1h":
            return np.sign(self.f("ema1h_20", t, j) - self.f("ema1h_50", t, j)) == ps.side
        if k == "pullback":
            return self.trend4h(t, j) == ps.side
        if k in ("swing", "agree"):
            return self.sig(t, j) != -ps.side
        if k == "higher_highs":     # exit only when the 4h EMAs cross
            return (self.f("ema4h_20", t, j) - self.f("ema4h_50", t, j)) * ps.side > 0
        return self.sig(t, j) == ps.side

    def tick(self, t):
        b = self.b
        for j, ps in list(b.pos.items()):
            if not self.keep(t, j, ps):
                b.close(j)
        if b.pos and not b.orders:
            return
        u = self.universe(t, list(self.coins) if self.coins else "allowed")
        if self.pick == "r7d":
            u = sorted(u, key=lambda j: self.f("r7d", t, j), reverse=True)
        for j in u:
            if j in b.pos:
                continue
            s = self.sig(t, j)
            if s < 0 and not self.shorts:
                continue
            if s and (self.kind != "ema4h_50_200" or self.f("rsi4h_14", t, j) > 55):
                if self.kind in ("ema4h_50_200", "tf4h_30m") and self.sig(t - 1, j) == s:
                    continue      # wait for a fresh cross / flip
                b.open(j, s, 1.0 / max(1, min(len(self.coins or [1]), 2)) if self.pick == "split" else 1.0)
                if self.pick != "split":
                    break


# --- I. Donchian ensemble on 4h (Donchian Drifter / Breezy Bee / Volta Fang)
class Donchian(Strat):
    coins = ("BTC", "ETH"); mode = "drifter"

    def tick(self, t):
        b = self.b
        u = self.universe(t, list(self.coins))
        sc = {j: self.f("donch", t, j) for j in u}
        for j, ps in list(b.pos.items()):
            s = sc[j] * ps.side
            if self.mode == "drifter" and s < 3:
                b.close(j)
            elif self.mode == "breezy":
                if s <= 0:
                    b.close(j)
                elif s <= ps.x["best"] - 3 and not ps.x.get("trimmed"):
                    b.reduce(j, 0.5); ps.x["trimmed"] = True
                ps.x["best"] = max(ps.x["best"], s)
            elif self.mode == "volta":
                other = [k for k in u if k != j]
                if s <= 0 or (other and sc[other[0]] * ps.side >= s + 4):
                    b.close(j)
        if b.pos and not b.orders:
            return
        if self.mode == "breezy" and (b.entries_today.get(self.p["dayno"][t], 0) >= 2
                                      or t - b.last_entry_t < 16):
            return
        best = max(u, key=lambda j: sc[j])
        worst = min(u, key=lambda j: sc[j])
        if sc[best] >= 3 and best not in b.pos:
            b.open(best, 1, 1.0, x={"best": sc[best]})
        elif self.mode != "drifter" and sc[worst] <= -3 and worst not in b.pos:
            b.open(worst, -1, 1.0, x={"best": -sc[worst]})


# --- J. RSI(2) pullback above the 200-day SMA (BamBee / ConnorsThorp / KellyPullback)
class RSI2(Strat):
    coins = ("BTC", "ETH"); risk = 0.05; stop = 0.04; rr = 1.5; exit_rsi = True

    def tick(self, t):
        b = self.b
        for j, ps in list(b.pos.items()):
            if self.exit_rsi and self.f("rsi2d", t, j) >= 50 and self.p["minute"][t] == 0:
                b.close(j)
        for j in self.universe(t, list(self.coins)):
            if j in b.pos or self.p["minute"][t] != 0:
                continue
            c = self.p["C"][t, j]
            if c > self.f("sma200d", t, j) and self.f("rsi2d", t, j) < 25:
                size = min(self.risk / self.stop, 1.0) / len(self.coins)
                b.open(j, 1, size, stop=c * (1 - self.stop), tps=[(c * (1 + self.rr * self.stop), 1.0)])


# --- K. Fast scalp rules (Vol Spike / Range Breaker / Zenith)
class Scalp(Strat):
    coins = None; cond = "r1h"; cut_after = 1; hold_min = 4; trail = 0.01

    def tick(self, t):
        b = self.b
        for j, ps in list(b.pos.items()):
            held = t - ps.t0
            if held >= self.cut_after and self.upl(t, j) < 0 and held < self.hold_min:
                b.close(j)
            elif held >= self.hold_min and ps.trail is None:
                ps.trail = self.trail
        if b.pos:
            return
        cands = []
        for j in self.universe(t, list(self.coins) if self.coins else "liquid"):
            r1 = self.f("r1h", t, j)
            if self.cond == "volspike" and r1 > 0.002 and self.f("score3", t, j) >= 2:
                cands.append((r1, j))
            elif self.cond == "trigger":
                trig = self.f("day_open", t, j) + 0.5 * self.f("prev_range", t, j)
                c = self.p["C"][t, j]
                if r1 > 0 and abs(c / trig - 1) < 0.0005:
                    cands.append((r1, j))
            elif self.cond == "score1" and r1 > 0 and self.f("score3", t, j) >= 1:
                cands.append((r1, j))
        if cands:
            b.open(max(cands)[1], 1, 1.0)


# --- L. BTC dip buyer
class DipBuy(Strat):
    def tick(self, t):
        b = self.b
        j = self.col["BTC"]
        if not b.pos and self.f("r24h", t, j) <= -0.02:
            b.open(j, 1, 1.0, tps=[(self.p["C"][t, j] * 1.04, 1.0)])


# --- M. DCA into the top 20 by volume, once a day
class DCA(Strat):
    def tick(self, t):
        if self.p["minute"][t] != 0:
            return
        u = self.universe(t, "top20")
        for j in u:
            frac = 1 / 7 / len(u)
            if j in self.b.pos:
                self.b.add(j, frac)
            else:
                self.b.open(j, 1, frac)


# --- N. Meme / fast momentum
class Meme(Strat):
    coins = tuple(MEMES); variant = "sleuth"

    def tick(self, t):
        b = self.b
        for j, ps in list(b.pos.items()):
            r1 = self.f("r1h", t, j)
            v = self.variant
            if v == "sleuth" and r1 < -0.06:
                b.close(j)
            elif v == "sleuth" and ps.units < 2 and 0 < self.upl(t, j) < 0.08 and r1 > 0:
                b.add(j, 0.5)
            elif v in ("flare", "hunter") and r1 <= 0:
                b.close(j)
            elif v == "slurp" and self.f("r30m", t, j) < 0:
                b.close(j)
        if b.pos:
            return
        cands = []
        for j in self.universe(t, list(self.coins) if self.coins else "allowed"):
            r1, r24 = self.f("r1h", t, j), self.f("r24h", t, j)
            spike = self.f("dv1h", t, j) / max(self.f("dv1h_avg", t, j), 1e-9)
            v = self.variant
            if v == "sleuth" and r24 > 0 and 0 < r1 <= 0.08:
                cands.append((r24, j))
            elif v == "flare" and r1 >= 0.03 and spike >= 2:
                cands.append((r1, j))
            elif v == "hunter" and r1 > 0 and spike >= 3:
                cands.append((spike, j))
            elif v == "slurp" and r1 > 0.01 and spike >= 2:
                cands.append((r1, j))
        if cands:
            j = max(cands)[1]
            c = self.p["C"][t, j]
            if self.variant == "sleuth":
                b.open(j, 1, 0.5, trail=0.05)
            elif self.variant == "flare":
                b.open(j, 1, 0.33, stop=c * 0.95)
            else:
                b.open(j, 1, 1.0)


# --- O. Bizzy Bee: Bollinger/RSI fade, 4h max hold, 4 trades/day 30 min apart
class Bizzy(Strat):
    coins = ("BTC", "DOGE", "ETH", "HYPE", "LINK", "SOL", "XRP")

    def tick(self, t):
        b = self.b
        for j, ps in list(b.pos.items()):
            c = self.p["C"][t, j]
            if t - ps.t0 >= 4 * B or (c - self.f("bb_mid", t, j)) * ps.side >= 0:
                b.close(j)
        if b.pos or b.entries_today.get(self.p["dayno"][t], 0) >= 4 or t - b.last_entry_t < 2:
            return
        for j in self.universe(t, list(self.coins)):
            c, rsi = self.p["C"][t, j], self.f("rsi14_1h", t, j)
            a = self.f("atr1h", t, j)
            if rsi < 30 and c < self.f("bb_lo", t, j):
                b.open(j, 1, 1.0, stop=c - a, tps=[(c + a, 1.0)])
                return
            if rsi > 70 and c > self.f("bb_up", t, j):
                b.open(j, -1, 1.0, stop=c + a, tps=[(c - a, 1.0)])
                return


# --- P. Squeeze Bomber: >15% 24h pump with "OI" (volume) jump; exit when it drops
class Squeeze(Strat):
    def tick(self, t):
        b = self.b
        for j, ps in list(b.pos.items()):
            if not self.oi_up(t, j):
                b.close(j)
            elif ps.units < 3 and self.f("dv1h", t, j) > self.f("dv1h", t - B, j):
                b.add(j, 1 / 3)
        if b.pos:
            return
        for j in self.universe(t, "liquid"):
            if self.f("r24h", t, j) > 0.15 and self.f("dv1h", t, j) > 1.2 * self.f("dv1h_avg", t, j):
                b.open(j, 1, 1 / 3)
                return


# --- Q. Boozy Bee (perp fresh momentum, both ways, 3 trades/day an hour apart)
class BoozyPerp(Strat):
    def tick(self, t):
        b = self.b
        for j, ps in list(b.pos.items()):
            held = t - ps.t0
            if held >= 12 * 4 or (held >= 2 and self.f("r30m", t, j) * ps.side <= 0):
                b.close(j)
            elif ps.units < 2 and self.upl(t, j) > 0.01:
                b.add(j, 0.5)
        if b.pos or b.entries_today.get(self.p["dayno"][t], 0) >= 3 or t - b.last_entry_t < B:
            return
        cands = []
        for j in self.universe(t, "liquid"):
            d = self.p["C"][t, j] / self.f("day_open", t, j) - 1
            r1 = self.f("r1h", t, j)
            if d >= 0.015 and r1 > 0 and self.oi_up(t, j):
                cands.append((d, j, 1))
            elif d <= -0.015 and r1 < 0 and self.oi_up(t, j):
                cands.append((-d, j, -1))
        if cands:
            _, j, side = max(cands)
            b.open(j, side, 1 / 1.5)


# --- R. Odd ones
class LinkAvax(Strat):
    def tick(self, t):
        b = self.b
        for j, ps in list(b.pos.items()):
            if self.f("r24h", t, j) <= 0:
                b.close(j)
            elif ps.units < 2 and self.upl(t, j) > 0.02 and self.f("r4h", t, j) > 0:
                b.add(j, 0.5)
        if b.pos:
            return
        for j in self.universe(t, ["LINK", "AVAX"]):
            pos_ticks = all(self.f("r24h", t - k, j) > 0 for k in range(0, 3 * B, B))
            if pos_ticks and self.f("r1h", t, j) < 0 and self.f("r4h", t, j) > -0.02:
                b.open(j, 1, 0.5)
                return


class WeeklyHigh(Strat):
    coins = ("ADA", "ETH", "LINK", "SOL")

    def tick(self, t):
        b = self.b
        for j, ps in list(b.pos.items()):
            if self.p["C"][t, j] < self.f("ema4h_20", t, j):
                b.close(j)
        for j in self.universe(t, list(self.coins)):
            if j not in b.pos and self.p["C"][t, j] > self.f("hi7d", t, j) and not b.pending(j):
                b.open(j, 1, 1 / len(self.coins))


class MemeMA(Strat):
    def tick(self, t):
        b = self.b
        for j, ps in list(b.pos.items()):
            if self.p["C"][t, j] < self.f("sma1h_20", t, j):
                b.close(j)
        if b.pos:
            return
        q = []
        for j in self.universe(t, MEMES):
            c = self.p["C"][t, j]
            s20, s50 = self.f("sma1h_20", t, j), self.f("sma1h_50", t, j)
            if c > s20 and c > s50 and s20 > self.f("sma1h_20", t - B, j) and s50 > self.f("sma1h_50", t - B, j):
                q.append((self.f("r7d", t, j), j))
        if q:
            b.open(max(q)[1], 1, 1.0)


# ---------------------------------------------------------------------------
# Bee -> rule mapping (rule number = order in hive_board.json grouped by text)
# ---------------------------------------------------------------------------

def cfg():
    E = lambda **k: k
    L = Leader
    R = {}
    # A. chase
    R[1] = (Chase, E())
    R[22] = (Chase, E())                                         # same rule at 50x -> run at 1x
    R[11] = (Chase, E(chop=True))
    R[36] = (Chase, E(rank="r7d", thresh=0.0, exit_on="r1h"))
    R[71] = (Chase, E(rank="r7d", thresh=0.0, exit_on="r24h"))
    R[78] = (Chase, E(rank="r24h", pair=True, thresh=0.0, exit_on="r4h", adds=1))
    # B. trend + volume
    R[2] = (TrendRules, E())
    R[12] = (TrendRules, E())
    R[34] = (TrendRules, E(uni=["BTC", "ETH"], maxpos=2, add_winner=True, er_min=0.0))
    R[14] = (TrendRules, E(uni=["BTC", "ETH"], maxpos=2, vol_need=False, tp_atr=(99, 99)))
    R[19] = (TrendRules, E(uni=["BTC", "ETH"], maxpos=2, vol_need=False, tp_atr=(99, 99), add_winner=True, er_min=0.0))
    R[41] = (TrendRules, E(uni=["BTC", "ETH"], maxpos=2, vol_need=False, tp_atr=(99, 99), shorts=False, pullback=True, er_min=0.0))
    # C. momentum rotation, price & volume rising
    MR = E(enter=lambda s, t, j: s.f(s.rank, t, j) > 0 and rising(s, t, j),
           exit=lambda s, t, j, ps, lead: faded(s, t, j), add="rising", switch=False)
    R[3] = (L, dict(MR, uni="liquid", pick="best"))
    R[4] = (L, dict(MR, rank="r3d", uni="liquid", n=3, unit=1 / 9, pick="leader"))
    R[5] = (L, dict(MR, uni="allowed"))
    R[18] = (L, dict(MR, uni="allowed"))
    R[37] = (L, dict(MR, uni="liquid"))
    # D. leader chase
    R[9] = (L, E(add="tick"))
    R[16] = (L, E(add="tick"))
    R[62] = (L, E(unit=1.0, max_units=1))
    R[13] = (L, E(uni="liquid", add="tick", min_hold=DAY,
                  exit=lambda s, t, j, ps, lead: not lead))
    R[28] = (L, E(uni="liquid", add="profit", add_profit=0.02, margin=0.02,
                  enter=lambda s, t, j: s.f("r24h", t, j) > 0 and s.f("r7d", t, j) > 0 and s.oi_up(t, j),
                  exit=lambda s, t, j, ps, lead: not lead or (s.f("r24h", t, j) <= 0 and not s.oi_up(t, j))))
    R[31] = (L, E(uni=["ADA", "AVAX", "BCH", "BTC", "DOGE", "DOT", "ETH", "LINK", "LTC", "SOL"], add="profit",
                  enter=lambda s, t, j: s.f("r24h", t, j) > 0 and s.f("r7d", t, j) > 0 and s.oi_up(t, j)
                  and s.f("r1h", t, j) < 0.02,
                  exit=lambda s, t, j, ps, lead: not lead or s.f("r24h", t, j) <= 0))
    R[30] = (L, E(uni=["BTC", "ETH", "SOL", "XRP", "HYPE", "LINK"], add="tick",
                  enter=lambda s, t, j: min(s.f("r7d", t, j), s.f("r24h", t, j), s.f("r1h", t, j)) > 0 and s.oi_up(t, j),
                  exit=lambda s, t, j, ps, lead: not lead or s.f("r1h", t, j) < 0 or s.f("r24h", t, j) < 0))
    R[44] = (L, E(unit=1.0, max_units=1,
                  enter=lambda s, t, j: s.f("r7d", t, j) > 0 and s.f("dv1h", t, j) > 2 * s.f("dv1h_avg", t, j),
                  exit=lambda s, t, j, ps, lead: s.f("r4h", t, j) < -0.01 or s.f("r24h", t, j) < 0))
    R[49] = (L, E(unit=1.0, max_units=1, lock="newleader", switch=False,
                  enter=lambda s, t, j: s.f("r7d", t, j) > 0 and s.f("r24h", t, j) > 0,
                  exit=lambda s, t, j, ps, lead: s.f("r24h", t, j) <= 0))
    R[86] = R[49]
    R[80] = (L, E(add="newhigh", switch=False, exit=lambda s, t, j, ps, lead: s.f("r24h", t, j) <= 0))
    R[87] = (L, E(uni="liquid", add="newhigh",
                  exit=lambda s, t, j, ps, lead: not lead or s.f("r24h", t, j) <= 0
                  or s.p["C"][t, j] < ps.peak * 0.97))
    APE = ["ADA", "AVAX", "BTC", "DOGE", "ETH", "HYPE", "LINK", "SOL", "XRP", "LTC", "BCH"]
    R[51] = (L, E(uni=APE, pick="best", unit=0.5, max_units=2, add="profit", add_profit=0.03, min_hold=DAY,
                  enter=lambda s, t, j: s.f("r7d", t, j) > 0 and s.f("r24h", t, j) > 0 and s.oi_up(t, j)
                  and s.f("r1h", t, j) < 0.03,
                  exit=lambda s, t, j, ps, lead: s.f("r24h", t, j) < 0 and s.upl(t, j) <= -0.015))
    R[61] = (L, E(uni=APE, pick="best", unit=0.5, max_units=2, add="profit", add_profit=0.03,
                  enter=lambda s, t, j: s.f("r1h", t, j) > 0 and s.f("r24h", t, j) > 0 and s.oi_up(t, j),
                  exit=lambda s, t, j, ps, lead: s.f("r1h", t, j) < 0 or (t - ps.t0 >= DAY and s.f("r24h", t, j) < 0)))
    R[68] = (L, E(uni=APE, pick="best", unit=0.5, max_units=2, add="profit", add_profit=0.045, min_hold=DAY,
                  enter=lambda s, t, j: s.f("r7d", t, j) >= 0.05 and s.f("r24h", t, j) > 0
                  and 0 <= s.f("r1h", t, j) <= 0.02 and s.oi_up(t, j),
                  exit=lambda s, t, j, ps, lead: s.upl(t, j) <= -0.02 or s.f("r24h", t, j) < 0))
    R[72] = (L, E(uni=APE, pick="best", unit=0.5, max_units=2, add="profit", add_profit=0.03, min_hold=DAY,
                  enter=lambda s, t, j: s.f("r7d", t, j) >= 0.03 and 0.01 <= s.f("r24h", t, j) <= 0.08
                  and 0 <= s.f("r1h", t, j) <= 0.015 and s.oi_up(t, j),
                  exit=lambda s, t, j, ps, lead: s.upl(t, j) <= -0.015 or s.f("r24h", t, j) < 0))
    R[77] = (L, E(uni=["LTC", "XRP"], unit=0.5, max_units=2, add="profit", add_profit=0.03, margin=0.03,
                  enter=lambda s, t, j: s.f("r24h", t, j) >= 0,
                  exit=lambda s, t, j, ps, lead: (t - ps.t0 >= DAY and not lead)
                  or (s.f("r7d", t, j) < 0 and s.f("r24h", t, j) < 0)))
    # E. range breakout + TP1/TP2
    R[6] = (RangeBreak, E())
    R[60] = (RangeBreak, E())
    R[83] = (RangeBreak, E())
    R[65] = (RangeBreak, E(uni=["HYPE"]))
    R[10] = (RangeBreak, E(rank="r7d", stop="level"))
    R[84] = (RangeBreak, E(need_trend=True))
    R[59] = (RangeBreak, E(uni=["BTC", "ETH", "HYPE", "SOL"], shorts=True, exit_fade=True, tp=None))
    R[29] = (RangeBreak, E(uni=["SOL"], stop="level", tp=None))
    # F. one daily breakout
    R[25] = (DailyBreak, E(coins=("BTC", "ETH", "SOL"), exit_rule="range"))
    R[26] = (DailyBreak, E(confirm_mom=True, exit_rule="stall"))
    R[32] = (DailyBreak, E(exit_rule="open"))
    R[54] = (DailyBreak, E(confirm_vol=True, exit_rule="stall"))
    R[57] = (DailyBreak, E(confirm_mom=True, exit_rule="open"))
    R[73] = (DailyBreak, E(orb=True, shorts=True, confirm_vol=True, exit_rule="range"))
    R[48] = (DailyBreak, E(coins=("SOL", "HYPE"), confirm_mom=True, exit_rule="cutR"))
    R[53] = (DailyBreak, E(confirm_mom=True, pick="tightest", exit_rule="cutR"))
    R[75] = (DailyBreak, E(coins=("SOL", "HYPE"), confirm_vol=True, window=(0, 8), exit_hour=14, exit_rule="none"))
    # G. fade pumps (short)
    R[7] = (Fade, E(variant="peak"))
    R[8] = (Fade, E(variant="dry"))
    # H. simple trend
    R[40] = (SimpleTrend, E(coins=("BTC",), kind="ema4h"))
    R[69] = (SimpleTrend, E(coins=("ADA",), kind="ema4h"))
    R[85] = (SimpleTrend, E(coins=("BTC",), kind="ema4h"))      # "your chosen coin" -> BTC
    R[45] = (SimpleTrend, E(coins=("BTC", "ETH"), kind="ema4h_50_200"))
    R[47] = (SimpleTrend, E(coins=None, kind="tf4h_30m", pick="r7d"))
    R[66] = (SimpleTrend, E(coins=("BTC", "ETH"), kind="tf15_1h", shorts=True))
    R[35] = (SimpleTrend, E(coins=("SOL",), kind="ema1h", shorts=True))
    R[52] = (SimpleTrend, E(coins=("BTC", "ETH"), kind="swing", shorts=True))
    R[39] = (SimpleTrend, E(coins=("BTC", "ETH"), kind="agree", shorts=True))
    R[38] = (SimpleTrend, E(coins=("BTC", "ETH"), kind="pullback", shorts=True))
    R[15] = (SimpleTrend, E(coins=("BTC", "ETH"), kind="higher_highs", shorts=True))
    R[33] = R[15]
    R[17] = (LinkAvax, E())
    R[76] = (WeeklyHigh, E())
    R[82] = (MemeMA, E())
    # I. Donchian
    R[21] = (Donchian, E(mode="drifter"))
    R[46] = (Donchian, E(mode="breezy"))
    R[74] = (Donchian, E(mode="volta"))
    # J. RSI(2)
    R[42] = (RSI2, E())
    R[81] = (RSI2, E(exit_rsi=False))
    R[43] = (RSI2, E())
    # K. scalp
    R[50] = (Scalp, E(coins=("BTC", "ETH", "SOL", "XRP"), cond="volspike", cut_after=1, hold_min=4))
    R[56] = (Scalp, E(coins=("AVAX", "BTC", "ETH", "LINK", "LTC", "SOL", "XRP"), cond="trigger", cut_after=1, hold_min=8))
    R[63] = (Scalp, E(cond="score1", cut_after=1, hold_min=3))
    # L/M/N/O/P/Q
    R[55] = (DipBuy, E())
    R[64] = (DCA, E())
    R[20] = (Meme, E(coins=("DOGE", "BONK", "PEPE", "WIF"), variant="sleuth"))
    R[58] = (Meme, E(coins=("BONK", "DOGE", "PEPE", "SHIB"), variant="flare"))
    R[79] = (Meme, E(coins=tuple(MEMES), variant="hunter"))
    R[70] = (Meme, E(coins=("SOL", "HYPE", "XRP", "DOGE"), variant="slurp"))
    R[67] = (Bizzy, E())
    R[23] = (Squeeze, E())
    R[88] = (BoozyPerp, E())
    return R


NOTES = {
    22: "50x on the site; run at 1x", 35: "10x on the site; run at 1x", 23: "OI -> volume proxy",
    28: "attn_z/OI -> volume proxy", 30: "OI -> volume proxy", 31: "attn_z/OI -> volume proxy",
    44: "OI -> volume proxy", 48: "OI/funding -> volume proxy", 51: "OI/attn -> volume proxy",
    53: "OI/funding -> volume proxy", 54: "volume confirm", 57: "OI -> volume proxy",
    61: "OI -> volume proxy; FLIP_SHORT ignored", 68: "OI/attn -> volume proxy", 72: "OI/attn -> volume proxy",
    74: "funding ignored", 50: "'score' -> 1h/4h/24h agreement", 56: "trigger = open + 0.5x prev day range",
    63: "'score' -> 1h/4h/24h agreement", 85: "coin unspecified -> BTC", 47: "'this list' -> all coins",
    20: "MOODENG/PENGU not on Alpaca", 58: "PUMP not on Alpaca", 70: "SUI not on Alpaca",
88: "liquid = top 15 by volume (Alpaca volumes are tiny vs OKX)",
}


def main():
    board = json.load(open(HERE / "hive_board.json"))
    groups: dict[str, list] = {}
    for bee in board:
        groups.setdefault((bee.get("instructions") or "").strip(), []).append(bee)
    rules = list(groups.items())

    p = load_panel()
    idx = p["index"]
    end = len(idx)
    start = idx.searchsorted(idx[-1] - pd.Timedelta(days=TEST_DAYS))
    print(f"Test window {idx[start]:%Y-%m-%d %H:%M} -> {idx[-1]:%Y-%m-%d %H:%M} UTC ({end - start} 15-min ticks)")
    for c in ("BTC", "ETH", "SOL"):
        j = p["cols"].index(c)
        print(f"  {c} buy & hold: {(p['C'][end - 1, j] / p['O'][start, j] - 1) * 100:+.2f}%")

    R = cfg()
    rows = []
    for i, (text, bees) in enumerate(rules, 1):
        names = ", ".join(sorted({b['name'] for b in bees}))
        week = [b["weekPct"] for b in bees if b["weekPct"] is not None]
        row = {"rule": i, "bees": names, "n_bees": len(bees), "board_pnl%": max(b["pnlPct"] for b in bees),
               "board_week%": week[0] if week else None, "note": NOTES.get(i, ""), "rule_text": text}
        if i not in R:
            row["note"] = "no rules shared" if not text else "untestable: uses the site's built-in strategy"
            rows.append(row)
            print(f"R{i:<3} {names[:28]:28} skipped ({row['note']})")
            continue
        cls, kw = R[i]
        for fee_name, fee in FEES:
            res = run(p, cls, kw, fee, start, end)
            tag = fee_name.split()[0]
            row.update({f"{tag} ret%": res["ret%"]})
            if fee_name.startswith("Alpaca"):
                row.update({"trades": res["trades"], "win%": res["win%"], "maxDD%": res["maxDD%"]})
        rows.append(row)
        print(f"R{i:<3} {names[:28]:28} board {row['board_pnl%']:+7.1f}% (wk {row['board_week%'] if row['board_week%'] is not None else float('nan'):+6.1f}) | "
              f"7d Alpaca {row['Alpaca ret%']:+6.2f}%  OKX {row['OKX ret%']:+6.2f}%  0fee {row['no ret%']:+6.2f}%  "
              f"trades {row['trades']:3}  DD {row['maxDD%']:4.1f}%", flush=True)
    pd.DataFrame(rows).to_csv(HERE / "results.csv", index=False)


if __name__ == "__main__":
    main()
