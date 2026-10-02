"""
AlpacaCryptoTrader — Calibrate per-coin profit-trail settings from price history.

For each coin, over the last --days of hourly bars:

  arm_pct    The median best gain reached within --horizon-days of a random
             hour (so price got there about half the time), rounded to 0.5
             and kept between 3 % (clears fees) and 25 %.
  trail_pct  3 x the median hourly ATR % (a pullback normal hourly noise
             rarely makes), rounded to 0.5, at least 1 % and at most half of
             arm_pct, so an armed trail keeps at least half the arm gain.

Writes profit_targets.json (PROFIT_TARGETS_FILE). Entries marked
"locked": true are kept as they are, so hand-tuned values survive a rerun.

Usage
-----
    python calibrate_profit_targets.py                      # SYMBOLS from .env
    python calibrate_profit_targets.py --symbols AAVE/USD BTC/USD
    python calibrate_profit_targets.py --symbols all --days 365
    python calibrate_profit_targets.py --dry-run            # print, don't write
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

import pandas as pd
from loguru import logger

import config
from brokers import resolve_symbols
from data.market_data import get_bars_history
from trader.indicators import calculate_atr
from trader.profit_targets import load_targets, save_targets

MIN_ARM_PCT = 3.0
MAX_ARM_PCT = 25.0
MIN_TRAIL_PCT = 1.0
TRAIL_ATR_MULTIPLE = 3.0


def _round_half(value: float) -> float:
    return round(value * 2) / 2


def calibrate(symbol: str, days: int, horizon_days: int) -> dict | None:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    bars = get_bars_history(symbol, start - timedelta(days=5), end, "1Hour")
    atr_pct = (calculate_atr(bars, 14) / bars["close"] * 100).loc[start:]
    bars = bars.loc[start:]
    horizon = horizon_days * 24
    if len(bars) < horizon * 2:
        logger.warning(f"{symbol}: Only {len(bars)} hourly bars; skipping")
        return None

    # Best high over the next `horizon` bars, as a gain on each bar's close.
    future_high = bars["high"][::-1].rolling(horizon, min_periods=horizon).max()[::-1].shift(-1)
    best_gain = (future_high / bars["close"] - 1).dropna() * 100
    median_gain = float(best_gain.median())
    median_atr = float(atr_pct.median())

    arm = min(max(_round_half(median_gain), MIN_ARM_PCT), MAX_ARM_PCT)
    trail = min(max(_round_half(median_atr * TRAIL_ATR_MULTIPLE), MIN_TRAIL_PCT), arm / 2)
    return {
        "arm_pct": arm,
        "trail_pct": trail,
        "median_best_gain_pct": round(median_gain, 2),
        "hourly_atr_pct": round(median_atr, 2),
        "reached_arm_pct_of_hours": round(float((best_gain >= arm).mean() * 100), 1),
        "calibrated": f"{end:%Y-%m-%d} ({days}d hourly, {horizon_days}d horizon)",
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Calibrate per-coin profit-trail settings from price history",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--symbols", nargs="+", default=None, metavar="SYM",
        help='Symbols to calibrate, or "all" for every broker coin (default: SYMBOLS from .env)',
    )
    parser.add_argument("--days", type=int, default=180, help="Days of history (default: 180)")
    parser.add_argument(
        "--horizon-days", type=int, default=7,
        help="How far ahead to look for the best gain (default: 7)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print results without writing")
    args = parser.parse_args()
    if args.symbols and [s.lower() for s in args.symbols] == ["all"]:
        config.TRADE_ALL_SYMBOLS = True
        config.SYMBOLS.clear()
        args.symbols = None
    args.symbols = list(resolve_symbols()) if args.symbols is None else [s.upper() for s in args.symbols]
    return args


def main() -> None:
    args = _parse_args()
    # load_targets() keys are compact (AAVEUSD); write back readable AAVE/USD keys.
    by_name = {f"{k[:-3]}/USD": v for k, v in load_targets().items()}

    print(f"\n{'Symbol':<12}{'Arm %':>7}{'Trail %':>9}{'Keeps ≥':>9}{'Med best':>10}{'ATR/h':>8}{'Hit %':>7}")
    for symbol in args.symbols:
        existing = by_name.get(symbol, {})
        if existing.get("locked"):
            print(f"{symbol:<12}{existing.get('arm_pct', '-'):>7}{existing.get('trail_pct', '-'):>9}   locked")
            continue
        try:
            result = calibrate(symbol, args.days, args.horizon_days)
        except Exception as exc:
            logger.error(f"{symbol}: Calibration failed: {exc}")
            continue
        if result is None:
            continue
        by_name[symbol] = result
        print(
            f"{symbol:<12}{result['arm_pct']:>7g}{result['trail_pct']:>9g}"
            f"{result['arm_pct'] - result['trail_pct']:>8g}%"
            f"{result['median_best_gain_pct']:>9.1f}%{result['hourly_atr_pct']:>7.2f}%"
            f"{result['reached_arm_pct_of_hours']:>6.0f}%"
        )
    print(
        "\nArm % = gain that starts the trail; Trail % = pullback from the best that sells;"
        "\nKeeps ≥ = gain still kept if it sells right after arming; Hit % = share of hours"
        f"\nthat reached the arm gain within {args.horizon_days} days."
    )
    if args.dry_run:
        print("\n--dry-run: nothing written")
        return
    save_targets(dict(sorted(by_name.items())))
    print(f"\nWrote {config.PROFIT_TARGETS_FILE}")


if __name__ == "__main__":
    main()
