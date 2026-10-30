import sys
from datetime import datetime, timedelta, timezone
from loguru import logger
logger.remove()
import config
config.PROFIT_TARGETS_FILE = sys.argv[1]
from backtest import engine
from data import history_store
from trader.profit_targets import load_targets

END = datetime(2026, 10, 7, 8, tzinfo=timezone.utc)
START = END - timedelta(days=180)
syms = ["BTC/USD", "ETH/USD"]
data = {s: history_store.load(s, START - timedelta(days=60), END, "1Hour") for s in syms}
orig = engine._check_exit
live_targets = load_targets()

def run(label, tp_pct, trail_on, symbols):
    def check(trade, bar):
        hit = orig(trade, bar)
        if hit or tp_pct is None:
            return hit
        px = trade.entry * (1 + tp_pct / 100)
        if float(bar["high"]) >= px:
            return {"exit_price": px, "exit_reason": f"take-profit {tp_pct}%"}
        return None
    engine._check_exit = check
    targets = live_targets if trail_on else {k: {"arm_pct": 0} for k in ("BTCUSD", "ETHUSD")}
    trades, curve = engine.run_portfolio_backtest({s: data[s] for s in symbols}, 500.0, targets=targets, trade_from=START)
    engine._check_exit = orig
    pnl = sum(t.pnl_usd for t in trades)
    wins = [t for t in trades if t.pnl_usd > 0]; losses = [t for t in trades if t.pnl_usd <= 0]
    dd = float((1 - curve / curve.cummax()).max()) * 100 if len(curve) else 0
    reasons = {}
    for t in trades: reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    aw = sum(t.pnl_usd for t in wins) / len(wins) if wins else 0
    al = sum(t.pnl_usd for t in losses) / len(losses) if losses else 0
    print(f"{label:<34} trades={len(trades):>3} win%={100*len(wins)/max(len(trades),1):5.1f} "
          f"P&L=${pnl:+7.2f} ({pnl/5:+.2f}%) avgW=${aw:+.2f} avgL=${al:+.2f} maxDD={dd:4.1f}%  exits={reasons}")

print(f"Period {START:%Y-%m-%d} -> {END:%Y-%m-%d}, $500 start, live rules\n")
for symbols, name in ((["BTC/USD"], "BTC"), (["ETH/USD"], "ETH"), (syms, "BTC+ETH together")):
    print(f"== {name} ==")
    run("A current (trail, no TP)", None, True, symbols)
    run("B sell at +5% (trail still on)", 5, True, symbols)
    run("C sell at +5% only (no trail)", 5, False, symbols)
    print()
