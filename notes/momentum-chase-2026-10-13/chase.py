"""
Momentum-chase backtest (2026-10-13).

Rules as described by the user:
  * Every hour, find the coin moving hardest: biggest |return| over LOOKBACK hours.
  * Strongly up (>= THRESH) -> long; strongly down (<= -THRESH) -> short.
  * Pyramid: add another unit each time price runs ADD_STEP further in our favor.
  * Bail the instant momentum stalls or flips, then jump to the next hot mover.

One position at a time. Signals use completed hourly closes; fills happen at the
next bar's open with fee + slippage against us. Shorts are simulated as spot
borrow with no borrow cost (optimistic; Alpaca can't short crypto at all).

Run from the repo root:  PYTHONPATH=. python notes/momentum-chase-2026-10-13/chase.py
"""
from __future__ import annotations

import itertools
import sys
from dataclasses import dataclass

import numpy as np
import pandas as pd
from loguru import logger

from data import history_store

logger.remove()
logger.add(sys.stderr, level="WARNING")

FEE = 0.0025          # Alpaca crypto taker, per side
SLIP = 0.0005         # extra slippage per side
START_EQUITY = 500.0
MIN_NOTIONAL_24H = 50.0   # bot's MIN_LIQUIDITY_VOLUME_USD: avg $/hour over 24h


@dataclass(frozen=True)
class Params:
    lookback: int          # hours of momentum
    thresh: float          # |return| needed to call it "strong"
    exit_rule: str         # "bar" = last hourly bar closes against us; "stall" = momentum < thresh/2
    pyramid: bool          # add units as it runs
    shorts: bool
    add_step: float = 0.02
    unit: float = 0.25     # fraction of equity per unit
    max_units: int = 4


def _ts(x) -> pd.Timestamp:
    x = pd.Timestamp(x)
    return x if x.tzinfo else x.tz_localize("UTC")


def load_panel() -> dict[str, pd.DataFrame]:
    frames = {s: history_store.load(s) for s in history_store.cached_symbols()}
    index = sorted(set().union(*(f.index for f in frames.values())))
    index = pd.DatetimeIndex(index)
    panel = {}
    for field in ("open", "close", "volume"):
        panel[field] = pd.DataFrame({s: f[field] for s, f in frames.items()}).reindex(index)
    panel["traded"] = panel["volume"].fillna(0) > 0
    panel["close"] = panel["close"].ffill()
    panel["open"] = panel["open"].fillna(panel["close"].shift(1))
    notional = (panel["volume"].fillna(0) * panel["close"]).rolling(24, min_periods=1).mean()
    panel["liquid"] = notional >= MIN_NOTIONAL_24H
    return panel


def run(panel: dict, p: Params, start=None, end=None) -> dict:
    close, open_ = panel["close"], panel["open"]
    mom = close / close.shift(p.lookback) - 1
    bar_ret = close / close.shift(1) - 1
    ok = panel["traded"] & panel["liquid"] & mom.notna()

    idx = close.index
    lo = idx.searchsorted(_ts(start)) if start else p.lookback + 1
    hi = idx.searchsorted(_ts(end)) if end else len(idx) - 1
    lo = max(lo, p.lookback + 1)

    C, O, M, R, OK = close.values, open_.values, mom.values, bar_ret.values, ok.values
    cols = list(close.columns)

    cash, qty, sym, side = START_EQUITY, 0.0, None, 0
    units, last_add = 0, 0.0
    peak, max_dd = START_EQUITY, 0.0
    trades, trade_pnl, entry_equity, fees_paid = 0, [], 0.0, 0.0
    pending = None  # action decided on bar t, filled at open of t+1

    def fill(j, q, price_open):
        nonlocal cash, qty, fees_paid
        px = price_open * (1 + SLIP * np.sign(q))
        cost = q * px
        fee = abs(cost) * FEE
        cash -= cost + fee
        qty += q
        fees_paid += fee

    for t in range(lo, hi):
        # 1) execute what was decided at the previous close
        if pending is not None:
            kind, j, s = pending
            pending = None
            if kind == "exit":
                fill(sym, -qty, O[t, sym])
                trade_pnl.append(cash - entry_equity)
                qty, sym, side, units = 0.0, None, 0, 0
            elif kind == "enter":
                eq = cash
                sym, side, units = j, s, 1
                entry_equity = eq
                fill(j, s * p.unit * eq / O[t, j], O[t, j])
                last_add = O[t, j]
                trades += 1
            elif kind == "add":
                eq = cash + qty * O[t, sym]
                fill(sym, side * p.unit * eq / O[t, sym], O[t, sym])
                units += 1
                last_add = O[t, sym]

        equity = cash + (qty * C[t, sym] if sym is not None else 0.0)
        peak = max(peak, equity)
        max_dd = max(max_dd, 1 - equity / peak)
        if equity <= 0:
            break

        # 2) decide at this close
        if sym is not None:
            m = M[t, sym]
            stalled = (
                side * R[t, sym] <= 0 if p.exit_rule == "bar"
                else side * m < p.thresh / 2
            )
            if stalled or np.isnan(m):
                pending = ("exit", None, None)
            elif p.pyramid and units < p.max_units and side * (C[t, sym] / last_add - 1) >= p.add_step:
                pending = ("add", None, None)
            continue

        row = np.where(OK[t], M[t], np.nan)
        if not p.shorts:
            row = np.where(row > 0, row, np.nan)
        if np.all(np.isnan(row)):
            continue
        j = int(np.nanargmax(np.abs(row)))
        if abs(row[j]) >= p.thresh:
            pending = ("enter", j, 1 if row[j] > 0 else -1)

    if sym is not None:  # mark to the last close, pay the exit fee
        fill(sym, -qty, C[hi - 1, sym])
        trade_pnl.append(cash - entry_equity)
    wins = [x for x in trade_pnl if x > 0]
    return {
        "final": cash,
        "ret%": (cash / START_EQUITY - 1) * 100,
        "trades": trades,
        "win%": 100 * len(wins) / len(trade_pnl) if trade_pnl else 0.0,
        "maxDD%": max_dd * 100,
        "fees$": fees_paid,
    }


