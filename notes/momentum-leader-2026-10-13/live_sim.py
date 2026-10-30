"""
Live paper-simulation of the momentum-leader rule for a fixed time (no orders are placed).

Every tick: rank all cached coins by trailing 7-day dollar volume, keep the top 5, score each by
N-day momentum (latest mid / hourly close N days ago - 1), and apply the rule:
  - flat and leader has momentum > 0 -> buy 1 unit (1/3 of equity) of the leader
  - holding the leader -> add a unit (max 3)
  - leader changed -> sell all, buy the new leader
  - held coin's momentum <= 0 -> sell all
Fills at the live ask (buy) / bid (sell) plus 0.25% Alpaca taker fee. Runs 7d and 3d books side by side.

Uses the BACKTEST key (never the bot's). Run from repo root:
  PYTHONPATH=. python notes/momentum-leader-2026-10-13/live_sim.py --minutes 60 --tick 5
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest, CryptoLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame

from data import history_store

FEE, START, UNITS, TOP_N = 0.0025, 500.0, 3, 5


class Book:
    def __init__(self, name, hours):
        self.name, self.hours = name, hours
        self.cash, self.sym, self.qty, self.units, self.fees, self.trades = START, None, 0.0, 0, 0.0, 0

    def equity(self, q):
        return self.cash + (self.qty * q[self.sym]["bid"] if self.sym else 0)

    def buy(self, sym, q, eq):
        usd = min(eq / UNITS, self.cash)
        if usd < 1:
            return
        px = q[sym]["ask"]
        self.qty += usd / (1 + FEE) / px
        self.fees += usd - usd / (1 + FEE)
        self.cash -= usd
        self.sym, self.units = sym, self.units + 1

    def sell(self, q):
        gross = self.qty * q[self.sym]["bid"]
        self.cash += gross * (1 - FEE)
        self.fees += gross * FEE
        self.sym, self.qty, self.units, self.trades = None, 0.0, 0, self.trades + 1


def snapshot(client, symbols):
    now = datetime.now(timezone.utc)
    bars = client.get_crypto_bars(CryptoBarsRequest(
        symbol_or_symbols=symbols, timeframe=TimeFrame.Hour, start=now - timedelta(days=8, hours=2), end=now)).df
    quotes = client.get_crypto_latest_quote(CryptoLatestQuoteRequest(symbol_or_symbols=symbols))
    q = {s: {"bid": float(v.bid_price), "ask": float(v.ask_price)} for s, v in quotes.items()
         if v.bid_price and v.ask_price}
    close = bars["close"].unstack(level=0)
    dollar = (bars["close"] * bars["volume"]).unstack(level=0).fillna(0)
    close.index = pd.to_datetime(close.index, utc=True)
    dollar.index = close.index
    vol7 = dollar[dollar.index >= now - timedelta(days=7)].sum().sort_values(ascending=False)
    top = [s for s in vol7.index if s in q][:TOP_N]
    mom = {}
    for h in (168, 72):
        ref_t = now - timedelta(hours=h)
        ref = close[close.index <= ref_t].ffill().iloc[-1]
        mom[h] = {s: ((q[s]["bid"] + q[s]["ask"]) / 2) / ref[s] - 1 for s in top if pd.notna(ref.get(s))}
    return now, top, q, mom


def step(b: Book, mom, q, log):
    m = mom[b.hours]
    leader = max(m, key=m.get) if m else None
    eq = b.equity(q)
    act = "hold"
    if b.sym and (b.sym not in m or m[b.sym] <= 0 or leader != b.sym):
        why = "lost momentum" if b.sym in m and m[b.sym] <= 0 else f"overtaken by {leader}"
        act = f"SELL {b.sym} ({why})"
        b.sell(q)
        eq = b.equity(q)
    if leader and m[leader] > 0:
        if b.sym is None:
            b.buy(leader, q, eq)
            act = f"BUY {leader}" if act == "hold" else f"{act} + BUY {leader}"
        elif b.units < UNITS:
            b.buy(leader, q, eq)
            act = f"ADD {leader} (unit {b.units})"
    log(f"  [{b.name}] leader {leader} {m.get(leader, 0)*100:+.2f}% | {act} | "
        f"equity ${b.equity(q):.2f}  fees ${b.fees:.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=60)
    ap.add_argument("--tick", type=float, default=5)
    a = ap.parse_args()
    key, secret = history_store._keys()
    client = CryptoHistoricalDataClient(api_key=key, secret_key=secret)
    symbols = history_store.cached_symbols()
    books = [Book("7d", 168), Book("3d", 72)]
    log = lambda s: print(s, flush=True)
    end = time.time() + a.minutes * 60
    while True:
        try:
            now, top, q, mom = snapshot(client, symbols)
        except Exception as exc:
            log(f"{datetime.now():%H:%M:%S} data error: {exc}")
        else:
            log(f"{now:%H:%M:%S} UTC  top5: " + ", ".join(
                f"{s.split('/')[0]} 7d {mom[168].get(s, float('nan'))*100:+.1f}% 3d {mom[72].get(s, float('nan'))*100:+.1f}%"
                for s in top))
            for b in books:
                step(b, mom, q, log)
        if time.time() + a.tick * 60 > end + 1:
            break
        time.sleep(a.tick * 60)
    log("FINAL (open positions valued at bid, exit fee not charged):")
    for b in books:
        log(f"  {b.name}: equity ${b.equity(q):.2f} ({(b.equity(q)/START-1)*100:+.2f}%), "
            f"holding {b.sym or 'cash'} x{b.units}, round trips {b.trades}, fees ${b.fees:.2f}")


if __name__ == "__main__":
    main()
