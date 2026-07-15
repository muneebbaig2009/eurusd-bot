"""Main entry point. Runs one full cycle for every configured pair.

Two execution modes (set via EXECUTION_MODE env var):
  sim  — Twelve Data feed, internal SL/TP scanning, no real orders (GitHub Actions default)
  mt5  — MT5 terminal feed, real demo orders placed and tracked via MT5 API

Trade lifecycle: OPEN -> WIN (TP hit) | LOSS (SL hit)
Lot size is calculated at signal creation (2% of balance) and stored.
P&L = lot_size x pip_move x pip_value_per_lot  (MT5-style).
"""
import traceback
from datetime import datetime, timezone
import pandas as pd
import config
import storage
import signal_engine
import learner
import discord_poster
import exporter


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _close_and_notify(symbol, db, s, status, close_price):
    """Close signal in DB, update learner + demo account, post to Discord."""
    if not storage.close_signal(db, s["id"], status, close_price):
        print(f"[{symbol}] #{s['id']} already closed; skipping.")
        return
    learner.update_weights(db, s["contributors"], s["direction"], won=(status == "WIN"))
    new_bal, pnl = storage.record_demo_trade(
        db, s["id"], s["direction"], status,
        entry=s["entry"], sl=s["sl"], close_price=close_price,
        lot_size=s.get("lot_size"))
    discord_poster.post_result(
        s["id"], s["direction"], status, s["entry"], close_price,
        storage.stats(db), symbol=symbol,
        pnl=pnl, new_balance=new_bal, lot_size=s.get("lot_size"))
    pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"${pnl:.2f}"
    print(f"[{symbol}] #{s['id']} {status} @ {close_price} "
          f"| Lots {s.get('lot_size')} | P&L {pnl_str} | Balance ${new_bal:.2f}")


# ---------------------------------------------------------------------------
# SIM mode — scan Twelve Data candles for SL/TP
# ---------------------------------------------------------------------------

def _candles_since(df5, created_at):
    try:
        ts = pd.to_datetime(created_at)
        if ts.tzinfo is not None:
            ts = ts.tz_convert(None) if hasattr(ts, "tz_convert") else ts.tz_localize(None)
    except Exception:
        return df5
    idx = df5.index
    try:
        if getattr(idx, "tz", None) is not None:
            idx = idx.tz_localize(None)
            df5 = df5.copy()
            df5.index = idx
    except Exception:
        pass
    return df5[df5.index >= ts]


def _check_open_sim(symbol, db, timeframes):
    """Sim mode: scan 5min candles to detect TP/SL hits."""
    df5 = timeframes["5min"]
    last_price = float(df5["close"].iloc[-1])

    for s in storage.open_signals(db):
        window = _candles_since(df5, s.get("created_at"))
        if window.empty:
            window = df5.tail(1)

        hit        = None
        close_price = last_price

        for _, row in window.iterrows():
            hi, lo = float(row["high"]), float(row["low"])
            if s["direction"] == "BUY":
                if lo <= s["sl"]:
                    hit, close_price = "LOSS", s["sl"]; break
                if hi >= s["tp"]:
                    hit, close_price = "WIN",  s["tp"]; break
            else:
                if hi >= s["sl"]:
                    hit, close_price = "LOSS", s["sl"]; break
                if lo <= s["tp"]:
                    hit, close_price = "WIN",  s["tp"]; break

        if hit:
            _close_and_notify(symbol, db, s, hit, close_price)


# ---------------------------------------------------------------------------
# MT5 mode — query terminal for position status
# ---------------------------------------------------------------------------

def _check_open_mt5(symbol, db):
    """MT5 mode: ask terminal whether each tracked position has closed."""
    import mt5_executor
    for s in storage.open_signals(db):
        ticket = s.get("mt5_ticket")
        if ticket is None:
            continue
        result = mt5_executor.get_position_result(ticket)
        if result is None:
            continue
        status, close_price = result
        _close_and_notify(symbol, db, s, status, close_price)


# ---------------------------------------------------------------------------
# Public API called by run_pair()
# ---------------------------------------------------------------------------

def check_open_signals(symbol, db, timeframes):
    if config.EXECUTION_MODE == "mt5":
        _check_open_mt5(symbol, db)
    else:
        _check_open_sim(symbol, db, timeframes)


def try_new_signal(symbol, db, timeframes):
    if storage.has_open_signal(db):
        print(f"[{symbol}] Open signal exists; not generating a new one.")
        return
    last_close = storage.last_close_time(db)
    if last_close is not None:
        now_utc = datetime.now(timezone.utc)
        if last_close.tzinfo is None:
            last_close = last_close.replace(tzinfo=timezone.utc)
        elapsed = (now_utc - last_close).total_seconds() / 3600
        if elapsed < config.SIGNAL_COOLDOWN_BARS:
            print(f"[{symbol}] Cooldown active — "
                  f"{config.SIGNAL_COOLDOWN_BARS - elapsed:.1f}h remaining.")
            return

    sig = signal_engine.generate_signal(db, timeframes)
    if sig is None:
        print(f"[{symbol}] No valid signal this cycle.")
        return

    demo_balance = storage.get_demo_stats(db)["balance"]
    sig["lot_size"] = storage.calc_lot_size(demo_balance, sig["entry"], sig["sl"])
    sid = storage.log_signal(db, sig)
    discord_poster.post_signal(sig, sid, symbol=symbol, demo_balance=demo_balance)
    print(f"[{symbol}] Posted #{sid}: {sig['direction']} @ {sig['entry']} "
          f"| Lots {sig['lot_size']} (conf {sig['confidence']}%, R:R 1:{sig['rr']})")

    if config.EXECUTION_MODE == "mt5":
        import mt5_executor
        tp = sig.get("tp1") or sig["tp"]
        ticket = mt5_executor.open_trade(
            symbol, sig["direction"], sig["lot_size"],
            sl=sig["sl"], tp=tp, magic=sid)
        if ticket:
            storage.set_mt5_ticket(db, sid, ticket)
        else:
            print(f"[{symbol}] MT5 order failed — cancelling signal #{sid}")
            storage.close_signal(db, sid, "LOSS", sig["entry"])


def run_pair(symbol):
    db = config.db_path(symbol)
    storage.init_db(db)
    try:
        if config.EXECUTION_MODE == "mt5":
            import mt5_feed
            timeframes = mt5_feed.get_all_timeframes(symbol)
        else:
            import data_feed
            timeframes = data_feed.get_all_timeframes(symbol)
    except Exception as e:
        print(f"[{symbol}] Data fetch failed: {e}")
        traceback.print_exc()
        return
    check_open_signals(symbol, db, timeframes)
    try_new_signal(symbol, db, timeframes)
    exporter.export(symbol)
    print(f"[{symbol}] Cycle complete. Weights:", storage.all_weights(db))


def main():
    for symbol in config.PAIRS:
        run_pair(symbol)


if __name__ == "__main__":
    main()
