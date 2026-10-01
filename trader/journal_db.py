"""
MySQL / MariaDB backend for the trade journal, used when DB_HOST is set.

Same columns as the CSV journal, one row per trade. Every write is a
transaction, so a crash or power cut never leaves a half-written journal.

If the database cannot be reached, writes are appended to
logs/journal_spool.jsonl and replayed in order on the next successful
connection, so no trade record is lost. Reads log the error and return the
same empty defaults the CSV journal does.

On first connection the table is created, and an existing
logs/trade_journal.csv is imported (then renamed) when the table is empty.

Export to CSV:  python -m trader.journal_db export [path]
"""
from __future__ import annotations

import csv
import json
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

import pymysql
from loguru import logger

import config

TABLE = "trades"
LOG_DIR = Path("logs")
CSV_FILE = LOG_DIR / "trade_journal.csv"
SPOOL_FILE = LOG_DIR / "journal_spool.jsonl"
BAD_SPOOL_FILE = LOG_DIR / "journal_spool.failed.jsonl"

OPEN_STATUSES = ("new", "pending", "accepted", "partially_filled", "held", "filled")
_OPEN_IN = ", ".join(["%s"] * len(OPEN_STATUSES))

# Server errors worth retrying: too many connections, access denied, shutting
# down, lock wait timeout, deadlock. Codes 2000+ are client-side connection errors.
_RETRY_CODES = {1040, 1045, 1053, 1205, 1213}

# Writes and spool replay come from the stream thread and the main loop.
_lock = threading.RLock()
_ready = False


def _schema() -> str:
    return f"""
    CREATE TABLE IF NOT EXISTS `{TABLE}` (
        `id`             BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
        `date`           DATE           NOT NULL,
        `time_utc`       TIME           NOT NULL,
        `symbol`         VARCHAR(32)    NOT NULL,
        `order_id`       VARCHAR(64)    NOT NULL,
        `entry_group_id` VARCHAR(64)    NULL,
        `exit_order_id`  VARCHAR(64)    NULL,
        `side`           VARCHAR(8)     NOT NULL,
        `entry_price`    DECIMAL(28,12) NULL,
        `stop_price`     DECIMAL(28,12) NULL,
        `target_price`   DECIMAL(28,12) NULL,
        `qty`            DECIMAL(28,10) NULL,
        `notional_usd`   DECIMAL(18,4)  NULL,
        `risk_usd`       DECIMAL(18,4)  NULL,
        `reward_usd`     DECIMAL(18,4)  NULL,
        `rr_ratio`       DECIMAL(10,2)  NULL,
        `regime`         VARCHAR(32)    NULL,
        `risk_profile`   VARCHAR(16)    NULL,
        `status`         VARCHAR(24)    NOT NULL,
        `exit_price`     DECIMAL(28,12) NULL,
        `pnl_usd`        DECIMAL(18,4)  NULL,
        `closed_at`      DATETIME       NULL,     -- UTC time the exit fill was booked
        `reason`         TEXT           NULL,
        KEY `ix_order_id` (`order_id`),
        KEY `ix_exit_order_id` (`exit_order_id`),
        KEY `ix_symbol_status` (`symbol`, `status`),
        KEY `ix_date` (`date`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """


def _unreachable(exc: BaseException) -> bool:
    """True when *exc* means "try again later"; spooled writes wait for it to clear.

    PyMySQL raises OperationalError for bad data too (e.g. 1292 invalid date),
    so classify by error code: retrying a bad row would block the spool forever.
    """
    if isinstance(exc, (pymysql.err.InterfaceError, OSError)):
        return True
    if isinstance(exc, pymysql.err.OperationalError):
        code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else 0
        return code >= 2000 or code in _RETRY_CODES
    return False


def _connect() -> pymysql.connections.Connection:
    return pymysql.connect(
        host=config.DB_HOST,
        port=config.DB_PORT,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        database=config.DB_NAME,
        charset="utf8mb4",
        autocommit=False,
        connect_timeout=5,
        read_timeout=15,
        write_timeout=15,
    )


@contextmanager
def _cursor() -> Iterator[pymysql.cursors.Cursor]:
    """One connection per call: cheap on localhost and safe across threads."""
    conn = _connect()
    try:
        _prepare(conn)
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _prepare(conn: pymysql.connections.Connection) -> None:
    """Create the table and import the CSV once, then flush any spooled writes."""
    global _ready
    with _lock:
        if not _ready:
            with conn.cursor() as cur:
                cur.execute(_schema())
                cur.execute(f"SELECT COUNT(*) FROM `{TABLE}`")
                if cur.fetchone()[0] == 0:
                    _import_csv(cur)
            conn.commit()
            _ready = True
        if SPOOL_FILE.exists():
            _replay_spool(conn)


