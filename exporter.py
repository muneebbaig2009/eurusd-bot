"""Export the SQLite data to docs/data.json so the static dashboard can read it.
Called automatically at the end of every bot cycle."""
import json
import sqlite3
from datetime import datetime, timezone
import config


def export(symbol):
    db = config.db_path(symbol)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # All signals, newest first
    cur.execute("""
        SELECT id, created_at, direction, entry, tp, sl, score,
               contributors, status, closed_at, close_price,
               tp1, tp2, confidence, rr, trend_strength, timeframe
        FROM signals ORDER BY id DESC
    """)
    signals = []
    for r in cur.fetchall():
        signals.append({
            "id": r["id"],
            "created_at": r["created_at"],
            "direction": r["direction"],
            "entry": r["entry"],
            "tp": r["tp"],
            "sl": r["sl"],
            "score": r["score"],
            "contributors": json.loads(r["contributors"]),
            "status": r["status"],
            "closed_at": r["closed_at"],
            "close_price": r["close_price"],
            "tp1": r["tp1"],
            "tp2": r["tp2"],
            "confidence": r["confidence"],
            "rr": r["rr"],
            "trend_strength": r["trend_strength"],
            "timeframe": r["timeframe"],
        })

    # Weights
    cur.execute("SELECT technique, weight FROM weights ORDER BY technique")
    weights = {row["technique"]: row["weight"] for row in cur.fetchall()}

    # Stats
    cur.execute("SELECT status, COUNT(*) c FROM signals GROUP BY status")
    by_status = {row["status"]: row["c"] for row in cur.fetchall()}
    wins = by_status.get("WIN", 0)
    losses = by_status.get("LOSS", 0)
    closed = wins + losses
    win_rate = round(wins / closed * 100, 1) if closed else 0.0

    # Equity curve: +1 per win, -1 per loss in chronological order
    cur.execute("""
        SELECT status FROM signals
        WHERE status IN ('WIN','LOSS') ORDER BY id ASC
    """)
    equity = []
    running = 0
    for row in cur.fetchall():
        running += 1 if row["status"] == "WIN" else -1
        equity.append(running)

    con.close()

    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "stats": {
            "wins": wins,
            "losses": losses,
            "open": by_status.get("OPEN", 0),
            "total": len(signals),
            "win_rate": win_rate,
        },
        "weights": weights,
        "equity": equity,
        "signals": signals,
    }

    out_path = config.data_json_path(symbol)
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"[export] {out_path} written")


if __name__ == "__main__":
    for sym in config.PAIRS:
        export(sym)
