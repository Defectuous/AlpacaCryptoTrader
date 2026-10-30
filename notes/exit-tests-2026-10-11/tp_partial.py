import sys, inspect
from datetime import datetime, timedelta, timezone
from loguru import logger
logger.remove()
import config
config.PROFIT_TARGETS_FILE = sys.argv[1]
from backtest import engine
from data import history_store
from trader.profit_targets import load_targets

src = inspect.getsource(engine.run_portfolio_backtest)
def sub(old, new):
    global src
    assert src.count(old) == 1, old
    src = src.replace(old, new)
sub("def run_portfolio_backtest(", "def run_partial_backtest(")
sub("    trail_best: dict[str, float] = {}\n",
    "    trail_best: dict[str, float] = {}\n    halfed: set = set()\n    partial_pnl: dict = {}\n")
sub("        trade.pnl_usd = proceeds * (1 - fee) - cost_basis.pop(symbol)\n",
    "        trade.pnl_usd = proceeds * (1 - fee) - cost_basis.pop(symbol) + partial_pnl.pop(symbol, 0.0)\n"
    "        pnl_today += partial_pnl_today_fix(symbol)\n        halfed.discard(symbol)\n")
sub("                arm_pct, trail_pct = trail_settings(symbol, targets)\n",
    "                arm_pct, trail_pct = trail_settings(symbol, targets)\n"
    "                if PARTIAL and symbol not in halfed and float(bar['high']) >= trade.entry * (1 + arm_pct / 100):\n"
    "                    px = trade.entry * (1 + arm_pct / 100)\n"
    "                    part = trade.qty * PARTIAL\n"
    "                    proceeds = part * px * (1 - half_spread)\n"
    "                    basis = cost_basis[symbol] * PARTIAL\n"
    "                    cash += proceeds * (1 - fee)\n"
    "                    cost_basis[symbol] -= basis\n"
    "                    pnl_today += proceeds * (1 - fee) - basis\n"
    "                    partial_pnl[symbol] = proceeds * (1 - fee) - basis\n"
    "                    trade.qty -= part\n"
    "                    halfed.add(symbol)\n"
    "                    if BREAKEVEN:\n"
    "                        trade.stop = max(trade.stop, trade.entry)\n")
ns = dict(vars(engine)); ns["PARTIAL"] = 0.5; ns["BREAKEVEN"] = False
ns["partial_pnl_today_fix"] = lambda s: 0.0
exec(src, ns)

END = datetime(2026, 10, 7, 8, tzinfo=timezone.utc)
START = END - timedelta(days=180)
data = {"BTC/USD": history_store.load("BTC/USD", START - timedelta(days=60), END, "1Hour")}
targets = load_targets(); base = dict(targets["BTCUSD"])
print(f"BTC {START:%Y-%m-%d} -> {END:%Y-%m-%d}, $500 start\n")
def show(label, trades, curve):
    pnl = sum(t.pnl_usd for t in trades); w = [t for t in trades if t.pnl_usd > 0]; l = [t for t in trades if t.pnl_usd <= 0]
    dd = float((1 - curve / curve.cummax()).max()) * 100
    print(f"{label:<44} trades={len(trades)} win%={100*len(w)/len(trades):5.1f} P&L=${pnl:+6.2f} "
          f"avgW=${sum(t.pnl_usd for t in w)/max(len(w),1):+.2f} avgL=${sum(t.pnl_usd for t in l)/max(len(l),1):+.2f} maxDD={dd:.1f}%")
    return trades
for tr in (1.5, 0.75):
    targets["BTCUSD"] = {**base, "trail_pct": tr}
    ns["PARTIAL"] = 0.0
    show(f"full position, trail {tr}%", *ns["run_partial_backtest"](data, 500.0, targets=targets, trade_from=START))
for be in (False, True):
    for tr in (1.5, 1.0, 0.75):
        targets["BTCUSD"] = {**base, "trail_pct": tr}
        ns["PARTIAL"] = 0.5; ns["BREAKEVEN"] = be
        t = show(f"sell 50% @2.64, trail rest {tr}%{' +BE stop' if be else ''}", *ns["run_partial_backtest"](data, 500.0, targets=targets, trade_from=START))