# ---------------------------------------------------------------------------
# CSV import and spool
# ---------------------------------------------------------------------------

def _import_csv(cur: pymysql.cursors.Cursor) -> None:
    if not CSV_FILE.exists():
        return
    with CSV_FILE.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    for row in rows:
        _insert(cur, row)
    imported = CSV_FILE.with_name(
        f"{CSV_FILE.name}.imported-{datetime.now(timezone.utc):%Y%m%d%H%M%S}"
    )
    CSV_FILE.rename(imported)
    logger.info(f"Journal: imported {len(rows)} trade(s) from {CSV_FILE} (renamed to {imported.name})")


def _spool(op: str, args: dict) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    with SPOOL_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"op": op, "args": args}, default=str) + "\n")


def _replay_spool(conn: pymysql.connections.Connection) -> None:
    """Apply spooled writes in order; a line the database rejects is set aside."""
    lines = SPOOL_FILE.read_text(encoding="utf-8").splitlines()
    applied = 0
    for n, line in enumerate(lines):
        if not line.strip():
            continue
        entry = json.loads(line)
        try:
            with conn.cursor() as cur:
                _OPS[entry["op"]](cur, **entry["args"])
            conn.commit()
            applied += 1
        except Exception as exc:
            conn.rollback()
            if _unreachable(exc):
                # Keep the unapplied tail for the next attempt.
                SPOOL_FILE.write_text("\n".join(lines[n:]) + "\n", encoding="utf-8")
                raise
            logger.error(f"Journal: spooled {entry['op']} rejected ({exc}); moved to {BAD_SPOOL_FILE}")
            with BAD_SPOOL_FILE.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    SPOOL_FILE.unlink()
    logger.info(f"Journal: replayed {applied} spooled write(s)")


def _write(op: str, args: dict) -> Any:
    with _lock:
        try:
            with _cursor() as cur:
                return _OPS[op](cur, **args)
        except Exception as exc:
            if not _unreachable(exc):
                logger.error(f"Journal {op} failed: {exc}")
                return None
            _spool(op, args)
            logger.error(f"Journal database unreachable ({exc}); {op} spooled to {SPOOL_FILE}")
    return None


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def _value(value: Any) -> Any:
    return None if value == "" else value


def _insert(cur: pymysql.cursors.Cursor, row: dict) -> None:
    columns = ", ".join(f"`{c}`" for c in row)
    marks = ", ".join(["%s"] * len(row))
    cur.execute(f"INSERT INTO `{TABLE}` ({columns}) VALUES ({marks})", [_value(v) for v in row.values()])


def _update(
    cur: pymysql.cursors.Cursor,
    order_id: str,
    status: str,
    exit_price: float | None = None,
    pnl_usd: float | None = None,
    exit_order_id: str | None = None,
    symbol: str | None = None,
    closed_at: str | None = None,
) -> tuple[int, float | None]:
    """Same matching as the CSV journal: order id, then exit order id, then open trade in symbol."""
    select = f"SELECT `id`, `entry_price`, `qty`, `side` FROM `{TABLE}` WHERE {{}} ORDER BY `id` FOR UPDATE"
    cur.execute(select.format("`order_id` = %s"), (order_id,))
    rows = cur.fetchall()
    if not rows and exit_order_id:
        cur.execute(select.format("`exit_order_id` = %s"), (exit_order_id,))
        rows = cur.fetchall()
    if not rows and symbol and exit_price is not None:
        cur.execute(
            select.format(f"`symbol` = %s AND LOWER(`status`) IN ({_OPEN_IN}) AND `exit_price` IS NULL"),
            (symbol, *OPEN_STATUSES),
        )
        rows = cur.fetchall()
    if not rows:
        return 0, None

    sets: dict[str, Any] = {"status": status}
    if exit_order_id:
        sets["exit_order_id"] = exit_order_id
    if exit_price is not None:
        sets["exit_price"] = round(exit_price, 8)
        if closed_at:
            sets["closed_at"] = closed_at
        _, entry, qty, side = rows[0]
        if pnl_usd is None and entry is not None and qty is not None:
            move = float(entry) - exit_price if str(side).upper() == "SHORT" else exit_price - float(entry)
            pnl_usd = move * float(qty)
    if pnl_usd is not None:
        sets["pnl_usd"] = round(pnl_usd, 4)

    ids = [r[0] for r in rows]
    assignments = ", ".join(f"`{c}` = %s" for c in sets)
    cur.execute(
        f"UPDATE `{TABLE}` SET {assignments} WHERE `id` IN ({', '.join(['%s'] * len(ids))})",
        [*sets.values(), *ids],
    )
    return len(ids), pnl_usd


