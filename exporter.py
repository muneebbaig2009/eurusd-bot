"""Export the SQLite data to docs/data.json so the static dashboard can read it.
Called automatically at the end of every bot cycle."""
import json
import sqlite3
from datetime import datetime, timezone
import config
import storage


def export(symbol):
    db = config.db_path(symbol)
    storage.init_db(db)   # creates tables if missing, back-fills demo account

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # All signals, newest first — LEFT JOIN demo_account for per-trade P&L and lot size
    cur.execute("""
        SELECT s.id, s.created_at, s.direction, s.entry, s.tp, s.sl, s.score,
               s.contributors, s.status, s.closed_at, s.close_price,
               s.tp1, s.tp2, s.confidence, s.rr, s.trend_strength, s.timeframe,
               s.lot_size,
               da.pnl, da.balance_after
        FROM signals s
        LEFT JOIN demo_account da ON da.signal_id = s.id
        ORDER BY s.id DESC
    """)
    signals = []
    for r in cur.fetchall():
        signals.append({
            "id":             r["id"],
            "created_at":     r["created_at"],
            "direction":      r["direction"],
            "entry":          r["entry"],
            "tp":             r["tp"],
            "sl":             r["sl"],
            "score":          r["score"],
            "contributors":   json.loads(r["contributors"]),
            "status":         r["status"],
            "closed_at":      r["closed_at"],
            "close_price":    r["close_price"],
            "tp1":            r["tp1"],
            "tp2":            r["tp2"],
            "confidence":     r["confidence"],
            "rr":             r["rr"],
            "trend_strength": r["trend_strength"],
            "timeframe":      r["timeframe"],
            "lot_size":       r["lot_size"],
            "pnl":            r["pnl"],
            "balance_after":  r["balance_after"],
        })

    # Weights
    cur.execute("SELECT technique, weight FROM weights ORDER BY technique")
    weights = {row["technique"]: row["weight"] for row in cur.fetchall()}

    # Stats (WIN / LOSS only)
    cur.execute("SELECT status, COUNT(*) c FROM signals GROUP BY status")
    by_status = {row["status"]: row["c"] for row in cur.fetchall()}
    wins   = by_status.get("WIN", 0)
    losses = by_status.get("LOSS", 0)
    closed = wins + losses
    win_rate = round(wins / closed * 100, 1) if closed else 0.0

    # Equity curve: +1 win / -1 loss, cumulative
    cur.execute("SELECT status FROM signals WHERE status IN ('WIN','LOSS') ORDER BY id ASC")
    equity  = []
    running = 0
    for row in cur.fetchall():
        running += 1 if row["status"] == "WIN" else -1
        equity.append(running)

    con.close()

    demo = storage.get_demo_stats(db)

    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "stats": {
            "wins":     wins,
            "losses":   losses,
            "open":     by_status.get("OPEN", 0),
            "total":    len(signals),
            "win_rate": win_rate,
        },
        "weights": weights,
        "equity":  equity,
        "demo":    demo,
        "signals": signals,
    }

    out_path = config.data_json_path(symbol)
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"[export] {out_path} written")


if __name__ == "__main__":
    for sym in config.PAIRS:
        export(sym)
