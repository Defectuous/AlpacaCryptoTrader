"""
AlpacaCryptoTrader — Show open trades, working orders and closed trades.

Reads the journal database (DB_HOST must be set), which the bot keeps in step
with the broker on every order update. Unrealized P&L uses a live quote when
the broker is reachable and is left blank when it is not.

Usage
-----
    python trades_report.py               # open trades, working orders, last 7 days closed
    python trades_report.py --days 30     # closed trades from the last 30 days
    python trades_report.py --days 0      # every closed trade
    python trades_report.py --offline     # skip live quotes
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

import config
from trader import journal_db

_DONE_IN = ", ".join(["%s"] * len(journal_db.DONE_STATUSES))


def _local(value: datetime | None) -> str:
    """UTC database time as local 'MM-DD HH:MM'."""
    if value is None:
        return "-"
    return value.replace(tzinfo=timezone.utc).astimezone().strftime("%m-%d %H:%M")


def _num(value, digits: int = 8) -> str:
    return "-" if value is None else f"{float(value):.{digits}g}"


def _money(value) -> str:
    return "-" if value is None else f"{float(value):+.2f}"


def _quotes(symbols: list[str]) -> dict[str, float]:
    """Bid per symbol, or {} when the broker cannot be reached."""
    if not symbols:
        return {}
    try:
        from brokers import get_broker
        broker = get_broker()
        return {s: broker.get_latest_quote(s)["bid"] for s in symbols}
    except Exception as exc:
        print(f"(live quotes unavailable: {exc})")
        return {}


def _open_trades(cur, offline: bool) -> None:
    cur.execute(
        f"""SELECT t.`symbol`, t.`order_id`, t.`date`, t.`time_utc`, t.`entry_fill_price`,
                   t.`entry_fill_qty`, t.`entry_price`, t.`qty`, t.`stop_price`, t.`current_stop`
            FROM `{journal_db.TABLE}` t
            WHERE t.`exit_price` IS NULL AND LOWER(t.`status`) IN ({journal_db._OPEN_IN})
            ORDER BY t.`id`""",
        journal_db.OPEN_STATUSES,
    )
    rows = cur.fetchall()
    print(f"\nOPEN TRADES ({len(rows)})")
    if not rows:
        return
    bids = {} if offline else _quotes(sorted({r[0] for r in rows}))
    print(f"  {'Symbol':<11}{'Opened':<13}{'Entry':>13}{'Qty':>15}{'Stop':>13}{'Bid':>13}{'Unreal $':>10}{'%':>8}")
    for symbol, _, day, clock, fill, fill_qty, planned, planned_qty, entry_stop, stop in rows:
        opened = datetime.combine(day, (datetime.min + clock).time())
        entry = float(fill if fill is not None else planned)
        qty = float(fill_qty if fill_qty is not None else planned_qty)
        bid = bids.get(symbol)
        unreal = pct = None
        if bid:
            unreal = (bid - entry) * qty
            pct = (bid / entry - 1) * 100
        stop_text = _num(stop) if stop is not None else f"NONE ({_num(entry_stop)})"
        print(
            f"  {symbol:<11}{_local(opened):<13}{_num(entry):>13}{_num(qty):>15}{stop_text:>13}"
            f"{_num(bid):>13}{_money(unreal):>10}{'-' if pct is None else f'{pct:+.2f}':>8}"
        )
    print("  Stop NONE = no working protective stop (planned stop in brackets).")


def _working_orders(cur) -> None:
    cur.execute(
        f"""SELECT `symbol`, `role`, `side`, `order_type`, `qty`, `filled_qty`, `stop_price`,
                   `limit_price`, `status`, `submitted_at`
            FROM `{journal_db.ORDERS}` WHERE `status` NOT IN ({_DONE_IN})
            ORDER BY `symbol`, `submitted_at`""",
        journal_db.DONE_STATUSES,
    )
    rows = cur.fetchall()
    print(f"\nWORKING ORDERS ({len(rows)})")
    if not rows:
        return
    print(f"  {'Symbol':<11}{'Role':<7}{'Side':<6}{'Type':<12}{'Qty':>15}{'Filled':>13}{'Stop':>13}{'Limit':>13}  {'Status':<17}Placed")
    for symbol, role, side, kind, qty, filled, stop, limit, status, submitted in rows:
        print(
            f"  {symbol:<11}{role:<7}{side:<6}{kind or '-':<12}{_num(qty):>15}{_num(filled):>13}"
            f"{_num(stop):>13}{_num(limit):>13}  {status:<17}{_local(submitted)}"
        )


def _closed_trades(cur, days: int) -> None:
    where = "t.`exit_price` IS NOT NULL"
    args: tuple = ()
    if days > 0:
        where += " AND COALESCE(t.`closed_at`, t.`date`) >= %s"
        args = ((datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S"),)
    cur.execute(
        f"""SELECT t.`symbol`, t.`date`, t.`time_utc`, t.`closed_at`,
                   COALESCE(t.`entry_fill_price`, t.`entry_price`), t.`exit_price`,
                   COALESCE(t.`exit_qty`, t.`qty`), t.`exit_reason`, t.`fees_usd`, t.`pnl_usd`
            FROM `{journal_db.TABLE}` t WHERE {where}
            ORDER BY COALESCE(t.`closed_at`, t.`date`), t.`id`""",
        args,
    )
    rows = cur.fetchall()
    span = "all time" if days <= 0 else f"last {days} days"
    print(f"\nCLOSED TRADES ({len(rows)}, {span})")
    if not rows:
        return
    print(f"  {'Symbol':<11}{'Opened':<13}{'Closed':<13}{'Entry':>13}{'Exit':>13}{'Qty':>15}{'%':>8}  {'Exit by':<14}{'Fees':>7}{'Net $':>9}")
    wins = total = fees_total = 0.0
    for symbol, day, clock, closed, entry, exit_price, qty, reason, fees, pnl in rows:
        opened = datetime.combine(day, (datetime.min + clock).time())
        pct = (float(exit_price) / float(entry) - 1) * 100 if entry else 0.0
        total += float(pnl or 0)
        fees_total += float(fees or 0)
        wins += float(pnl or 0) > 0
        print(
            f"  {symbol:<11}{_local(opened):<13}{_local(closed):<13}{_num(entry):>13}{_num(exit_price):>13}"
            f"{_num(qty):>15}{pct:>+8.2f}  {reason or '-':<14}{_num(fees, 3):>7}{_money(pnl):>9}"
        )
    print(f"  {int(wins)} of {len(rows)} profitable | est. fees ${fees_total:.2f} | net ${total:+.2f}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Show open trades, working orders and closed trades")
    parser.add_argument("--days", type=int, default=7, help="Closed trades from the last N days; 0 = all (default: 7)")
    parser.add_argument("--offline", action="store_true", help="Skip live quotes")
    args = parser.parse_args()
    if not config.DB_HOST:
        raise SystemExit("trades_report.py needs the MySQL / MariaDB journal (set DB_HOST in .env)")
    with journal_db._cursor() as cur:
        _open_trades(cur, args.offline)
        _working_orders(cur)
        _closed_trades(cur, args.days)
    print(f"\nTimes are local. Fees are estimated at {config.TRADE_FEE_PCT:g} % per fill and included in Net $.")


if __name__ == "__main__":
    main()
