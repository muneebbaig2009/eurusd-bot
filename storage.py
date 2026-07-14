"""SQLite persistence: per-pair technique weights + signal log.

Every function takes a `db` path so each currency pair keeps its own
independent database (signals_eurusd.db, signals_gbpusd.db, ...).
"""
import sqlite3
import json
from datetime import datetime, timezone
import config


def _conn(db):
    return sqlite3.connect(db)


def init_db(db):
    con = _conn(db)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS weights (
            technique TEXT PRIMARY KEY,
            weight REAL NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            direction TEXT NOT NULL,
            entry REAL NOT NULL,
            tp REAL NOT NULL,
            sl REAL NOT NULL,
            score REAL NOT NULL,
            contributors TEXT NOT NULL,
            status TEXT NOT NULL,
            closed_at TEXT,
            close_price REAL,
            tp1 REAL,
            tp2 REAL,
            confidence INTEGER,
            rr REAL,
            trend_strength INTEGER,
            timeframe TEXT
        )
    """)
    con.commit()
    # Migration for older databases
    existing = {row[1] for row in cur.execute("PRAGMA table_info(signals)").fetchall()}
    for col, coltype in [
        ("tp1", "REAL"), ("tp2", "REAL"), ("confidence", "INTEGER"),
        ("rr", "REAL"), ("trend_strength", "INTEGER"), ("timeframe", "TEXT"),
    ]:
        if col not in existing:
            cur.execute(f"ALTER TABLE signals ADD COLUMN {col} {coltype}")
    con.commit()
    con.close()


def get_weight(db, technique: str) -> float:
    con = _conn(db)
    cur = con.cursor()
    cur.execute("SELECT weight FROM weights WHERE technique = ?", (technique,))
    row = cur.fetchone()
    con.close()
    if row is None:
        set_weight(db, technique, config.DEFAULT_WEIGHT)
        return config.DEFAULT_WEIGHT
    return row[0]


def set_weight(db, technique: str, weight: float):
    weight = max(config.MIN_WEIGHT, min(config.MAX_WEIGHT, weight))
    con = _conn(db)
    cur = con.cursor()
    cur.execute(
        "INSERT INTO weights(technique, weight) VALUES(?, ?) "
        "ON CONFLICT(technique) DO UPDATE SET weight=excluded.weight",
        (technique, weight),
    )
    con.commit()
    con.close()


def all_weights(db) -> dict:
    con = _conn(db)
    cur = con.cursor()
    cur.execute("SELECT technique, weight FROM weights")
    rows = cur.fetchall()
    con.close()
    return {t: w for t, w in rows}


def log_signal(db, sig: dict) -> int:
    con = _conn(db)
    cur = con.cursor()
    cur.execute(
        "INSERT INTO signals("
        "created_at, direction, entry, tp, sl, score, contributors, status, "
        "tp1, tp2, confidence, rr, trend_strength, timeframe) "
        "VALUES(?,?,?,?,?,?,?, 'OPEN', ?,?,?,?,?,?)",
        (
            datetime.now(timezone.utc).isoformat(),
            sig["direction"], sig["entry"], sig["tp"], sig["sl"], sig["score"],
            json.dumps(sig["contributors"]),
            sig.get("tp1"), sig.get("tp2"), sig.get("confidence"),
            sig.get("rr"), sig.get("trend_strength"), sig.get("timeframe"),
        ),
    )
    con.commit()
    sid = cur.lastrowid
    con.close()
    return sid


def open_signals(db) -> list:
    con = _conn(db)
    cur = con.cursor()
    cur.execute("SELECT id, direction, entry, tp, sl, contributors, created_at "
                "FROM signals WHERE status='OPEN'")
    rows = cur.fetchall()
    con.close()
    return [
        {"id": r[0], "direction": r[1], "entry": r[2], "tp": r[3],
         "sl": r[4], "contributors": json.loads(r[5]), "created_at": r[6]}
        for r in rows
    ]


def close_signal(db, signal_id, status, close_price):
    con = _conn(db)
    cur = con.cursor()
    cur.execute(
        "UPDATE signals SET status=?, closed_at=?, close_price=? WHERE id=?",
        (status, datetime.now(timezone.utc).isoformat(), close_price, signal_id),
    )
    con.commit()
    con.close()


def has_open_signal(db) -> bool:
    con = _conn(db)
    cur = con.cursor()
    cur.execute("SELECT COUNT(*) FROM signals WHERE status='OPEN'")
    n = cur.fetchone()[0]
    con.close()
    return n > 0


def last_close_time(db):
    """Return the UTC datetime of the most recently closed signal, or None."""
    con = _conn(db)
    cur = con.cursor()
    cur.execute(
        "SELECT closed_at FROM signals WHERE status IN ('WIN','LOSS') "
        "ORDER BY closed_at DESC LIMIT 1"
    )
    row = cur.fetchone()
    con.close()
    if row is None or row[0] is None:
        return None
    try:
        return datetime.fromisoformat(row[0])
    except Exception:
        return None


def stats(db) -> dict:
    con = _conn(db)
    cur = con.cursor()
    cur.execute("SELECT status, COUNT(*) FROM signals GROUP BY status")
    rows = dict(cur.fetchall())
    con.close()
    wins = rows.get("WIN", 0)
    losses = rows.get("LOSS", 0)
    total_closed = wins + losses
    win_rate = (wins / total_closed * 100) if total_closed else 0.0
    return {"wins": wins, "losses": losses, "open": rows.get("OPEN", 0),
            "win_rate": round(win_rate, 1)}
