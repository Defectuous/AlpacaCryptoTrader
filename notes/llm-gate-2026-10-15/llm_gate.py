"""
LLM-gated momentum chase (2026-10-15).

Same rules as notes/momentum-chase-2026-10-13/chase.py (chase the hardest mover,
pyramid into winners, bail when momentum stalls), long-only because Alpaca can't
short. Before every entry, the local Ollama model is asked whether to take the
trade. Rules-only vs LLM-gated runs are compared over the last 30 / 60 / 90 days.

The prompt hides the coin name and the date so the model can't recall what
happened next. It only gets prices, volume and market context up to the
signal bar. Answers are cached by (bar, coin, config) in cache.json, so reruns
and overlapping windows don't ask the same question twice.

Run from the repo root:  PYTHONPATH=. python notes/llm-gate-2026-10-15/llm_gate.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import requests

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent / "momentum-chase-2026-10-13"))
import chase  # noqa: E402  (load_panel, Params, FEE, SLIP, START_EQUITY)

OLLAMA = os.getenv("OLLAMA_URL", "http://192.168.1.151:11434")
MODEL = os.getenv("OLLAMA_MODEL", "hf.co/ConwayResearch/Underdog-Saluki-27B-1.0:Underdog-Saluki-27B-1.0-IQ2-mix.gguf")
CACHE_PATH = HERE / "cache.json"
WINDOWS = (30, 60, 90)

CONFIGS = {
    "strict": chase.Params(lookback=3, thresh=0.08, exit_rule="bar", pyramid=True, shorts=False),
    "loose": chase.Params(lookback=6, thresh=0.05, exit_rule="bar", pyramid=True, shorts=False),
}

SYSTEM = (
    "You are a risk filter for a short-term crypto momentum strategy on a spot exchange (long only). "
    "The strategy buys the coin with the strongest recent move, adds 25% more equity each time price "
    "runs another +2% (up to 4 units), and sells everything on the first hourly bar that closes lower. "
    "Each round trip costs about 0.6% in fees and slippage, so the move must keep going for at least a "
    "few more hours to be worth it. Most of these signals lose money; approve only setups that look "
    "likely to continue. Answer with JSON only."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "take": {"type": "boolean"},
        "confidence": {"type": "integer"},
        "reason": {"type": "string"},
    },
    "required": ["take", "confidence", "reason"],
}


def _pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def build_prompt(panel: dict, t: int, j: int, p: chase.Params) -> str:
    close, vol = panel["close"], panel["volume"].fillna(0)
    c = close.iloc[:, j].values
    v = vol.iloc[:, j].values
    last24 = c[t - 23:t + 1]
    series = ", ".join(f"{x / last24[0] * 100:.1f}" for x in last24)
    notional = v * c
    avg = notional[t - 167:t + 1].mean() or 1.0
    vol12 = ", ".join(f"{x / avg:.1f}" for x in notional[t - 11:t + 1])
    hi7 = np.nanmax(c[t - 167:t + 1])

    btc = close["BTC/USD"].values
    mom = close.iloc[t] / close.iloc[t - p.lookback] - 1
    breadth = float((mom > 0).mean())
    qualifying = int((mom >= p.thresh).sum())

    return (
        f"Signal: the coin is up {_pct(c[t] / c[t - p.lookback] - 1)} over the last {p.lookback} hours, "
        f"the biggest move among {close.shape[1]} coins ({qualifying} coins cleared the {p.thresh:.0%} bar).\n"
        f"Coin returns: 1h {_pct(c[t] / c[t - 1] - 1)}, 24h {_pct(c[t] / c[t - 24] - 1)}, "
        f"7d {_pct(c[t] / c[t - 168] - 1)}; price is {_pct(c[t] / hi7 - 1)} from its 7-day high.\n"
        f"Last 24 hourly closes, indexed to 100 (oldest first): {series}\n"
        f"Last 12 hours of dollar volume vs the 7-day hourly average (oldest first): {vol12}\n"
        f"Market: BTC {p.lookback}h {_pct(btc[t] / btc[t - p.lookback] - 1)}, BTC 24h {_pct(btc[t] / btc[t - 24] - 1)}; "
        f"{breadth:.0%} of coins are up over the last {p.lookback}h.\n"
        'Should the strategy buy now? Reply {"take": true|false, "confidence": 0-100, "reason": "<one sentence>"}.'
    )


class Gate:
    def __init__(self, panel: dict):
        self.panel = panel
        self.cache: dict = json.loads(CACHE_PATH.read_text()) if CACHE_PATH.exists() else {}
        self.calls = 0
        self.seconds = 0.0

    def ask(self, name: str, p: chase.Params, t: int, j: int) -> dict:
        key = f"{name}|{self.panel['close'].index[t].isoformat()}|{self.panel['close'].columns[j]}"
        if key in self.cache:
            return self.cache[key]
        prompt = build_prompt(self.panel, t, j, p)
        t0 = time.time()
        for attempt in range(3):
            try:
                r = requests.post(f"{OLLAMA}/api/chat", timeout=300, json={
                    "model": MODEL, "stream": False, "think": False, "format": SCHEMA,
                    "options": {"temperature": 0, "num_ctx": 4096},
                    "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                })
                r.raise_for_status()
                ans = json.loads(r.json()["message"]["content"])
                ans = {"take": bool(ans["take"]), "confidence": int(ans.get("confidence", 0)),
                       "reason": str(ans.get("reason", ""))[:300]}
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 2:
                    raise
                print(f"  retry after {e!r}", file=sys.stderr)
                time.sleep(5)
        self.seconds += time.time() - t0
        self.calls += 1
        self.cache[key] = ans
        if self.calls % 10 == 0:
            self.save()
            print(f"  {self.calls} LLM calls, {self.seconds / self.calls:.1f}s avg", file=sys.stderr)
        return ans

    def save(self):
        CACHE_PATH.write_text(json.dumps(self.cache, indent=0))


@dataclass
class Result:
    ret: float
    trades: int
    win: float
    max_dd: float
    fees: float
    asked: int
    approved: int


def run(panel: dict, p: chase.Params, lo: int, hi: int, gate=None, name: str = "") -> Result:
    """chase.run, long-only, with an optional LLM veto on each new entry."""
    close, open_ = panel["close"], panel["open"]
    mom = close / close.shift(p.lookback) - 1
    bar_ret = close / close.shift(1) - 1
    ok = panel["traded"] & panel["liquid"] & mom.notna()
    C, O, M, R, OK = close.values, open_.values, mom.values, bar_ret.values, ok.values

    cash, qty, sym, units, last_add = chase.START_EQUITY, 0.0, None, 0, 0.0
    peak, max_dd, trades, pnl, entry_eq, fees = chase.START_EQUITY, 0.0, 0, [], 0.0, 0.0
    asked = approved = 0
    pending = None

    def fill(j, q, px_open):
        nonlocal cash, qty, fees
        px = px_open * (1 + chase.SLIP * np.sign(q))
        fee = abs(q * px) * chase.FEE
        cash -= q * px + fee
        qty += q
        fees += fee

    for t in range(lo, hi):
        if pending is not None:
            kind, j = pending
            pending = None
            if kind == "exit":
                fill(sym, -qty, O[t, sym])
                pnl.append(cash - entry_eq)
                qty, sym, units = 0.0, None, 0
            elif kind == "enter":
                sym, units, entry_eq = j, 1, cash
                fill(j, p.unit * cash / O[t, j], O[t, j])
                last_add = O[t, j]
                trades += 1
            elif kind == "add":
                eq = cash + qty * O[t, sym]
                fill(sym, p.unit * eq / O[t, sym], O[t, sym])
                units += 1
                last_add = O[t, sym]

        equity = cash + (qty * C[t, sym] if sym is not None else 0.0)
        peak = max(peak, equity)
        max_dd = max(max_dd, 1 - equity / peak)

        if sym is not None:
            m = M[t, sym]
            stalled = R[t, sym] <= 0 if p.exit_rule == "bar" else m < p.thresh / 2
            if stalled or np.isnan(m):
                pending = ("exit", None)
            elif p.pyramid and units < p.max_units and C[t, sym] / last_add - 1 >= p.add_step:
                pending = ("add", None)
            continue

        row = np.where(OK[t] & (M[t] > 0), M[t], np.nan)
        if np.all(np.isnan(row)):
            continue
        j = int(np.nanargmax(row))
        if row[j] < p.thresh:
            continue
        if gate is not None:
            asked += 1
            if not gate.ask(name, p, t, j)["take"]:
                continue
            approved += 1
        pending = ("enter", j)

    if sym is not None:
        fill(sym, -qty, C[hi - 1, sym])
        pnl.append(cash - entry_eq)
    wins = sum(x > 0 for x in pnl)
    return Result((cash / chase.START_EQUITY - 1) * 100, trades, 100 * wins / len(pnl) if pnl else 0.0,
                  max_dd * 100, fees, asked, approved)


def standalone_trade(panel: dict, p: chase.Params, t: int, j: int) -> float:
    """% return of one trade entered at bar t+1's open on coin j, run to its exit (incl. costs)."""
    C, O = panel["close"].values, panel["open"].values
    n = len(C)
    if t + 1 >= n:
        return np.nan
    cash, qty = 1.0, 0.0
    cost = chase.FEE + chase.SLIP

    def buy(frac_eq, px):
        nonlocal cash, qty
        spend = frac_eq * (cash + qty * px)
        qty += spend / (px * (1 + chase.SLIP))
        cash -= spend * (1 + chase.FEE)

    buy(p.unit, O[t + 1, j])
    last_add, units = O[t + 1, j], 1
    mom_den = panel["close"].iloc[:, j].shift(p.lookback).values
    for k in range(t + 1, n - 1):
        stalled = C[k, j] <= C[k - 1, j] if p.exit_rule == "bar" else C[k, j] / mom_den[k] - 1 < p.thresh / 2
        if stalled:
            cash += qty * O[k + 1, j] * (1 - cost)
            return (cash - 1) * 100
        if p.pyramid and units < p.max_units and C[k, j] / last_add - 1 >= p.add_step:
            buy(p.unit, O[k + 1, j])
            last_add, units = O[k + 1, j], units + 1
    cash += qty * C[-1, j] * (1 - cost)
    return (cash - 1) * 100


