"""
Backtest results analysis and reporting.

Computes aggregate statistics from a list of BacktestTrade objects and
outputs a human-readable console report plus an optional CSV trade log.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Sequence

from loguru import logger

from backtest.engine import BacktestTrade


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def compute_stats(trades: list[BacktestTrade], initial_equity: float) -> dict:
    """
    Compute aggregate performance statistics from a list of completed trades.

    End-of-data trades are included in equity curve / drawdown but excluded
    from win-rate and profit-factor (they are not clean exits).
    """
    if not trades:
        return {}

    # Only count trades with a definitive exit for win/loss stats
    clean = [t for t in trades if t.exit_reason not in ("end-of-data",)]
    total = len(clean)
    wins   = [t for t in clean if t.is_win]
    losses = [t for t in clean if not t.is_win]

    gross_profit  = sum(t.pnl_usd for t in wins)
    gross_loss    = abs(sum(t.pnl_usd for t in losses))
    total_pnl     = sum(t.pnl_usd for t in trades)  # includes end-of-data

    win_rate      = len(wins) / total if total > 0 else 0.0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # Max drawdown from equity curve
    equity_curve = [initial_equity] + [t.equity_after for t in trades]
    peak   = initial_equity
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    r_multiples = [t.r_multiple for t in clean if abs(t.r_multiple) > 0]
    avg_r = sum(r_multiples) / len(r_multiples) if r_multiples else 0.0

    longs  = [t for t in clean if t.side == "long"]
    shorts = [t for t in clean if t.side == "short"]

    exit_breakdown: dict[str, int] = {}
    for t in trades:
        exit_breakdown[t.exit_reason] = exit_breakdown.get(t.exit_reason, 0) + 1

    return {
        "total_trades":    total,
        "wins":            len(wins),
        "losses":          len(losses),
        "win_rate":        win_rate,
        "gross_profit":    gross_profit,
        "gross_loss":      gross_loss,
        "total_pnl":       total_pnl,
        "total_pnl_pct":   total_pnl / initial_equity * 100,
        "profit_factor":   profit_factor,
        "max_drawdown":    max_dd,
        "avg_r_multiple":  avg_r,
        "long_trades":     len(longs),
        "short_trades":    len(shorts),
        "long_wins":       len([t for t in longs  if t.is_win]),
        "short_wins":      len([t for t in shorts if t.is_win]),
        "exit_breakdown":  exit_breakdown,
    }


# ---------------------------------------------------------------------------
# Console report
# ---------------------------------------------------------------------------

def print_report(symbol: str, stats: dict, initial_equity: float) -> None:
    """Print a formatted backtest report to stdout."""
    if not stats:
        logger.warning(f"{symbol}: No completed trades to report")
        return

    sep   = "=" * 60
    total = stats["total_trades"]
    wins  = stats["wins"]
    loss  = stats["losses"]
    wr    = stats["win_rate"] * 100
    pnl   = stats["total_pnl"]
    pnl_p = stats["total_pnl_pct"]
    pf    = stats["profit_factor"]
    dd    = stats["max_drawdown"] * 100
    ar    = stats["avg_r_multiple"]
    end_eq = initial_equity + stats["total_pnl"]

    print(f"\n{sep}")
    print(f"  BACKTEST RESULTS — {symbol}")
    print(sep)
    print(f"  Trades (clean exits) : {total}  ({wins}W / {loss}L)")
    print(f"  Win rate             : {wr:.1f}%")
    print(f"  Profit factor        : {pf:.2f}")
    print(f"  Total PnL            : ${pnl:+.2f}  ({pnl_p:+.2f}%)")
    print(f"  Gross profit         : ${stats['gross_profit']:.2f}")
    print(f"  Gross loss           : ${stats['gross_loss']:.2f}")
    print(f"  Max drawdown         : {dd:.1f}%")
    print(f"  Avg R-multiple       : {ar:.2f}R")

    if stats["long_trades"] > 0:
        lwr = stats["long_wins"] / stats["long_trades"] * 100
        print(f"  Long  trades         : {stats['long_trades']}  ({lwr:.0f}% WR)")
    if stats["short_trades"] > 0:
        swr = stats["short_wins"] / stats["short_trades"] * 100
        print(f"  Short trades         : {stats['short_trades']}  ({swr:.0f}% WR)")

    # Exit reason breakdown
    breakdown = stats.get("exit_breakdown", {})
    if breakdown:
        bd_str = "  |  ".join(f"{k}: {v}" for k, v in sorted(breakdown.items()))
        print(f"  Exit breakdown       : {bd_str}")

    print(f"  Starting equity      : ${initial_equity:.2f}")
    print(f"  Ending equity        : ${end_eq:.2f}")
    print(f"{sep}\n")


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

def save_trades_csv(trades: list[BacktestTrade], path: Path) -> None:
    """Write all backtest trades to a CSV file."""
    if not trades:
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "symbol", "side", "entry_time", "exit_time",
        "entry", "stop", "target", "qty",
        "exit_price", "exit_reason", "pnl_usd", "r_multiple",
        "regime", "risk_profile", "equity_after",
    ]

    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for t in trades:
            writer.writerow({
                "symbol":       t.symbol,
                "side":         t.side,
                "entry_time":   t.entry_time,
                "exit_time":    t.exit_time,
                "entry":        round(t.entry, 8),
                "stop":         round(t.stop, 8),
                "target":       round(t.target, 8),
                "qty":          round(t.qty, 8),
                "exit_price":   round(t.exit_price, 8) if t.exit_price is not None else "",
                "exit_reason":  t.exit_reason,
                "pnl_usd":      round(t.pnl_usd, 4),
                "r_multiple":   round(t.r_multiple, 2),
                "regime":       t.regime,
                "risk_profile": t.risk_profile,
                "equity_after": round(t.equity_after, 2),
            })

    logger.info(f"Trades saved → {path.resolve()}")
