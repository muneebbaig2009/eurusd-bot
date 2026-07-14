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
            timeframe TEXT,
            lot_size REAL
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
            lot_size      REAL,
            closed_at     TEXT
        )
    """)
    con.commit()

    # Column migrations for older databases
    existing_sig = {row[1] for row in cur.execute("PRAGMA table_info(signals)").fetchall()}
    for col, coltype in [
        ("tp1", "REAL"), ("tp2", "REAL"), ("confidence", "INTEGER"),
        ("rr", "REAL"), ("trend_strength", "INTEGER"), ("timeframe", "TEXT"),
        ("lot_size", "REAL"),
    ]:
        if col not in existing_sig:
            cur.execute(f"ALTER TABLE signals ADD COLUMN {col} {coltype}")

    existing_da = {row[1] for row in cur.execute("PRAGMA table_info(demo_account)").fetchall()}
    if "lot_size" not in existing_da:
        # First run with lot-based model: clear old records and re-backfill from scratch
        cur.execute("DELETE FROM demo_account")
        cur.execute("ALTER TABLE demo_account ADD COLUMN lot_size REAL")

    con.commit()
    con.close()
    _backfill_demo(db)


# ---------------------------------------------------------------------------
# MT5 lot sizing helpers
# ---------------------------------------------------------------------------

def calc_lot_size(balance: float, entry: float, sl: float) -> float:
    """Suggested MT5 lot size: risk DEMO_RISK_PCT of balance over SL distance.

    lot = (balance × risk%) / (sl_pips × pip_value_per_lot)
    Rounded to nearest LOT_STEP, floored at MIN_LOT.
    """
    risk_usd = balance * config.DEMO_RISK_PCT
    sl_pips = abs(entry - sl) / config.PIP_SIZE
    if sl_pips < 1:
        return config.MIN_LOT
    raw = risk_usd / (sl_pips * config.PIP_VALUE_PER_LOT)
    stepped = round(raw / config.LOT_STEP) * config.LOT_STEP
    return round(max(config.MIN_LOT, min(stepped, config.MAX_LOT)), 2)


def _calc_demo_pnl(status: str, entry: float, close_price: float,
                   lot_size: float) -> float:
    """MT5-style P&L: pip_move × lot_size × pip_value_per_lot.

    WIN:  +pip_move dollars
    LOSS: −pip_move dollars
    """
    pip_move = abs(close_price - entry) / config.PIP_SIZE
    pnl = round(lot_size * pip_move * config.PIP_VALUE_PER_LOT, 2)
    return pnl if status == "WIN" else -pnl


# ---------------------------------------------------------------------------
# Demo account persistence
# ---------------------------------------------------------------------------

def _backfill_demo(db):
    """Back-fill demo_account for any closed signals not yet recorded."""
    con = _conn(db)
    cur = con.cursor()
    cur.execute("""
        SELECT s.id, s.direction, s.status, s.entry, s.sl, s.close_price,
               s.closed_at, s.lot_size
        FROM signals s
        WHERE s.status IN ('WIN','LOSS')
          AND s.id NOT IN (SELECT signal_id FROM demo_account)
        ORDER BY s.id ASC
    """)
    missing = cur.fetchall()
    if missing:
        cur.execute("SELECT balance_after FROM demo_account ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        balance = row[0] if row else config.DEMO_INITIAL_BALANCE
        for sig_id, direction, status, entry, sl, close_price, closed_at, lot in missing:
            e = entry or 0
            s = sl or 0
            cp = close_price or 0
            if lot is None:
                lot = calc_lot_size(balance, e, s)
            pnl = _calc_demo_pnl(status, e, cp, lot)
            balance = round(balance + pnl, 2)
            cur.execute("""
                INSERT OR IGNORE INTO demo_account
                    (signal_id, direction, status, pnl, balance_after, lot_size, closed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (sig_id, direction, status, pnl, balance, lot, closed_at))
        con.commit()
    con.close()


