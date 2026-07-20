"""SQLite persistence: per-pair technique weights + signal log.

Every function takes a `db` path so each currency pair keeps its own
independent database (signals_eurusd.db, signals_gbpusd.db, ...).

Dual-trade strategy:
  Each signal opens two trades sharing the same pair_id.
  Trade 1 (trade_num=1): targets TP1
  Trade 2 (trade_num=2): targets TP2; SL moves to entry after TP1 is hit
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
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at        TEXT NOT NULL,
            direction         TEXT NOT NULL,
            entry             REAL NOT NULL,
            tp                REAL NOT NULL,
            sl                REAL NOT NULL,
            score             REAL NOT NULL,
            contributors      TEXT NOT NULL,
            status            TEXT NOT NULL,
            closed_at         TEXT,
            close_price       REAL,
            tp1               REAL,
            tp2               REAL,
            confidence        INTEGER,
            rr                REAL,
            trend_strength    INTEGER,
            timeframe         TEXT,
            lot_size          REAL,
            trade_num         INTEGER DEFAULT 1,
            pair_id           INTEGER,
            breakeven_active  INTEGER DEFAULT 0
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
    existing_sig = {r[1] for r in cur.execute("PRAGMA table_info(signals)").fetchall()}
    for col, ctype in [
        ("tp1", "REAL"), ("tp2", "REAL"), ("confidence", "INTEGER"),
        ("rr", "REAL"), ("trend_strength", "INTEGER"), ("timeframe", "TEXT"),
        ("lot_size", "REAL"), ("trade_num", "INTEGER"), ("pair_id", "INTEGER"),
        ("breakeven_active", "INTEGER"), ("mt5_ticket", "INTEGER"),
    ]:
        if col not in existing_sig:
            cur.execute(f"ALTER TABLE signals ADD COLUMN {col} {ctype}")

    existing_da = {r[1] for r in cur.execute("PRAGMA table_info(demo_account)").fetchall()}
    if "lot_size" not in existing_da:
        cur.execute("DELETE FROM demo_account")  # clear old schema, re-backfill
        cur.execute("ALTER TABLE demo_account ADD COLUMN lot_size REAL")

    con.commit()
    con.close()
    _backfill_demo(db)


# ---------------------------------------------------------------------------
# MT5 lot sizing
# ---------------------------------------------------------------------------

def calc_lot_size(balance: float, entry: float, sl: float,
                  risk_pct: float = None) -> float:
    """Suggested MT5 lot size for given risk % (default: DEMO_RISK_PCT = 2%)."""
    pct = risk_pct if risk_pct is not None else config.DEMO_RISK_PCT
    risk_usd = balance * pct
    sl_pips = abs(entry - sl) / config.PIP_SIZE
    if sl_pips < 1:
        return config.MIN_LOT
    raw     = risk_usd / (sl_pips * config.PIP_VALUE_PER_LOT)
    floored = int(raw / config.LOT_STEP) * config.LOT_STEP   # always size down
    return round(max(config.MIN_LOT, min(floored, config.MAX_LOT)), 2)


# ---------------------------------------------------------------------------
# Demo account P&L
# ---------------------------------------------------------------------------

def _calc_demo_pnl(status: str, entry: float, close_price: float,
                   lot_size: float) -> float:
    """MT5-style P&L: pip_move × lot_size × pip_value_per_lot.

    BREAKEVEN always returns 0 (close_price == entry in our simulation).
    """
    if status == "BREAKEVEN":
        return 0.0
    pip_move = abs(close_price - entry) / config.PIP_SIZE
    pnl = round(lot_size * pip_move * config.PIP_VALUE_PER_LOT, 2)
    return pnl if status == "WIN" else -pnl


def _backfill_demo(db):
    """Back-fill demo_account for any closed signals not yet recorded."""
    con = _conn(db)
    cur = con.cursor()
    cur.execute("""
        SELECT s.id, s.direction, s.status, s.entry, s.sl, s.close_price,
               s.closed_at, s.lot_size
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
        for sig_id, direction, status, entry, sl, close_price, closed_at, lot in missing:
            e, s_, cp = (entry or 0), (sl or 0), (close_price or 0)
            if lot is None:
                lot = calc_lot_size(balance, e, s_)
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
                      lot_size: float = None) -> tuple:
    """Record P&L for a freshly closed trade. Returns (new_balance, pnl)."""
    con = _conn(db)
    cur = con.cursor()
    cur.execute("SELECT balance_after FROM demo_account ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    prev = row[0] if row else config.DEMO_INITIAL_BALANCE
    if lot_size is None:
        lot_size = calc_lot_size(prev, entry, sl)
    pnl = _calc_demo_pnl(status, entry, close_price, lot_size)
    new_bal = round(prev + pnl, 2)
    cur.execute("""
        INSERT OR IGNORE INTO demo_account
            (signal_id, direction, status, pnl, balance_after, lot_size, closed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (signal_id, direction, status, pnl, new_bal, lot_size,
          datetime.now(timezone.utc).isoformat()))
    con.commit()
    con.close()
    return new_bal, pnl


def get_demo_stats(db) -> dict:
    con = _conn(db)
    cur = con.cursor()
    cur.execute("SELECT balance_after FROM demo_account ORDER BY id DESC LIMIT 1")
    last = cur.fetchone()
    cur.execute("SELECT balance_after FROM demo_account ORDER BY id ASC")
    history = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT signal_id, pnl FROM demo_account")
    per_signal_pnl = {r[0]: r[1] for r in cur.fetchall()}
    con.close()

    balance = last[0] if last else config.DEMO_INITIAL_BALANCE
    pnl_total  = round(balance - config.DEMO_INITIAL_BALANCE, 2)
    return_pct = round((balance / config.DEMO_INITIAL_BALANCE - 1) * 100, 1)
    next_lot = calc_lot_size(balance, 1.13, 1.12)  # noqa: F841 — kept for potential dashboard use
    return {
        "initial_balance": config.DEMO_INITIAL_BALANCE,
        "balance":         balance,
        "pnl_total":       pnl_total,
        "return_pct":      return_pct,
        "risk_pct":        config.DEMO_RISK_PCT * 100,
        "balance_history": [config.DEMO_INITIAL_BALANCE] + history,
        "per_signal_pnl":  per_signal_pnl,
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
# Signals — dual-trade pair support
# ---------------------------------------------------------------------------

def log_signal(db, sig: dict) -> int:
    """Insert one signal row and return its id (single-trade path)."""
    now = datetime.now(timezone.utc).isoformat()
    con = _conn(db)
    cur = con.cursor()
    cur.execute(
        "INSERT INTO signals("
        "created_at, direction, entry, tp, sl, score, contributors, status, "
        "tp1, tp2, confidence, rr, trend_strength, timeframe, lot_size) "
        "VALUES(?,?,?,?,?,?,?,'OPEN',?,?,?,?,?,?,?)",
        (now, sig["direction"], sig["entry"],
         sig.get("tp1") or sig["tp"], sig["sl"],
         sig["score"], json.dumps(sig["contributors"]),
         sig.get("tp1") or sig["tp"], sig.get("tp2"),
         sig.get("confidence"), sig.get("rr"),
         sig.get("trend_strength"), sig.get("timeframe"),
         sig.get("lot_size")),
    )
    sid = cur.lastrowid
    con.commit()
    con.close()
    return sid


def log_signal_pair(db, sig: dict) -> tuple:
    """Create Trade 1 + Trade 2 records sharing the same pair_id.

    Returns (id1, id2).  id2 is None if sig has no tp2.
    Lot size per trade = half of 2% risk (total exposure stays at 2%).
    """
    now = datetime.now(timezone.utc).isoformat()
    tp1 = sig.get("tp1") or sig["tp"]
    tp2 = sig.get("tp2")
    lot = sig.get("lot_size", config.MIN_LOT)
    con = _conn(db)
    cur = con.cursor()

    def _insert(tp_val, trade_num):
        cur.execute(
            "INSERT INTO signals("
            "created_at, direction, entry, tp, sl, score, contributors, status, "
            "tp1, tp2, confidence, rr, trend_strength, timeframe, lot_size, trade_num) "
            "VALUES(?,?,?,?,?,?,?,'OPEN',?,?,?,?,?,?,?,?)",
            (now, sig["direction"], sig["entry"], tp_val, sig["sl"], sig["score"],
             json.dumps(sig["contributors"]),
             tp1, tp2, sig.get("confidence"), sig.get("rr"),
             sig.get("trend_strength"), sig.get("timeframe"), lot, trade_num),
        )
        return cur.lastrowid

    id1 = _insert(tp1, 1)
    id2 = _insert(tp2, 2) if tp2 else None

    # Set pair_id = id1 on both rows
    if id2:
        cur.execute("UPDATE signals SET pair_id=? WHERE id IN (?,?)", (id1, id1, id2))
    else:
        cur.execute("UPDATE signals SET pair_id=? WHERE id=?", (id1, id1))

    con.commit()
    con.close()
    return id1, id2


def set_mt5_ticket(db, signal_id: int, ticket: int) -> None:
    """Store the MT5 position ticket so we can track the position later."""
    con = _conn(db)
    cur = con.cursor()
    cur.execute("UPDATE signals SET mt5_ticket=? WHERE id=?", (ticket, signal_id))
    con.commit()
    con.close()


def open_signals(db) -> list:
    con = _conn(db)
    cur = con.cursor()
    cur.execute(
        "SELECT id, direction, entry, tp, sl, contributors, created_at, rr, "
        "lot_size, trade_num, pair_id, breakeven_active, tp2, mt5_ticket "
        "FROM signals WHERE status='OPEN'"
    )
    rows = cur.fetchall()
    con.close()
    return [
        {"id": r[0], "direction": r[1], "entry": r[2], "tp": r[3],
         "sl": r[4], "contributors": json.loads(r[5]), "created_at": r[6],
         "rr": r[7], "lot_size": r[8], "trade_num": r[9] or 1,
         "pair_id": r[10], "breakeven_active": bool(r[11]), "tp2": r[12],
         "mt5_ticket": r[13]}
        for r in rows
    ]


def move_pair_to_breakeven(db, pair_id: int, trade1_id: int,
                           entry_price: float) -> bool:
    """After TP1 hit: set Trade 2's SL to entry and flag breakeven_active."""
    con = _conn(db)
    cur = con.cursor()
    cur.execute(
        "UPDATE signals SET sl=?, breakeven_active=1 "
        "WHERE pair_id=? AND id!=? AND trade_num=2 AND status='OPEN'",
        (entry_price, pair_id, trade1_id),
    )
    changed = cur.rowcount > 0
    con.commit()
    con.close()
    return changed


def close_pair_partner(db, pair_id: int, exclude_id: int,
                       status: str, close_price: float) -> dict | None:
    """Close the other open trade in a pair. Returns its data or None."""
    con = _conn(db)
    cur = con.cursor()
    cur.execute(
        "SELECT id, direction, entry, sl, lot_size FROM signals "
        "WHERE pair_id=? AND id!=? AND status='OPEN'",
        (pair_id, exclude_id),
    )
    row = cur.fetchone()
    if not row:
        con.close()
        return None
    cur.execute(
        "UPDATE signals SET status=?, closed_at=?, close_price=? "
        "WHERE id=? AND status='OPEN'",
        (status, datetime.now(timezone.utc).isoformat(), close_price, row[0]),
    )
    con.commit()
    con.close()
    return {"id": row[0], "direction": row[1], "entry": row[2],
            "sl": row[3], "lot_size": row[4]}


def close_signal(db, signal_id: int, status: str, close_price: float,
                 closed_at=None) -> bool:
    """Close a signal. Returns True only if this call changed the row.

    closed_at: UTC datetime of the actual MT5 deal. Falls back to now() when
    not provided (e.g. manual closes, timeouts, or orphan cleanup).
    """
    ts = closed_at.isoformat() if closed_at is not None else datetime.now(timezone.utc).isoformat()
    con = _conn(db)
    cur = con.cursor()
    cur.execute(
        "UPDATE signals SET status=?, closed_at=?, close_price=? "
        "WHERE id=? AND status='OPEN'",
        (status, ts, close_price, signal_id),
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
    """UTC datetime of the most recently closed signal (any trade in any pair)."""
    con = _conn(db)
    cur = con.cursor()
    cur.execute(
        "SELECT closed_at FROM signals "
        "WHERE status IN ('WIN','LOSS','BREAKEVEN') "
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
    cur.execute(
        "SELECT status, COUNT(*) FROM signals "
        "WHERE status IN ('WIN','LOSS') GROUP BY status"
    )
    rows = dict(cur.fetchall())
    # Total OPEN: any open trade
    cur.execute("SELECT COUNT(*) FROM signals WHERE status='OPEN'")
    open_count = cur.fetchone()[0]
    con.close()
    wins   = rows.get("WIN", 0)
    losses = rows.get("LOSS", 0)
    total  = wins + losses
    return {
        "wins":     wins,
        "losses":   losses,
        "open":     open_count,
        "win_rate": round(wins / total * 100, 1) if total else 0.0,
    }
