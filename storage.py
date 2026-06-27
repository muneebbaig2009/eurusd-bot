"""SQLite persistence: technique weights + signal log (for learning + results)."""
import sqlite3
import json
from datetime import datetime, timezone
import config


def _conn():
    return sqlite3.connect(config.DB_PATH)


def init_db():
    con = _conn()
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
            direction TEXT NOT NULL,      -- BUY / SELL
            entry REAL NOT NULL,
            tp REAL NOT NULL,             -- == tp1, used for win/loss
            sl REAL NOT NULL,
            score REAL NOT NULL,
            contributors TEXT NOT NULL,   -- JSON {technique: vote}
            status TEXT NOT NULL,         -- OPEN / WIN / LOSS
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

    # Migration: add new columns if upgrading an older database
    existing = {row[1] for row in cur.execute("PRAGMA table_info(signals)").fetchall()}
    for col, coltype in [
        ("tp1", "REAL"), ("tp2", "REAL"), ("confidence", "INTEGER"),
        ("rr", "REAL"), ("trend_strength", "INTEGER"), ("timeframe", "TEXT"),
    ]:
        if col not in existing:
            cur.execute(f"ALTER TABLE signals ADD COLUMN {col} {coltype}")
    con.commit()
    con.close()


def get_weight(technique: str) -> float:
    con = _conn()
    cur = con.cursor()
    cur.execute("SELECT weight FROM weights WHERE technique = ?", (technique,))
    row = cur.fetchone()
    con.close()
    if row is None:
        set_weight(technique, config.DEFAULT_WEIGHT)
        return config.DEFAULT_WEIGHT
    return row[0]


def set_weight(technique: str, weight: float):
    weight = max(config.MIN_WEIGHT, min(config.MAX_WEIGHT, weight))
    con = _conn()
    cur = con.cursor()
    cur.execute(
        "INSERT INTO weights(technique, weight) VALUES(?, ?) "
        "ON CONFLICT(technique) DO UPDATE SET weight=excluded.weight",
        (technique, weight),
    )
    con.commit()
    con.close()


def all_weights() -> dict:
    con = _conn()
    cur = con.cursor()
    cur.execute("SELECT technique, weight FROM weights")
    rows = cur.fetchall()
    con.close()
    return {t: w for t, w in rows}


def log_signal(sig: dict) -> int:
    """Insert a new OPEN signal from a signal dict produced by signal_engine."""
    con = _conn()
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


def open_signals() -> list:
    con = _conn()
    cur = con.cursor()
    cur.execute("SELECT id, direction, entry, tp, sl, contributors FROM signals WHERE status='OPEN'")
    rows = cur.fetchall()
    con.close()
    return [
        {"id": r[0], "direction": r[1], "entry": r[2], "tp": r[3],
         "sl": r[4], "contributors": json.loads(r[5])}
        for r in rows
    ]


def close_signal(signal_id, status, close_price):
    con = _conn()
    cur = con.cursor()
    cur.execute(
        "UPDATE signals SET status=?, closed_at=?, close_price=? WHERE id=?",
        (status, datetime.now(timezone.utc).isoformat(), close_price, signal_id),
    )
    con.commit()
    con.close()


def has_open_signal() -> bool:
    """Avoid spamming overlapping signals while one is still live."""
    con = _conn()
    cur = con.cursor()
    cur.execute("SELECT COUNT(*) FROM signals WHERE status='OPEN'")
    n = cur.fetchone()[0]
    con.close()
    return n > 0


def stats() -> dict:
    con = _conn()
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
