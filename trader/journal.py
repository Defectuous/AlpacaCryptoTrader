"""
Trade journal — log of every trade, in MySQL / MariaDB when DB_HOST is set
(see trader/journal_db.py), otherwise in logs/trade_journal.csv.

The journal is the single source of truth for:
  - How many trades have been placed today
  - The cumulative P&L for the day
  - Which symbols currently have open / pending positions
  - A written record of every entry for post-session review
"""
from __future__ import annotations

import csv
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pandas as pd
from loguru import logger

import config

if config.DB_HOST:
    from trader import journal_db as _db
else:
    _db = None

# ---------------------------------------------------------------------------
# File paths
# ---------------------------------------------------------------------------
JOURNAL_DIR  = Path("logs")
JOURNAL_FILE = JOURNAL_DIR / "trade_journal.csv"

COLUMNS = [
    "date",
    "time_utc",
    "symbol",
    "order_id",
    "entry_group_id",
    "exit_order_id",
    "side",
    "entry_price",
    "stop_price",
    "target_price",
    "qty",
    "notional_usd",
    "risk_usd",
    "reward_usd",
    "rr_ratio",
    "regime",
    "risk_profile",
    "status",
    "exit_price",
    "pnl_usd",
    "reason",
    "closed_at",   # UTC time the exit was booked
]


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------

def ensure_journal() -> None:
    """Create the journal file with header row if it does not exist.

    Also migrates an existing file to add any new columns introduced since it
    was first created — new columns are appended with empty values.
    """
    if _db:
        _db.ensure()
        return
    JOURNAL_DIR.mkdir(exist_ok=True)
    if not JOURNAL_FILE.exists():
        with JOURNAL_FILE.open("w", newline="") as fh:
            csv.DictWriter(fh, fieldnames=COLUMNS).writeheader()
        logger.info(f"Journal created → {JOURNAL_FILE.resolve()}")
        return

    # Migration: add any missing columns to existing file
    try:
        df = pd.read_csv(JOURNAL_FILE, dtype=str)
        missing = [c for c in COLUMNS if c not in df.columns]
        if missing:
            for col in missing:
                df[col] = ""
            # Reorder to match canonical column list
            df = df.reindex(columns=COLUMNS, fill_value="")
            df.to_csv(JOURNAL_FILE, index=False)
            logger.info(f"Journal migrated — added columns: {missing}")
    except Exception as exc:
        logger.warning(f"Journal migration check failed: {exc}")


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------

def log_trade(order_info: dict) -> None:
    """
    Append a new trade row to the journal.

    *order_info* is the dict returned by order_manager.place_order().
    """

    entry  = float(order_info.get("entry",  0))
    stop   = float(order_info.get("stop",   0))
    target = float(order_info.get("target", 0))
    qty    = float(order_info.get("qty",    0))

    risk_usd   = abs(entry - stop)   * qty
    reward_usd = abs(target - entry) * qty
    rr_ratio   = reward_usd / risk_usd if risk_usd > 0 else 0.0

    now = datetime.now(timezone.utc)

    row = {
        "date":          now.strftime("%Y-%m-%d"),
        "time_utc":      now.strftime("%H:%M:%S"),
        "symbol":        order_info.get("symbol", ""),
        "order_id":      order_info.get("order_id", ""),
        "entry_group_id": order_info.get("entry_group_id", ""),
        "exit_order_id": "",
        "side":          order_info.get("side", "BUY"),
        "entry_price":   round(entry,  8),
        "stop_price":    round(stop,   8),
        "target_price":  round(target, 8),
        "qty":           round(qty,    8),
        "notional_usd":  round(entry * qty, 4),
        "risk_usd":      round(risk_usd,    4),
        "reward_usd":    round(reward_usd,  4),
        "rr_ratio":      round(rr_ratio,    2),
        "regime":        order_info.get("regime", ""),
        "risk_profile":  order_info.get("risk_profile", ""),
        "status":        order_info.get("status", "pending"),
        "exit_price":    "",
        "pnl_usd":       "",
        "reason":        str(order_info.get("reason", ""))[:300],
    }

    if _db:
        _db.insert_trade(row)
    else:
        ensure_journal()
        with JOURNAL_FILE.open("a", newline="") as fh:
            csv.DictWriter(fh, fieldnames=COLUMNS).writerow(row)

    logger.info(
        f"Journal ▶ {row['symbol']} logged | "
        f"risk=${risk_usd:.4f} R:R={rr_ratio:.2f}"
    )


