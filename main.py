"""Main entry point. Runs one full cycle for every configured pair.

For each pair:
  1. Check open signals against ALL 5min candles since the signal opened
     (so a brief TP/SL touch between runs is never missed).
  2. If no open signal, try to generate a new one; if valid, log + post.
  3. Export that pair's dashboard JSON.
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
    """Scan 5min candles for each open signal.

    Phase 1 (OPEN):    watch original SL and TP1.
    Phase 2 (TP1_HIT): TP1 already touched → watch entry (breakeven SL) and TP2.
    Outcomes: WIN | LOSS | BREAKEVEN | TP1_HIT (transition)
    """
    df5_full = timeframes["5min"]
    last_price = float(df5_full["close"].iloc[-1])

    for s in storage.open_signals(db):
        # Use tp1_hit_at as Phase 2 window start; fall back to created_at for Phase 1
        if s["status"] == "TP1_HIT":
            start_ts = s.get("tp1_hit_at") or s.get("created_at")
        else:
            start_ts = s.get("created_at")

        window = _candles_since(df5_full, start_ts)
        if window.empty:
            window = df5_full.tail(1)

        hit = None
        close_price = last_price

        for _, row in window.iterrows():
            hi, lo = float(row["high"]), float(row["low"])

            if s["status"] == "TP1_HIT":
                # Phase 2 — SL = entry (breakeven), TP = tp2
                tp2 = s.get("tp2")
                if tp2 is None:
                    break
                if s["direction"] == "BUY":
                    if lo <= s["entry"]:
                        hit, close_price = "BREAKEVEN", s["entry"]; break
                    if hi >= tp2:
                        hit, close_price = "WIN", tp2; break
                else:
                    if hi >= s["entry"]:
                        hit, close_price = "BREAKEVEN", s["entry"]; break
                    if lo <= tp2:
                        hit, close_price = "WIN", tp2; break
            else:
                # Phase 1 — SL = original sl, TP = tp1
                if s["direction"] == "BUY":
                    if lo <= s["sl"]:
                        hit, close_price = "LOSS", s["sl"]; break
                    if hi >= s["tp"]:
                        hit, close_price = "TP1_HIT", s["tp"]; break
                else:
                    if hi >= s["sl"]:
                        hit, close_price = "LOSS", s["sl"]; break
                    if lo <= s["tp"]:
                        hit, close_price = "TP1_HIT", s["tp"]; break

        if hit == "TP1_HIT":
            # Transition: not yet closed — move SL to entry, target TP2.
            # Only post Discord if this run was the one that changed the row
            # (prevents duplicate alerts when two CI runs overlap).
            if storage.set_tp1_hit(db, s["id"]):
                discord_poster.post_tp1_hit(
                    s["id"], s["direction"], s["entry"], s.get("tp2"), symbol=symbol)
                print(f"[{symbol}] Signal #{s['id']} -> TP1 HIT! SL now at entry "
                      f"{s['entry']}, targeting TP2 {s.get('tp2')}")

        elif hit in ("WIN", "LOSS", "BREAKEVEN"):
            # close_signal returns False if another run already closed it — skip Discord.
            if storage.close_signal(db, s["id"], hit, close_price):
                if hit != "BREAKEVEN":
                    learner.update_weights(
                        db, s["contributors"], s["direction"], won=(hit == "WIN"))
                new_bal, pnl = storage.record_demo_trade(
                    db, s["id"], s["direction"], hit,
                    entry=s["entry"], sl=s["sl"], close_price=close_price)
                discord_poster.post_result(
                    s["id"], s["direction"], hit, s["entry"], close_price,
                    storage.stats(db), symbol=symbol,
                    pnl=pnl, new_balance=new_bal,
                )
                pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"${pnl:.2f}"
                print(f"[{symbol}] Signal #{s['id']} -> {hit} @ {close_price} "
                      f"| Demo P&L {pnl_str} | Balance ${new_bal:.2f}")
            else:
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
        cooldown_hours = config.SIGNAL_COOLDOWN_BARS  # 1 bar = 1h on PRIMARY_TF
        elapsed = (now_utc - last_close).total_seconds() / 3600
        if elapsed < cooldown_hours:
            remaining = cooldown_hours - elapsed
            print(f"[{symbol}] Cooldown active — {remaining:.1f}h remaining after last close.")
            return
    sig = signal_engine.generate_signal(db, timeframes)
    if sig is None:
        print(f"[{symbol}] No valid signal this cycle.")
        return
    sid = storage.log_signal(db, sig)
    demo_balance = storage.get_demo_stats(db)["balance"]
    discord_poster.post_signal(sig, sid, symbol=symbol, demo_balance=demo_balance)
    print(f"[{symbol}] Posted #{sid}: {sig['direction']} @ {sig['entry']} "
          f"(conf {sig['confidence']}%, R:R 1:{sig['rr']})")


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
