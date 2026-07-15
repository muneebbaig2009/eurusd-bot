"""Export the SQLite data to docs/data.json so the static dashboard can read it.
Called automatically at the end of every bot cycle."""
import json
import sqlite3
from datetime import datetime, timezone
import config
import storage


def export(symbol):
    db = config.db_path(symbol)
    storage.init_db(db)

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # All signals, newest first — include dual-trade columns
    cur.execute("""
        SELECT s.id, s.created_at, s.direction, s.entry, s.tp, s.sl, s.score,
               s.contributors, s.status, s.closed_at, s.close_price,
               s.tp1, s.tp2, s.confidence, s.rr, s.trend_strength, s.timeframe,
               s.lot_size, s.trade_num, s.pair_id, s.breakeven_active,
               da.pnl, da.balance_after
        FROM signals s
        LEFT JOIN demo_account da ON da.signal_id = s.id
        ORDER BY s.id DESC
    """)
    signals = []
    for r in cur.fetchall():
        signals.append({
            "id":               r["id"],
            "created_at":       r["created_at"],
            "direction":        r["direction"],
            "entry":            r["entry"],
            "tp":               r["tp"],
            "sl":               r["sl"],
            "score":            r["score"],
            "contributors":     json.loads(r["contributors"]),
            "status":           r["status"],
            "closed_at":        r["closed_at"],
            "close_price":      r["close_price"],
            "tp1":              r["tp1"],
            "tp2":              r["tp2"],
            "confidence":       r["confidence"],
            "rr":               r["rr"],
            "trend_strength":   r["trend_strength"],
            "timeframe":        r["timeframe"],
            "lot_size":         r["lot_size"],
            "trade_num":        r["trade_num"] or 1,
            "pair_id":          r["pair_id"],
            "breakeven_active": bool(r["breakeven_active"]),
            "pnl":              r["pnl"],
            "balance_after":    r["balance_after"],
        })

    # Weights
    cur.execute("SELECT technique, weight FROM weights ORDER BY technique")
    weights = {row["technique"]: row["weight"] for row in cur.fetchall()}

    cur.execute(
        "SELECT status, COUNT(*) c FROM signals "
        "WHERE status IN ('WIN','LOSS') GROUP BY status"
    )
    by_status  = {row["status"]: row["c"] for row in cur.fetchall()}
    wins   = by_status.get("WIN", 0)
    losses = by_status.get("LOSS", 0)
    closed = wins + losses

    cur.execute("SELECT COUNT(*) c FROM signals WHERE status='OPEN'")
    open_count = cur.fetchone()["c"]

    win_rate = round(wins / closed * 100, 1) if closed else 0.0

    # Equity curve: +1 WIN / -1 LOSS, cumulative
    cur.execute(
        "SELECT status FROM signals "
        "WHERE status IN ('WIN','LOSS') ORDER BY id ASC"
    )
    equity  = []
    running = 0
    for row in cur.fetchall():
        running += 1 if row["status"] == "WIN" else -1
        equity.append(running)

    con.close()

    demo = storage.get_demo_stats(db)

    # MT5 live account data (requires MT5 to be connected)
    mt5_account, mt5_trades = {}, []
    try:
        import MetaTrader5 as _mt5
        if _mt5.account_info() is not None:
            import mt5_executor
            mt5_account = mt5_executor.get_account_info()
            mt5_trades  = mt5_executor.get_trade_history(days=60)
    except Exception as _e:
        print(f"[export] MT5 account fetch skipped: {_e}")

    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "symbol":     symbol,
        "stats": {
            "wins":     wins,
            "losses":   losses,
            "open":     open_count,
            "total":    len(signals),
            "win_rate": win_rate,
        },
        "weights": weights,
        "equity":  equity,
        "demo":        demo,
        "mt5_account": mt5_account,
        "mt5_trades":  mt5_trades,
        "signals":     signals,
    }

    out_path = config.data_json_path(symbol)
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"[export] {out_path} written")


if __name__ == "__main__":
    for sym in config.PAIRS:
        export(sym)