def main():
    panel = chase.load_panel()
    idx = panel["close"].index
    end = len(idx)
    gate = Gate(panel)
    print(f"Data to {idx[-1]:%Y-%m-%d %H:%M} UTC, {panel['close'].shape[1]} coins, model {MODEL}")

    rows = []
    try:
        for days in sorted(WINDOWS, reverse=True):  # 90 first so 60/30 hit the cache
            lo = idx.searchsorted(idx[-1] - pd.Timedelta(days=days))
            bench = chase.bench(panel, start=idx[lo])
            for name, p in CONFIGS.items():
                base = run(panel, p, lo, end)
                gated = run(panel, p, lo, end, gate=gate, name=name)
                for label, r in (("rules only", base), ("LLM gated", gated)):
                    rows.append({"window": f"{days}d", "config": name, "mode": label, "ret%": r.ret,
                                 "trades": r.trades, "win%": r.win, "maxDD%": r.max_dd, "fees$": r.fees,
                                 "asked": r.asked, "approved": r.approved,
                                 "BTC hold%": bench["BTC hold %"], "basket%": bench["33-coin basket %"]})
                print(f"{days}d {name}: rules {base.ret:+.1f}% ({base.trades} tr) | "
                      f"LLM {gated.ret:+.1f}% ({gated.trades} tr, approved {gated.approved}/{gated.asked})")
    finally:
        gate.save()

    df = pd.DataFrame(rows)
    df.to_csv(HERE / "results.csv", index=False)
    pd.set_option("display.width", 220)
    print(df.round(1).to_string(index=False))

    # How good is the model as a classifier? Score every question it was asked by
    # what that trade would have returned on its own, regardless of portfolio path.
    recs = []
    cols = {c: i for i, c in enumerate(panel["close"].columns)}
    pos = {ts: i for i, ts in enumerate(idx)}
    for key, ans in gate.cache.items():
        name, ts, coin = key.split("|")
        t, j = pos.get(pd.Timestamp(ts)), cols.get(coin)
        if t is None or j is None or name not in CONFIGS:
            continue
        recs.append({"config": name, "ts": ts, "coin": coin, **ans,
                     "trade%": standalone_trade(panel, CONFIGS[name], t, j)})
    q = pd.DataFrame(recs)
    q.to_csv(HERE / "decisions.csv", index=False)
    print("\nEvery signal the model judged, scored by the trade's own result (after costs):")
    print(q.groupby(["config", "take"])["trade%"].agg(["count", "mean", "median", lambda s: (s > 0).mean() * 100])
          .rename(columns={"<lambda_0>": "win%"}).round(2).to_string())
    q["conf_bin"] = pd.cut(q["confidence"], [-1, 50, 70, 80, 90, 100])
    print(q.groupby(["config", "conf_bin"], observed=True)["trade%"].agg(["count", "mean"]).round(2).to_string())
    print(f"\n{gate.calls} new LLM calls this run, {gate.seconds / max(gate.calls, 1):.1f}s avg")


if __name__ == "__main__":
    main()
