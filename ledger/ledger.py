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
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, symbol TEXT NOT NULL, level TEXT NOT NULL,
    pct REAL, title TEXT, pushed INTEGER DEFAULT 0, dry_run INTEGER DEFAULT 0
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
    cur = conn.execute(
        "INSERT INTO sentinel_items (ts, symbol, source, title, grade, url,"
        " pushed) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ts, symbol, source, title, grade, url, pushed))
    conn.commit()
    return cur.lastrowid


def mark_sentinel_pushed(conn, row_id):
    conn.execute("UPDATE sentinel_items SET pushed = 1 WHERE id = ?",
                 (row_id,))
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


def record_alert(conn, ts, symbol, level, pct, title, pushed=0, dry_run=0):
    """记一条已放行并（真实或 dry-run）投递的预警，供门控去重/冷却/每日上限查询。

    门控统计的是全部记录（含 dry-run），这样影子测试跑出的抑制行为与实盘一致。
    """
    conn.execute(
        "INSERT INTO alerts (ts, symbol, level, pct, title, pushed, dry_run)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ts, symbol, level, pct, title, pushed, dry_run))
    conn.commit()


def count_alerts_since(conn, symbol, since_ts, level=None):
    """since_ts 之后该标的的预警数；level 指定时只计同级别（去重用）。"""
    if level is None:
        row = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE symbol = ? AND ts >= ?",
            (symbol, since_ts)).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE symbol = ? AND ts >= ?"
            " AND level = ?", (symbol, since_ts, level)).fetchone()
    return row[0] if row else 0


def count_alerts_today(conn, symbol, date_prefix):
    """当日（北京时间日期前缀，如 '2026-10-27'）该标的的预警数。"""
    row = conn.execute(
        "SELECT COUNT(*) FROM alerts WHERE symbol = ? AND ts LIKE ?",
        (symbol, date_prefix + "%")).fetchone()
    return row[0] if row else 0


def count_judgments(conn, kind, date_prefix, symbol=None):
    """当日某类研判数；symbol 指定时只计该标的（日配额用）。"""
    if symbol is None:
        row = conn.execute(
            "SELECT COUNT(*) FROM judgments WHERE kind = ? AND ts LIKE ?",
            (kind, date_prefix + "%")).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) FROM judgments WHERE kind = ? AND ts LIKE ?"
            " AND symbol = ?", (kind, date_prefix + "%", symbol)).fetchone()
    return row[0] if row else 0


def count_judgments_since(conn, kind, symbol, since_ts):
    """since_ts 之后某标的某类研判数（辩论 2 小时去重用）。"""
    row = conn.execute(
        "SELECT COUNT(*) FROM judgments WHERE kind = ? AND symbol = ?"
        " AND ts >= ?", (kind, symbol, since_ts)).fetchone()
    return row[0] if row else 0


def sum_costs_month(conn, year_month_prefix):
    """某月累计 LLM 花费（costs.date 前缀如 '2026-10'）。"""
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_rmb), 0) FROM costs WHERE date LIKE ?",
        (year_month_prefix + "%",)).fetchone()
    return row[0] if row else 0.0


def recent_sentinel(conn, symbol, since_ts, limit=20):
    """某标的 since_ts 之后的哨兵记录（异动联动：先查账本再搜因）。"""
    return conn.execute(
        "SELECT ts, title, grade FROM sentinel_items WHERE symbol = ?"
        " AND ts >= ? ORDER BY id DESC LIMIT ?",
        (symbol, since_ts, limit)).fetchall()
