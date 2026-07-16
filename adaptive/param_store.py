"""Versioned, SQLite-backed storage for every adaptive strategy parameter.

Each write appends a new row (immutable audit log) so any change can be
inspected and rolled back.  The current value is the most-recent row for
a (param_name, regime) pair.

Three tables are created here and shared across the adaptive package:
  adaptive_params   — versioned parameter values
  optimization_log  — audit trail of every optimization run
  trade_context     — full market state captured at each trade entry
"""
import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

# ── Per-DB locks and store cache ──────────────────────────────────────────────
_STORES: dict[str, "AdaptiveParamStore"] = {}
_STORES_LOCK = threading.Lock()
_DB_LOCKS: dict[str, threading.RLock] = {}
_DB_LOCKS_META = threading.Lock()


def _db_lock(db: str) -> threading.RLock:
    with _DB_LOCKS_META:
        if db not in _DB_LOCKS:
            _DB_LOCKS[db] = threading.RLock()
        return _DB_LOCKS[db]


def get_store(db: str) -> "AdaptiveParamStore":
    """Return the singleton AdaptiveParamStore for this database path."""
    with _STORES_LOCK:
        if db not in _STORES:
            _STORES[db] = AdaptiveParamStore(db)
        return _STORES[db]


# ── DDL ───────────────────────────────────────────────────────────────────────
_SCHEMA = """\
CREATE TABLE IF NOT EXISTS adaptive_params (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    regime        TEXT    NOT NULL DEFAULT 'all',
    param_name    TEXT    NOT NULL,
    current_value REAL    NOT NULL,
    prev_value    REAL,
    min_value     REAL,
    max_value     REAL,
    updated_at    TEXT    NOT NULL,
    update_reason TEXT,
    perf_before   TEXT,
    n_trades      INTEGER,
    version       INTEGER NOT NULL DEFAULT 1,
    confirmed     INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS optimization_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at        TEXT NOT NULL,
    pair          TEXT,
    regime        TEXT NOT NULL DEFAULT 'all',
    n_trades      INTEGER,
    changes_json  TEXT,
    perf_before   TEXT,
    accepted      INTEGER NOT NULL DEFAULT 0,
    reason        TEXT
);
CREATE TABLE IF NOT EXISTS trade_context (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id      INTEGER NOT NULL UNIQUE,
    pair           TEXT    NOT NULL,
    direction      TEXT    NOT NULL,
    regime         TEXT,
    session        TEXT,
    timeframe      TEXT,
    entry          REAL,
    atr            REAL,
    adx            REAL,
    rsi            REAL,
    macd_hist      REAL,
    ema_above      INTEGER,
    stoch_k        REAL,
    stoch_d        REAL,
    bb_pct         REAL,
    supertrend_dir INTEGER,
    score          REAL,
    confidence     INTEGER,
    votes_json     TEXT,
    weights_json   TEXT,
    params_json    TEXT,
    r_multiple     REAL,
    pnl            REAL,
    hold_hours     REAL,
    won            INTEGER,
    created_at     TEXT    NOT NULL
);
"""