def record_demo_trade(db, signal_id: int, direction: str, status: str,
                      entry: float, sl: float, close_price: float,
                      lot_size: float = None):
    """Record P&L for a freshly closed trade using MT5 lot-based calculation.
    Returns (new_balance, pnl)."""
    con = _conn(db)
    cur = con.cursor()
    cur.execute("SELECT balance_after FROM demo_account ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    prev_balance = row[0] if row else config.DEMO_INITIAL_BALANCE
    if lot_size is None:
        lot_size = calc_lot_size(prev_balance, entry, sl)
    pnl = _calc_demo_pnl(status, entry, close_price, lot_size)
    new_balance = round(prev_balance + pnl, 2)
    cur.execute("""
        INSERT OR IGNORE INTO demo_account
            (signal_id, direction, status, pnl, balance_after, lot_size, closed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (signal_id, direction, status, pnl, new_balance, lot_size,
          datetime.now(timezone.utc).isoformat()))
    con.commit()
    con.close()
    return new_balance, pnl


def get_demo_stats(db) -> dict:
    """Return demo account summary for the exporter."""
    con = _conn(db)
    cur = con.cursor()
    cur.execute("SELECT balance_after FROM demo_account ORDER BY id DESC LIMIT 1")
    last = cur.fetchone()
    cur.execute("SELECT balance_after FROM demo_account ORDER BY id ASC")
    history = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT signal_id, pnl, lot_size FROM demo_account")
    rows = cur.fetchall()
    con.close()

    per_signal_pnl = {r[0]: r[1] for r in rows}
    per_signal_lot = {r[0]: r[2] for r in rows}

    balance = last[0] if last else config.DEMO_INITIAL_BALANCE
    pnl_total = round(balance - config.DEMO_INITIAL_BALANCE, 2)
    return_pct = round((balance / config.DEMO_INITIAL_BALANCE - 1) * 100, 1)
    return {
        "initial_balance": config.DEMO_INITIAL_BALANCE,
        "balance": balance,
        "pnl_total": pnl_total,
        "return_pct": return_pct,
        "risk_pct": config.DEMO_RISK_PCT * 100,
        "balance_history": [config.DEMO_INITIAL_BALANCE] + history,
        "per_signal_pnl": per_signal_pnl,
        "per_signal_lot": per_signal_lot,
    }


# ---------------------------------------------------------------------------
# Weights
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

def log_signal(db, sig: dict) -> int:
    con = _conn(db)
    cur = con.cursor()
    cur.execute(
        "INSERT INTO signals("
        "created_at, direction, entry, tp, sl, score, contributors, status, "
        "tp1, tp2, confidence, rr, trend_strength, timeframe, lot_size) "
        "VALUES(?,?,?,?,?,?,?, 'OPEN', ?,?,?,?,?,?,?)",
        (
            datetime.now(timezone.utc).isoformat(),
            sig["direction"], sig["entry"], sig["tp"], sig["sl"], sig["score"],
            json.dumps(sig["contributors"]),
            sig.get("tp1"), sig.get("tp2"), sig.get("confidence"),
            sig.get("rr"), sig.get("trend_strength"), sig.get("timeframe"),
            sig.get("lot_size"),
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
        "SELECT id, direction, entry, tp, sl, contributors, created_at, rr, lot_size "
        "FROM signals WHERE status='OPEN'"
    )
    rows = cur.fetchall()
    con.close()
    return [
        {"id": r[0], "direction": r[1], "entry": r[2], "tp": r[3],
         "sl": r[4], "contributors": json.loads(r[5]), "created_at": r[6],
         "rr": r[7], "lot_size": r[8]}
        for r in rows
    ]


def close_signal(db, signal_id, status, close_price) -> bool:
    """Close a signal. Returns True only if this call changed the row
    (prevents duplicate Discord alerts from concurrent CI runs)."""
    con = _conn(db)
    cur = con.cursor()
    cur.execute(
        "UPDATE signals SET status=?, closed_at=?, close_price=? "
        "WHERE id=? AND status='OPEN'",
        (status, datetime.now(timezone.utc).isoformat(), close_price, signal_id),
    )
    changed = cur.rowcount > 0
    con.commit()
    con.close()
    return changed


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
    return {
        "wins": wins, "losses": losses,
        "open": rows.get("OPEN", 0),
        "win_rate": round(win_rate, 1),
    }
