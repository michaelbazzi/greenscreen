"""
Shared SQLite connection helper for trades.db.

Turns on WAL mode (so concurrent readers/writers from execute_trade.py and
the autotrader don't collide) and wraps writes in a retry/backoff loop for
the transient "database is locked" errors WAL doesn't fully eliminate under
overlapping writers.
"""

import sqlite3
import time
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
DB_PATH = REPO_DIR / "trades.db"

TRADES_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc TEXT NOT NULL,
    account_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    order_type TEXT NOT NULL,
    shares REAL,
    notional REAL,
    limit_price REAL,
    fill_price REAL,
    status TEXT NOT NULL,
    order_id TEXT NOT NULL,
    paper_account INTEGER NOT NULL
);
"""

DECISIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc TEXT NOT NULL,
    run_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    action TEXT NOT NULL,
    size_type TEXT,
    size_value REAL,
    technical_score REAL,
    rationale TEXT,
    source_url TEXT,
    risk_checks_passed INTEGER NOT NULL,
    rejection_reason TEXT,
    order_id TEXT,
    UNIQUE(run_id, ticker, action)
);
"""


def get_connection(db_path=DB_PATH, timeout=10.0):
    conn = sqlite3.connect(db_path, timeout=timeout)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def init_decisions_table(conn):
    conn.execute(TRADES_SCHEMA)
    conn.execute(DECISIONS_SCHEMA)
    conn.commit()


def log_trade(conn, row):
    """row matches the existing trades schema used by execute_trade.py:
    (timestamp_utc, account_id, ticker, side, order_type, shares, notional,
     limit_price, fill_price, status, order_id, paper_account)"""
    return execute_with_retry(
        conn,
        """INSERT INTO trades
           (timestamp_utc, account_id, ticker, side, order_type,
            shares, notional, limit_price, fill_price, status, order_id, paper_account)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        row,
    )


def export_csv(conn, csv_path=REPO_DIR / "trades.csv"):
    import csv as csv_module

    cur = conn.execute("SELECT * FROM trades ORDER BY id")
    cols = [d[0] for d in cur.description]
    with open(csv_path, "w", newline="") as f:
        writer = csv_module.writer(f)
        writer.writerow(cols)
        writer.writerows(cur.fetchall())


def execute_with_retry(conn, sql, params=(), max_attempts=5, base_delay=0.2):
    attempt = 0
    while True:
        try:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() or attempt >= max_attempts - 1:
                raise
            time.sleep(base_delay * (2 ** attempt))
            attempt += 1


def log_decision(conn, row):
    """row: (timestamp_utc, run_id, ticker, action, size_type, size_value,
    technical_score, rationale, source_url, risk_checks_passed,
    rejection_reason, order_id)"""
    return execute_with_retry(
        conn,
        """INSERT INTO decisions
           (timestamp_utc, run_id, ticker, action, size_type, size_value,
            technical_score, rationale, source_url, risk_checks_passed,
            rejection_reason, order_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        row,
    )


def decision_already_logged(conn, run_id, ticker, action):
    cur = conn.execute(
        "SELECT 1 FROM decisions WHERE run_id = ? AND ticker = ? AND action = ? LIMIT 1",
        (run_id, ticker, action),
    )
    return cur.fetchone() is not None


def count_new_positions_today(conn, utc_date_str):
    cur = conn.execute(
        """SELECT COUNT(*) FROM decisions
           WHERE date(timestamp_utc) = ?
             AND action = 'buy'
             AND risk_checks_passed = 1""",
        (utc_date_str,),
    )
    return cur.fetchone()[0]


def sum_notional_deployed_today(conn, utc_date_str):
    cur = conn.execute(
        """SELECT COALESCE(SUM(size_value), 0) FROM decisions
           WHERE date(timestamp_utc) = ?
             AND action = 'buy'
             AND risk_checks_passed = 1
             AND size_type = 'notional'""",
        (utc_date_str,),
    )
    return cur.fetchone()[0]
