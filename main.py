"""Main entry point. Runs one full cycle for every configured pair.

For each pair:
  1. Check open signals against ALL 5min candles since the signal opened
     (so a brief TP/SL touch between runs is never missed).
  2. If no open signal, try to generate a new one; if valid, log + post.
  3. Export that pair's dashboard JSON.

Trade lifecycle: OPEN -> WIN (TP hit) | LOSS (SL hit)
Lot size is calculated at signal creation and stored with the signal.
P&L = lot_size x pip_move x pip_value_per_lot  (MT5-style).
"""
import traceback
from datetime import datetime, timezone, timedelta
import pandas as pd
import config
import storage
import data_feed
import signal_engine
import learner
import discord_poster
import exporter


def _candles_since(df5, created_at):
    """Return the 5min candles at/after the signal's creation time."""
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


def check_open_signals(symbol, db, timeframes):
    """Scan every 5min candle since each signal opened for a TP or SL touch.

    WIN  = TP1 hit
    LOSS = SL hit
    Duplicate-safe: close_signal() returns False if another CI run already closed it.
    """
    df5_full = timeframes["5min"]
    last_price = float(df5_full["close"].iloc[-1])

    for s in storage.open_signals(db):
        window = _candles_since(df5_full, s.get("created_at"))
        if window.empty:
            window = df5_full.tail(1)

        hit = None
        close_price = last_price

        for _, row in window.iterrows():
            hi, lo = float(row["high"]), float(row["low"])
            if s["direction"] == "BUY":
                if lo <= s["sl"]:
                    hit, close_price = "LOSS", s["sl"]; break
                if hi >= s["tp"]:
                    hit, close_price = "WIN", s["tp"]; break
            else:  # SELL
                if hi >= s["sl"]:
                    hit, close_price = "LOSS", s["sl"]; break
                if lo <= s["tp"]:
                    hit, close_price = "WIN", s["tp"]; break

        if hit and storage.close_signal(db, s["id"], hit, close_price):
            learner.update_weights(db, s["contributors"], s["direction"],
                                   won=(hit == "WIN"))
            new_bal, pnl = storage.record_demo_trade(
                db, s["id"], s["direction"], hit,
                entry=s["entry"], sl=s["sl"], close_price=close_price,
                lot_size=s.get("lot_size"))
            discord_poster.post_result(
                s["id"], s["direction"], hit, s["entry"], close_price,
                storage.stats(db), symbol=symbol,
                pnl=pnl, new_balance=new_bal,
                lot_size=s.get("lot_size"),
            )
            pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"${pnl:.2f}"
            print(f"[{symbol}] Signal #{s['id']} -> {hit} @ {close_price} "
                  f"| Lots {s.get('lot_size')} | P&L {pnl_str} | Balance ${new_bal:.2f}")
        elif hit:
            print(f"[{symbol}] Signal #{s['id']} already closed by another run; skipping.")


def try_new_signal(symbol, db, timeframes):
    if storage.has_open_signal(db):
        print(f"[{symbol}] Open signal exists; not generating a new one.")
        return
    last_close = storage.last_close_time(db)
    if last_close is not None:
        now_utc = datetime.now(timezone.utc)
        if last_close.tzinfo is None:
            last_close = last_close.replace(tzinfo=timezone.utc)
        cooldown_hours = config.SIGNAL_COOLDOWN_BARS
        elapsed = (now_utc - last_close).total_seconds() / 3600
        if elapsed < cooldown_hours:
            remaining = cooldown_hours - elapsed
            print(f"[{symbol}] Cooldown active — {remaining:.1f}h remaining after last close.")
            return
    sig = signal_engine.generate_signal(db, timeframes)
    if sig is None:
        print(f"[{symbol}] No valid signal this cycle.")
        return
    # Calculate MT5 lot size based on current demo balance
    demo_stats = storage.get_demo_stats(db)
    demo_balance = demo_stats["balance"]
    sig["lot_size"] = storage.calc_lot_size(demo_balance, sig["entry"], sig["sl"])
    sid = storage.log_signal(db, sig)
    discord_poster.post_signal(sig, sid, symbol=symbol, demo_balance=demo_balance)
    print(f"[{symbol}] Posted #{sid}: {sig['direction']} @ {sig['entry']} "
          f"| Lots {sig['lot_size']} (conf {sig['confidence']}%, R:R 1:{sig['rr']})")


def run_pair(symbol):
    db = config.db_path(symbol)
    storage.init_db(db)
    try:
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
