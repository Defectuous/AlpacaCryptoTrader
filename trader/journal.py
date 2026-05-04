"""
Trade journal — CSV-backed log of every trade.

The journal is the single source of truth for:
  - How many trades have been placed today
  - The cumulative P&L for the day
  - Which symbols currently have open / pending positions
  - A written record of every entry for post-session review
"""
from __future__ import annotations

import csv
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
from loguru import logger

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
]


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------

def ensure_journal() -> None:
    """Create the journal file with header row if it does not exist.

    Also migrates an existing file to add any new columns introduced since it
    was first created — new columns are appended with empty values.
    """
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
    ensure_journal()

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
) -> None:
    """Update status, exit price, and P&L for a previously logged trade.

    If exit_price is provided but pnl_usd is not, compute realized P&L from
    the journal's entry_price and qty so the daily breaker has accurate data.
    """
    ensure_journal()
    try:
        df = pd.read_csv(JOURNAL_FILE, dtype=str)
        mask = df["order_id"] == order_id

        # Also match by exit_order_id so exit fills update the right row
        if not mask.any() and exit_order_id:
            mask = df["exit_order_id"] == exit_order_id

        if not mask.any():
            return

        df.loc[mask, "status"] = status
        if exit_order_id:
            df.loc[mask, "exit_order_id"] = exit_order_id
        if exit_price is not None:
            df.loc[mask, "exit_price"] = str(round(exit_price, 8))
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
# Query helpers
# ---------------------------------------------------------------------------

def _load_today(df: pd.DataFrame) -> pd.DataFrame:
    today = date.today().strftime("%Y-%m-%d")
    return df[df["date"] == today]


def get_today_stats() -> dict:
    """
    Return a dict with:
      trades_today : int   — number of trade rows for today
      daily_pnl    : float — sum of closed P&L for today (negative = loss)
    """
    ensure_journal()
    try:
        df = pd.read_csv(JOURNAL_FILE, dtype=str)
        if df.empty:
            return {"trades_today": 0, "daily_pnl": 0.0}

        today_df = _load_today(df)
        trades_today = len(today_df)

        closed = today_df[
            today_df["pnl_usd"].notna() & (today_df["pnl_usd"] != "")
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
    ensure_journal()
    open_statuses = {"new", "pending", "accepted", "partially_filled", "held", "filled"}
    try:
        df = pd.read_csv(JOURNAL_FILE, dtype=str)
        if df.empty:
            return []
        today_df = _load_today(df)
        open_today = today_df[today_df["status"].str.lower().isin(open_statuses)]
        return open_today["symbol"].unique().tolist()
    except Exception as exc:
        logger.error(f"Error querying open trades: {exc}")
        return []
