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
    cur.execute("""
        CREATE TABLE IF NOT EXISTS demo_account (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id     INTEGER NOT NULL UNIQUE,
            direction     TEXT NOT NULL,
            status        TEXT NOT NULL,
            pnl           REAL NOT NULL,
            balance_after REAL NOT NULL,
            closed_at     TEXT
        )
    """)
    con.commit()
    # Migration for older databases
    existing = {row[1] for row in cur.execute("PRAGMA table_info(signals)").fetchall()}
    for col, coltype in [
        ("tp1", "REAL"), ("tp2", "REAL"), ("confidence", "INTEGER"),
        ("rr", "REAL"), ("trend_strength", "INTEGER"), ("timeframe", "TEXT"),
        ("tp1_hit_at", "TEXT"),
    ]:
        if col not in existing:
            cur.execute(f"ALTER TABLE signals ADD COLUMN {col} {coltype}")
    con.commit()
    con.close()
    _backfill_demo(db)


def _calc_demo_pnl(status: str, entry: float, sl: float, close_price: float) -> float:
    """P&L based on actual pip movement vs SL distance, scaled to fixed risk.

    BREAKEVEN: price returned to entry after TP1 — always $0.
    WIN:  profit = risk × |close − entry| / |entry − sl|  (TP pip value)
    LOSS: loss   = −risk × |close − entry| / |entry − sl| (usually exactly −risk)
    """
    if status == "BREAKEVEN":
        return 0.0
    sl_dist = abs(entry - sl)
    if sl_dist <= 0:
        return config.DEMO_RISK_PER_TRADE if status == "WIN" else -config.DEMO_RISK_PER_TRADE
    price_move = abs(close_price - entry)
    raw = config.DEMO_RISK_PER_TRADE * (price_move / sl_dist)
    return round(raw, 2) if status == "WIN" else round(-raw, 2)


def _backfill_demo(db):
    """Back-fill demo_account rows for any closed signals not yet recorded."""
    con = _conn(db)
    cur = con.cursor()
    cur.execute("""
        SELECT s.id, s.direction, s.status, s.entry, s.sl, s.close_price, s.closed_at
        FROM signals s
        WHERE s.status IN ('WIN','LOSS','BREAKEVEN')
          AND s.id NOT IN (SELECT signal_id FROM demo_account)
        ORDER BY s.id ASC
    """)
    missing = cur.fetchall()
    if missing:
        cur.execute("SELECT balance_after FROM demo_account ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        balance = row[0] if row else config.DEMO_INITIAL_BALANCE
        for sig_id, direction, status, entry, sl, close_price, closed_at in missing:
            pnl = _calc_demo_pnl(status, entry or 0, sl or 0, close_price or 0)
            balance = round(balance + pnl, 2)
            cur.execute("""
                INSERT OR IGNORE INTO demo_account
                    (signal_id, direction, status, pnl, balance_after, closed_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (sig_id, direction, status, pnl, balance, closed_at))
        con.commit()
    con.close()


def record_demo_trade(db, signal_id: int, direction: str, status: str,
                      entry: float, sl: float, close_price: float):
    """Record P&L for a freshly closed trade using actual price levels.
    Returns (new_balance, pnl)."""
    pnl = _calc_demo_pnl(status, entry, sl, close_price)
    con = _conn(db)
    cur = con.cursor()
    cur.execute("SELECT balance_after FROM demo_account ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    balance = round((row[0] if row else config.DEMO_INITIAL_BALANCE) + pnl, 2)
    cur.execute("""
        INSERT OR IGNORE INTO demo_account
            (signal_id, direction, status, pnl, balance_after, closed_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (signal_id, direction, status, pnl, balance,
          datetime.now(timezone.utc).isoformat()))
    con.commit()
    con.close()
    return balance, pnl


def get_demo_stats(db) -> dict:
    """Return demo account summary for the exporter."""
    con = _conn(db)
    cur = con.cursor()
    cur.execute("SELECT balance_after FROM demo_account ORDER BY id DESC LIMIT 1")
    last = cur.fetchone()
    cur.execute("SELECT balance_after FROM demo_account ORDER BY id ASC")
    history = [r[0] for r in cur.fetchall()]
    # per-signal P&L keyed by signal_id for the exporter to attach
    cur.execute("SELECT signal_id, pnl FROM demo_account")
    per_signal = {r[0]: r[1] for r in cur.fetchall()}
    con.close()

    balance = last[0] if last else config.DEMO_INITIAL_BALANCE
    pnl_total = round(balance - config.DEMO_INITIAL_BALANCE, 2)
    return_pct = round((balance / config.DEMO_INITIAL_BALANCE - 1) * 100, 1)
    return {
        "initial_balance": config.DEMO_INITIAL_BALANCE,
        "balance": balance,
        "pnl_total": pnl_total,
        "return_pct": return_pct,
        "risk_per_trade": config.DEMO_RISK_PER_TRADE,
        "balance_history": [config.DEMO_INITIAL_BALANCE] + history,
        "per_signal_pnl": per_signal,
    }


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
    cur.execute(
        "SELECT id, direction, entry, tp, sl, contributors, created_at, rr, "
        "tp2, tp1_hit_at, status "
        "FROM signals WHERE status IN ('OPEN','TP1_HIT')"
    )
    rows = cur.fetchall()
    con.close()
    return [
        {"id": r[0], "direction": r[1], "entry": r[2], "tp": r[3],
         "sl": r[4], "contributors": json.loads(r[5]), "created_at": r[6],
         "rr": r[7], "tp2": r[8], "tp1_hit_at": r[9], "status": r[10]}
        for r in rows
    ]


def set_tp1_hit(db, signal_id: int) -> bool:
    """Transition signal to TP1_HIT. Returns True only if this call changed the row."""
    con = _conn(db)
    cur = con.cursor()
    cur.execute(
        "UPDATE signals SET status='TP1_HIT', tp1_hit_at=? WHERE id=? AND status='OPEN'",
        (datetime.now(timezone.utc).isoformat(), signal_id),
    )
    changed = cur.rowcount > 0
    con.commit()
    con.close()
    return changed


def close_signal(db, signal_id, status, close_price) -> bool:
    """Close a signal. Returns True only if this call actually changed the row
    (guards against two concurrent runs processing the same signal)."""
    con = _conn(db)
    cur = con.cursor()
    cur.execute(
        "UPDATE signals SET status=?, closed_at=?, close_price=? "
        "WHERE id=? AND status IN ('OPEN','TP1_HIT')",
        (status, datetime.now(timezone.utc).isoformat(), close_price, signal_id),
    )
    changed = cur.rowcount > 0
    con.commit()
    con.close()
    return changed


def has_open_signal(db) -> bool:
    con = _conn(db)
    cur = con.cursor()
    cur.execute("SELECT COUNT(*) FROM signals WHERE status IN ('OPEN','TP1_HIT')")
    n = cur.fetchone()[0]
    con.close()
    return n > 0


def last_close_time(db):
    """Return the UTC datetime of the most recently closed signal, or None."""
    con = _conn(db)
    cur = con.cursor()
    cur.execute(
        "SELECT closed_at FROM signals WHERE status IN ('WIN','LOSS','BREAKEVEN') "
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
    breakevens = rows.get("BREAKEVEN", 0)
    total_closed = wins + losses + breakevens
    win_rate = (wins / total_closed * 100) if total_closed else 0.0
    return {
        "wins": wins, "losses": losses, "breakevens": breakevens,
        "open": rows.get("OPEN", 0) + rows.get("TP1_HIT", 0),
        "win_rate": round(win_rate, 1),
    }