class AdaptiveParamStore:
    """Read/write adaptive parameters with full, versioned audit history.

    All parameter values live exclusively in SQLite so they survive process
    restarts.  The DEFAULTS dict also defines the hard min/max bounds that
    prevent any single optimization step from making reckless changes.
    """

    # Canonical default value + bounds for every adaptive parameter.
    # "v"    = starting default (matches current hard-coded values in the bot)
    # "min"  = absolute floor  — optimizer may never go below this
    # "max"  = absolute ceiling — optimizer may never exceed this
    # "step" = smallest meaningful perturbation
    DEFAULTS: dict[str, dict] = {
        # ── Signal gates ─────────────────────────────────────────────────────
        "SIGNAL_THRESHOLD":     {"v": 1.2,    "min": 0.5,    "max": 3.0,   "step": 0.1},
        "MIN_ADX":              {"v": 15.0,   "min": 10.0,   "max": 40.0,  "step": 1.0},
        "MIN_CONFIDENCE":       {"v": 55.0,   "min": 40.0,   "max": 80.0,  "step": 2.0},
        # ── ATR multipliers ──────────────────────────────────────────────────
        "SL_ATR_MULT":          {"v": 1.5,    "min": 0.5,    "max": 3.0,   "step": 0.1},
        "TP1_ATR_MULT":         {"v": 0.75,   "min": 0.25,   "max": 2.0,   "step": 0.05},
        "TP2_ATR_MULT":         {"v": 1.5,    "min": 0.5,    "max": 4.0,   "step": 0.1},
        # ── Timing ───────────────────────────────────────────────────────────
        "SIGNAL_COOLDOWN_BARS": {"v": 2.0,    "min": 1.0,    "max": 10.0,  "step": 1.0},
        "MAX_HOLD_HOURS":       {"v": 8.0,    "min": 2.0,    "max": 24.0,  "step": 1.0},
        # ── RSI ──────────────────────────────────────────────────────────────
        "RSI_PERIOD":           {"v": 14.0,   "min": 7.0,    "max": 21.0,  "step": 1.0},
        "RSI_OS":               {"v": 35.0,   "min": 20.0,   "max": 45.0,  "step": 1.0},
        "RSI_OB":               {"v": 75.0,   "min": 55.0,   "max": 80.0,  "step": 1.0},
        # ── MA Cross ─────────────────────────────────────────────────────────
        "MA_FAST":              {"v": 8.0,    "min": 5.0,    "max": 15.0,  "step": 1.0},
        "MA_SLOW":              {"v": 21.0,   "min": 15.0,   "max": 35.0,  "step": 1.0},
        # ── MACD ─────────────────────────────────────────────────────────────
        "MACD_FAST":            {"v": 10.0,   "min": 8.0,    "max": 16.0,  "step": 1.0},
        "MACD_SLOW":            {"v": 21.0,   "min": 17.0,   "max": 30.0,  "step": 1.0},
        "MACD_SIGNAL":          {"v": 9.0,    "min": 5.0,    "max": 13.0,  "step": 1.0},
        # ── Bollinger Bands ───────────────────────────────────────────────────
        "BB_PERIOD":            {"v": 15.0,   "min": 10.0,   "max": 25.0,  "step": 1.0},
        "BB_STD":               {"v": 1.5,    "min": 1.0,    "max": 2.5,   "step": 0.25},
        # ── EMA Trend ────────────────────────────────────────────────────────
        "EMA_PERIOD":           {"v": 200.0,  "min": 100.0,  "max": 300.0, "step": 10.0},
        "EMA_BUFFER":           {"v": 0.0003, "min": 0.0,    "max": 0.001, "step": 0.0001},
        # ── Stochastic ───────────────────────────────────────────────────────
        "STOCH_OS":             {"v": 30.0,   "min": 15.0,   "max": 35.0,  "step": 1.0},
        "STOCH_OB":             {"v": 70.0,   "min": 65.0,   "max": 85.0,  "step": 1.0},
        # ── Supertrend ───────────────────────────────────────────────────────
        "ST_LENGTH":            {"v": 7.0,    "min": 5.0,    "max": 14.0,  "step": 1.0},
        "ST_MULT":              {"v": 3.0,    "min": 2.0,    "max": 5.0,   "step": 0.5},
    }

    def __init__(self, db: str) -> None:
        self._db = db
        self._lock = _db_lock(db)
        self._ensure_schema()

    # ── Schema ────────────────────────────────────────────────────────────────

    def _ensure_schema(self) -> None:
        with self._lock:
            con = sqlite3.connect(self._db)
            try:
                con.executescript(_SCHEMA)
                con.commit()
            finally:
                con.close()

    # ── Reads ─────────────────────────────────────────────────────────────────

    def get(self, name: str, regime: str = "all") -> Optional[float]:
        """Return the current adaptive value; falls back to DEFAULTS if never set."""
        with self._lock:
            con = sqlite3.connect(self._db)
            try:
                cur = con.cursor()
                regimes = [regime, "all"] if regime != "all" else ["all"]
                for r in regimes:
                    cur.execute(
                        "SELECT current_value FROM adaptive_params "
                        "WHERE param_name=? AND regime=? ORDER BY id DESC LIMIT 1",
                        (name, r),
                    )
                    row = cur.fetchone()
                    if row is not None:
                        return float(row[0])
            finally:
                con.close()
        return self.DEFAULTS.get(name, {}).get("v")

    def get_all(self, regime: str = "all") -> dict[str, float]:
        """Return every parameter's current value as a flat dict."""
        return {name: self.get(name, regime) for name in self.DEFAULTS}

    # ── Writes ────────────────────────────────────────────────────────────────

    def set(
        self,
        name: str,
        value: float,
        reason: str = "",
        perf_before: Optional[dict] = None,
        n_trades: int = 0,
        regime: str = "all",
        confirmed: int = 1,
    ) -> bool:
        """Write a new parameter value (clamped to [min, max]).

        Returns True if the stored value actually changed.
        """
        defn = self.DEFAULTS.get(name, {})
        lo, hi = defn.get("min", -1e9), defn.get("max", 1e9)
        value = float(max(lo, min(hi, value)))

        old = self.get(name, regime)
        if old is not None and abs(value - old) < 1e-9:
            return False

        with self._lock:
            con = sqlite3.connect(self._db)
            try:
                cur = con.cursor()
                cur.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM adaptive_params "
                    "WHERE param_name=? AND regime=?",
                    (name, regime),
                )
                version = cur.fetchone()[0] + 1
                cur.execute(
                    """INSERT INTO adaptive_params
                       (regime, param_name, current_value, prev_value,
                        min_value, max_value, updated_at, update_reason,
                        perf_before, n_trades, version, confirmed)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        regime, name, value, old,
                        lo, hi, _now(), reason,
                        json.dumps(perf_before) if perf_before else None,
                        n_trades, version, confirmed,
                    ),
                )
                con.commit()
            finally:
                con.close()
        return True

    def rollback(self, name: str, regime: str = "all") -> bool:
        """Restore the previous value.  Returns True if a rollback was possible."""
        with self._lock:
            con = sqlite3.connect(self._db)
            try:
                cur = con.cursor()
                cur.execute(
                    "SELECT prev_value FROM adaptive_params "
                    "WHERE param_name=? AND regime=? ORDER BY id DESC LIMIT 1",
                    (name, regime),
                )
                row = cur.fetchone()
            finally:
                con.close()

        if row is None or row[0] is None:
            return False
        return self.set(
            name, float(row[0]),
            reason="rollback — performance degraded after previous change",
            regime=regime,
        )

    # ── History ───────────────────────────────────────────────────────────────

    def get_history(self, name: str, regime: str = "all", limit: int = 20) -> list[dict]:
        """Return the N most recent rows for a parameter, newest first."""
        with self._lock:
            con = sqlite3.connect(self._db)
            try:
                cur = con.cursor()
                cur.execute(
                    """SELECT current_value, prev_value, updated_at, update_reason,
                              perf_before, n_trades, version, confirmed
                       FROM adaptive_params
                       WHERE param_name=? AND regime=?
                       ORDER BY id DESC LIMIT ?""",
                    (name, regime, limit),
                )
                rows = cur.fetchall()
            finally:
                con.close()
        return [
            {
                "value":       float(r[0]),
                "prev":        float(r[1]) if r[1] is not None else None,
                "at":          r[2],
                "reason":      r[3],
                "perf_before": json.loads(r[4]) if r[4] else None,
                "n_trades":    r[5],
                "version":     r[6],
                "confirmed":   bool(r[7]),
            }
            for r in rows
        ]

    def log_optimization(
        self,
        pair: str,
        regime: str,
        n_trades: int,
        changes: dict,
        perf_before: dict,
        accepted: bool,
        reason: str,
    ) -> None:
        with self._lock:
            con = sqlite3.connect(self._db)
            try:
                con.execute(
                    """INSERT INTO optimization_log
                       (run_at, pair, regime, n_trades, changes_json,
                        perf_before, accepted, reason)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        _now(), pair, regime, n_trades,
                        json.dumps(changes), json.dumps(perf_before),
                        1 if accepted else 0, reason,
                    ),
                )
                con.commit()
            finally:
                con.close()

    def get_optimization_log(self, limit: int = 20) -> list[dict]:
        with self._lock:
            con = sqlite3.connect(self._db)
            try:
                cur = con.cursor()
                cur.execute(
                    "SELECT run_at, pair, regime, n_trades, changes_json, "
                    "perf_before, accepted, reason FROM optimization_log "
                    "ORDER BY id DESC LIMIT ?",
                    (limit,),
                )
                rows = cur.fetchall()
            finally:
                con.close()
        return [
            {
                "at": r[0], "pair": r[1], "regime": r[2], "n_trades": r[3],
                "changes": json.loads(r[4]) if r[4] else {},
                "perf_before": json.loads(r[5]) if r[5] else {},
                "accepted": bool(r[6]), "reason": r[7],
            }
            for r in rows
        ]

    # ── Snapshot ─────────────────────────────────────────────────────────────

    def snapshot(self, regime: str = "all") -> dict[str, dict]:
        """Current state vs defaults — useful for reporting and debugging."""
        result = {}
        for name, defn in self.DEFAULTS.items():
            current = self.get(name, regime)
            default = defn["v"]
            result[name] = {
                "current":  current,
                "default":  default,
                "changed":  current is not None and abs(current - default) > 1e-9,
                "min":      defn["min"],
                "max":      defn["max"],
                "step":     defn["step"],
            }
        return result

    def param_names(self) -> list[str]:
        return list(self.DEFAULTS.keys())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