_OPS = {"insert": _insert, "update": _update}


def ensure() -> None:
    """Connect once at startup so the table exists and the CSV is imported."""
    try:
        with _cursor():
            pass
        logger.info(f"Journal: MySQL {config.DB_USER}@{config.DB_HOST}/{config.DB_NAME}.{TABLE}")
    except Exception as exc:
        logger.error(f"Journal database unavailable at startup ({exc}); writes will be spooled")


def insert_trade(row: dict) -> None:
    _write("insert", {"row": row})


def update_trade(**kwargs: Any) -> tuple[int, float | None] | None:
    """(rows updated, pnl) or None when the write was spooled or failed."""
    if kwargs.get("exit_price") is not None:
        # Stamped now, so a spooled update replayed later keeps the real exit time.
        kwargs["closed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return _write("update", kwargs)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def today_stats(today: str) -> dict:
    try:
        with _cursor() as cur:
            cur.execute(
                f"""SELECT COUNT(DISTINCT NULLIF(`entry_group_id`, ''))
                           + COALESCE(SUM(`entry_group_id` IS NULL OR `entry_group_id` = ''), 0),
                           COALESCE(SUM(`pnl_usd`), 0)
                    FROM `{TABLE}` WHERE `date` = %s""",
                (today,),
            )
            trades, pnl = cur.fetchone()
        return {"trades_today": int(trades), "daily_pnl": float(pnl)}
    except Exception as exc:
        logger.error(f"Error reading journal stats: {exc}")
        return {"trades_today": 0, "daily_pnl": 0.0}


def open_trade_symbols(today: str) -> list[str]:
    try:
        with _cursor() as cur:
            cur.execute(
                f"""SELECT `symbol` FROM `{TABLE}`
                    WHERE `date` = %s AND LOWER(`status`) IN ({_OPEN_IN}) AND `exit_price` IS NULL
                    GROUP BY `symbol` ORDER BY MIN(`id`)""",
                (today, *OPEN_STATUSES),
            )
            return [r[0] for r in cur.fetchall()]
    except Exception as exc:
        logger.error(f"Error querying open trades: {exc}")
        return []


def open_trade(symbol: str) -> dict | None:
    try:
        with _cursor() as cur:
            cur.execute(
                f"""SELECT `order_id`, `side`, `date`, `time_utc` FROM `{TABLE}`
                    WHERE `symbol` = %s AND LOWER(`status`) IN ({_OPEN_IN}) AND `exit_price` IS NULL
                    ORDER BY `id` DESC LIMIT 1""",
                (symbol, *OPEN_STATUSES),
            )
            row = cur.fetchone()
    except Exception as exc:
        logger.error(f"Error querying open trade for {symbol}: {exc}")
        return None
    if row is None:
        return None
    order_id, side, day, clock = row
    # PyMySQL returns TIME columns as timedelta.
    if isinstance(clock, timedelta):
        clock = (datetime.min + clock).time()
    opened_at = (
        datetime.combine(day, clock, tzinfo=timezone.utc)
        if isinstance(day, date) and isinstance(clock, time)
        else None
    )
    return {"order_id": str(order_id), "side": str(side or "LONG").upper(), "opened_at": opened_at}


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_csv(path: Path) -> int:
    with _cursor() as cur:
        cur.execute(f"SELECT * FROM `{TABLE}` ORDER BY `id`")
        names = [d[0] for d in cur.description]
        rows = cur.fetchall()
    keep = [i for i, name in enumerate(names) if name != "id"]
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([names[i] for i in keep])
        for row in rows:
            writer.writerow(["" if row[i] is None else row[i] for i in keep])
    return len(rows)


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] != "export":
        sys.exit("usage: python -m trader.journal_db export [path]")
    target = Path(sys.argv[2] if len(sys.argv) > 2 else f"trade_journal_export_{date.today()}.csv")
    print(f"Exported {export_csv(target)} trade(s) to {target}")
