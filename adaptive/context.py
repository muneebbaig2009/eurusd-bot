"""Trade context recording — capture the full market state when a signal fires.

Every signal that reaches storage.log_signal() gets a corresponding row in
trade_context.  When the trade closes, update_outcome() fills in the result
columns (r_multiple, pnl, hold_hours, won).

This table becomes the learning dataset for the adaptive engine.
"""
import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

from adaptive.param_store import _db_lock

# ── Indicator value extractor ─────────────────────────────────────────────────
# Imported lazily inside the function to avoid circular imports.

def capture_context(
    db: str,
    signal_id: int,
    sig: dict,
    pair: str,
    regime: str = "unknown",
) -> None:
    """Write one trade_context row for a newly opened signal.

    sig is the dict returned by signal_engine.generate_signal().
    It may carry an optional '_ctx' key with raw indicator values.
    """
    ctx = sig.get("_ctx", {})
    indicator_vals = ctx.get("indicator_values", {})
    adaptive_params = ctx.get("adaptive_params", {})
    weights = ctx.get("weights", {})

    votes = sig.get("contributors", {})
    adx   = sig.get("trend_strength")
    session = _infer_session()

    row = (
        signal_id,
        pair,
        sig.get("direction", ""),
        regime,
        session,
        sig.get("timeframe", ""),
        sig.get("entry"),
        indicator_vals.get("atr"),
        adx,
        indicator_vals.get("rsi"),
        indicator_vals.get("macd_hist"),
        int(indicator_vals.get("ema_above", 0)) if indicator_vals.get("ema_above") is not None else None,
        indicator_vals.get("stoch_k"),
        indicator_vals.get("stoch_d"),
        indicator_vals.get("bb_pct"),
        indicator_vals.get("supertrend_dir"),
        sig.get("score"),
        sig.get("confidence"),
        json.dumps(votes),
        json.dumps(weights),
        json.dumps(adaptive_params),
        _now(),
    )
    lock = _db_lock(db)
    with lock:
        con = sqlite3.connect(db)
        try:
            con.execute(
                """INSERT OR IGNORE INTO trade_context
                   (signal_id, pair, direction, regime, session, timeframe,
                    entry, atr, adx, rsi, macd_hist, ema_above,
                    stoch_k, stoch_d, bb_pct, supertrend_dir,
                    score, confidence, votes_json, weights_json, params_json,
                    created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                row,
            )
            con.commit()
        except Exception:
            pass  # never let context recording crash the bot
        finally:
            con.close()


def update_outcome(
    db: str,
    signal_id: int,
    status: str,
    close_price: float,
    entry: float,
    sl: float,
    pnl: float,
    hold_hours: float,
) -> None:
    """Fill in result columns once a trade closes (WIN/LOSS/BREAKEVEN)."""
    # R-multiple: how many risk units did we gain or lose?
    # Positive on WIN, negative on LOSS, 0 on BREAKEVEN.
    risk = abs(entry - sl) if sl and abs(entry - sl) > 1e-9 else 1.0
    if status == "WIN":
        r_mult = abs(close_price - entry) / risk
    elif status == "LOSS":
        r_mult = -abs(close_price - entry) / risk
    else:
        r_mult = 0.0

    won = 1 if status == "WIN" else 0

    lock = _db_lock(db)
    with lock:
        con = sqlite3.connect(db)
        try:
            con.execute(
                """UPDATE trade_context
                   SET r_multiple=?, pnl=?, hold_hours=?, won=?
                   WHERE signal_id=?""",
                (round(r_mult, 4), round(pnl, 4),
                 round(hold_hours, 2), won, signal_id),
            )
            con.commit()
        except Exception:
            pass
        finally:
            con.close()


def get_recent_contexts(db: str, n: int = 100) -> list[dict]:
    """Return the N most recent trade_context rows that have outcomes filled in."""
    lock = _db_lock(db)
    with lock:
        con = sqlite3.connect(db)
        try:
            cur = con.cursor()
            cur.execute(
                """SELECT signal_id, pair, direction, regime, session, timeframe,
                          entry, atr, adx, rsi, macd_hist, ema_above,
                          stoch_k, stoch_d, bb_pct, supertrend_dir,
                          score, confidence, votes_json, weights_json, params_json,
                          r_multiple, pnl, hold_hours, won, created_at
                   FROM trade_context
                   WHERE won IS NOT NULL
                   ORDER BY id DESC LIMIT ?""",
                (n,),
            )
            rows = cur.fetchall()
        finally:
            con.close()

    result = []
    cols = [
        "signal_id", "pair", "direction", "regime", "session", "timeframe",
        "entry", "atr", "adx", "rsi", "macd_hist", "ema_above",
        "stoch_k", "stoch_d", "bb_pct", "supertrend_dir",
        "score", "confidence", "votes_json", "weights_json", "params_json",
        "r_multiple", "pnl", "hold_hours", "won", "created_at",
    ]
    for r in rows:
        d = dict(zip(cols, r))
        for k in ("votes_json", "weights_json", "params_json"):
            try:
                d[k] = json.loads(d[k]) if d[k] else {}
            except Exception:
                d[k] = {}
        result.append(d)
    return result


def get_all_contexts(db: str) -> list[dict]:
    """Return every row with an outcome — used for full history reports."""
    return get_recent_contexts(db, n=10_000)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _infer_session() -> str:
    """Map current UTC hour to the session name used by the bot."""
    h = datetime.now(timezone.utc).hour
    if 12 <= h <= 16:
        return "overlap"
    if 7 <= h < 12:
        return "london"
    if 17 <= h <= 20:
        return "ny"
    return "asian"