def bench(panel, start=None, end=None) -> dict:
    c = panel["close"]
    c = c[(c.index >= _ts(start)) if start else slice(None)]
    if end:
        c = c[c.index < _ts(end)]
    btc = c["BTC/USD"].dropna()
    basket = (c.iloc[-1] / c.bfill().iloc[0]).mean()
    return {"BTC hold %": (btc.iloc[-1] / btc.iloc[0] - 1) * 100, "33-coin basket %": (basket - 1) * 100}


GRID = {
    "lookback": [3, 6, 12, 24],
    "thresh": [0.03, 0.05, 0.08],
    "exit_rule": ["bar", "stall"],
    "pyramid": [True, False],
    "shorts": [True, False],
}


def main():
    panel = load_panel()
    idx = panel["close"].index
    mid = idx[len(idx) // 2]
    print(f"Data: {idx[0]:%Y-%m-%d} -> {idx[-1]:%Y-%m-%d %H:%M} UTC, {panel['close'].shape[1]} coins; "
          f"halves split at {mid:%Y-%m-%d}")
    print("Benchmarks full:", {k: round(v, 1) for k, v in bench(panel).items()})
    print("Benchmarks H1:  ", {k: round(v, 1) for k, v in bench(panel, end=mid).items()})
    print("Benchmarks H2:  ", {k: round(v, 1) for k, v in bench(panel, start=mid).items()})

    rows = []
    for combo in itertools.product(*GRID.values()):
        p = Params(**dict(zip(GRID, combo)))
        full = run(panel, p)
        h1 = run(panel, p, end=mid)
        h2 = run(panel, p, start=mid)
        rows.append({**dict(zip(GRID, combo)), **full, "H1 ret%": h1["ret%"], "H2 ret%": h2["ret%"]})
    df = pd.DataFrame(rows)
    df.to_csv("notes/momentum-chase-2026-10-13/grid.csv", index=False)
    pd.set_option("display.width", 200)
    pd.set_option("display.max_rows", 200)
    fmt = df.round({"ret%": 1, "win%": 0, "maxDD%": 1, "fees$": 0, "final": 2, "H1 ret%": 1, "H2 ret%": 1})
    print(fmt.sort_values("ret%", ascending=False).to_string(index=False))
    print("\nprofitable configs:", int((df["ret%"] > 0).sum()), "of", len(df),
          "| both halves profitable:", int(((df["H1 ret%"] > 0) & (df["H2 ret%"] > 0)).sum()))
    print("median ret% by group:")
    for k in ("shorts", "pyramid", "exit_rule", "lookback", "thresh"):
        print(" ", k, df.groupby(k)["ret%"].median().round(1).to_dict())


if __name__ == "__main__":
    main()