def update_trade(
    order_id: str,
    status: str,
    exit_price: float | None = None,
    pnl_usd: float | None = None,
    exit_order_id: str | None = None,
    symbol: str | None = None,
) -> None:
    """Update status, exit price, and P&L for a previously logged trade.

    If exit_price is provided but pnl_usd is not, compute realized P&L from
    the journal's entry_price and qty so the daily breaker has accurate data.
    """
    if _db:
        result = _db.update_trade(
            order_id=order_id, status=status, exit_price=exit_price, pnl_usd=pnl_usd,
            exit_order_id=exit_order_id, symbol=symbol,
        )
        if result and result[0]:
            logger.info(f"Journal updated — order {order_id} → status={status} pnl={result[1]}")
        return
    ensure_journal()
    try:
        df = pd.read_csv(JOURNAL_FILE, dtype=str)
        mask = df["order_id"] == order_id

        # Also match by exit_order_id so exit fills update the right row
        if not mask.any() and exit_order_id:
            mask = df["exit_order_id"] == exit_order_id

        if not mask.any() and symbol and exit_price is not None:
            open_statuses = {"new", "pending", "accepted", "partially_filled", "held", "filled"}
            mask = (
                (df["symbol"] == symbol)
                & df["status"].str.lower().isin(open_statuses)
                & (df["exit_price"].fillna("") == "")
            )

        if not mask.any():
            return

        df.loc[mask, "status"] = status
        if exit_order_id:
            df.loc[mask, "exit_order_id"] = exit_order_id
        if exit_price is not None:
            df.loc[mask, "exit_price"] = str(round(exit_price, 8))
            df.loc[mask, "closed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            # Compute P&L when not supplied externally
            if pnl_usd is None:
                try:
                    row = df[mask].iloc[0]
                    entry_p = float(row["entry_price"])
                    qty_val = float(row["qty"])
                    side = str(row.get("side", "LONG")).upper()
                    if side == "SHORT":
                        pnl_usd = (entry_p - exit_price) * qty_val
                    else:
                        pnl_usd = (exit_price - entry_p) * qty_val
                except Exception:
                    pass
        if pnl_usd is not None:
            df.loc[mask, "pnl_usd"] = str(round(pnl_usd, 4))

        df.to_csv(JOURNAL_FILE, index=False)
        logger.info(f"Journal updated — order {order_id} → status={status} pnl={pnl_usd}")
    except Exception as exc:
        logger.error(f"Journal update failed for {order_id}: {exc}")


# ---------------------------------------------------------------------------
# Order ledger (MySQL / MariaDB only)
# ---------------------------------------------------------------------------

def tracks_orders() -> bool:
    """True when every broker order is mirrored and trade exits come from real fills."""
    return _db is not None


def _utc_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.strftime("%Y-%m-%d %H:%M:%S.%f")


def _order_row(order) -> dict:
    return {
        "id": order.id,
        "client_order_id": order.client_order_id or None,
        "symbol": order.symbol,
        "side": order.side,
        "order_type": order.order_type or None,
        "role": order.role or "other",
        "qty": order.qty,
        "filled_qty": order.filled_qty,
        "filled_avg_price": order.filled_avg_price,
        "limit_price": order.limit_price,
        "stop_price": order.stop_price,
        "status": order.status,
        "submitted_at": _utc_text(order.submitted_at),
        "filled_at": _utc_text(order.filled_at),
        "updated_at": _utc_text(order.updated_at),
        "seen_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }


def record_orders(orders: list) -> None:
    """Mirror broker orders into the orders table and refresh the trades they belong to."""
    if _db:
        _db.record_orders([_order_row(o) for o in orders])


def note_exit(order, reason: str) -> None:
    """Record that the bot closed a position with *order*, and why (e.g. "profit_trail")."""
    if _db and order is not None:
        _db.note_exit(_order_row(order), reason)


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def today_bounds() -> tuple[datetime, datetime]:
    """Today's local calendar day as naive UTC [start, end), to compare with stored UTC times."""
    start, end = (
        datetime.combine(day, time.min).astimezone().astimezone(timezone.utc).replace(tzinfo=None)
        for day in (date.today(), date.today() + timedelta(days=1))
    )
    return start, end


def _in_today(stamps: pd.Series) -> pd.Series:
    start, end = today_bounds()
    times = pd.to_datetime(stamps, errors="coerce")
    return (times >= start) & (times < end)


def _load_today(df: pd.DataFrame) -> pd.DataFrame:
    """Rows opened today; `date` and `time_utc` are UTC, today is the local day."""
    return df[_in_today(df["date"].fillna("") + " " + df["time_utc"].fillna(""))]


def get_today_stats() -> dict:
    """
    Return a dict with:
      trades_today : int   — number of trades opened today
      daily_pnl    : float — P&L of trades closed today, whenever they opened (negative = loss)
    """
    if _db:
        return _db.today_stats(*today_bounds())
    ensure_journal()
    try:
        df = pd.read_csv(JOURNAL_FILE, dtype=str)
        if df.empty:
            return {"trades_today": 0, "daily_pnl": 0.0}

        today_df = _load_today(df)
        if "entry_group_id" in today_df.columns:
            groups = today_df["entry_group_id"].fillna("").astype(str)
            grouped_trades = groups[groups != ""].nunique()
            ungrouped_trades = int((groups == "").sum())
            trades_today = grouped_trades + ungrouped_trades
        else:
            trades_today = len(today_df)

        closed = df[
            _in_today(df["closed_at"]) & df["pnl_usd"].notna() & (df["pnl_usd"] != "")
        ]
        daily_pnl = (
            closed["pnl_usd"].astype(float).sum() if not closed.empty else 0.0
        )
        return {"trades_today": trades_today, "daily_pnl": daily_pnl}

    except Exception as exc:
        logger.error(f"Error reading journal stats: {exc}")
        return {"trades_today": 0, "daily_pnl": 0.0}


def get_open_trade_symbols() -> list[str]:
    """
    Return a list of symbols that have open/pending trades today.
    Used to prevent opening a second position in the same coin.
    """
    if _db:
        return _db.open_trade_symbols(*today_bounds())
    ensure_journal()
    open_statuses = {"new", "pending", "accepted", "partially_filled", "held", "filled"}
    try:
        df = pd.read_csv(JOURNAL_FILE, dtype=str)
        if df.empty:
            return []
        today_df = _load_today(df)
        open_today = today_df[today_df["status"].str.lower().isin(open_statuses)]
        open_today = open_today[open_today["exit_price"].fillna("") == ""]
        return open_today["symbol"].unique().tolist()
    except Exception as exc:
        logger.error(f"Error querying open trades: {exc}")
        return []


def get_open_trade(symbol: str) -> dict | None:
    """Return the latest unclosed journal trade for a symbol.

    Keys: order_id, side ("LONG"/"SHORT") and opened_at (UTC datetime or None),
    so callers can tell a real exit fill from an unrelated older order.
    """
    if _db:
        return _db.open_trade(symbol)
    ensure_journal()
    open_statuses = {"new", "pending", "accepted", "partially_filled", "held", "filled"}
    try:
        df = pd.read_csv(JOURNAL_FILE, dtype=str)
        if df.empty:
            return None
        open_rows = df[
            (df["symbol"] == symbol)
            & df["status"].str.lower().isin(open_statuses)
            & (df["exit_price"].fillna("") == "")
        ]
        if open_rows.empty:
            return None
        row = open_rows.iloc[-1]
        opened_at = pd.to_datetime(f"{row['date']} {row['time_utc']}", utc=True, errors="coerce")
        return {
            "order_id": str(row["order_id"]),
            "side": str(row.get("side", "LONG")).upper(),
            "opened_at": None if pd.isna(opened_at) else opened_at.to_pydatetime(),
        }
    except Exception as exc:
        logger.error(f"Error querying open trade for {symbol}: {exc}")
        return None
