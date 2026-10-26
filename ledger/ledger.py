"""研判账本 SQLite：快照、研判结论、哨兵项、成本、心跳。"""
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, symbol TEXT NOT NULL, name TEXT,
    price REAL, prev_close REAL, pct REAL, level TEXT, source TEXT
);
CREATE TABLE IF NOT EXISTS judgments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, kind TEXT NOT NULL, symbol TEXT,
    title TEXT, verdict_json TEXT, cost_rmb REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sentinel_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, symbol TEXT, source TEXT,
    title TEXT, grade TEXT, url TEXT, pushed INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS costs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL, model TEXT, calls INTEGER DEFAULT 0,
    cost_rmb REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS heartbeat (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_run_ts TEXT, status TEXT, detail TEXT
);
"""


def init_db(path):
    """建库（path 可为 ':memory:'，用于测试）。返回 connection。"""
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def record_snapshot(conn, ts, symbol, name, price, prev_close, pct, level,
                    source):
    conn.execute(
        "INSERT INTO snapshots (ts, symbol, name, price, prev_close, pct,"
        " level, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ts, symbol, name, price, prev_close, pct, level, source))
    conn.commit()


def record_judgment(conn, ts, kind, symbol, title, verdict_json, cost_rmb=0):
    conn.execute(
        "INSERT INTO judgments (ts, kind, symbol, title, verdict_json,"
        " cost_rmb) VALUES (?, ?, ?, ?, ?, ?)",
        (ts, kind, symbol, title, verdict_json, cost_rmb))
    conn.commit()


def record_sentinel(conn, ts, symbol, source, title, grade, url="",
                    pushed=0):
    conn.execute(
        "INSERT INTO sentinel_items (ts, symbol, source, title, grade, url,"
        " pushed) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ts, symbol, source, title, grade, url, pushed))
    conn.commit()


def add_cost(conn, date, model, calls, cost_rmb):
    conn.execute(
        "INSERT INTO costs (date, model, calls, cost_rmb) VALUES (?, ?, ?, ?)",
        (date, model, calls, cost_rmb))
    conn.commit()


def update_heartbeat(conn, ts, status, detail=""):
    conn.execute(
        "INSERT INTO heartbeat (id, last_run_ts, status, detail)"
        " VALUES (1, ?, ?, ?)"
        " ON CONFLICT(id) DO UPDATE SET last_run_ts=excluded.last_run_ts,"
        " status=excluded.status, detail=excluded.detail",
        (ts, status, detail))
    conn.commit()


def get_heartbeat(conn):
    row = conn.execute(
        "SELECT last_run_ts, status, detail FROM heartbeat WHERE id = 1"
    ).fetchone()
    return row


def get_recent_snapshots(conn, limit=10):
    return conn.execute(
        "SELECT ts, symbol, price, pct, level, source FROM snapshots"
        " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
