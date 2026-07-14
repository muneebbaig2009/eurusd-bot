"""Main entry point. Runs one full cycle for every configured pair.

Dual-trade strategy:
  Each signal creates TWO trades with the same pair_id.
  Trade 1 (trade_num=1): entry → TP1 | SL  (lot = half of 2% risk)
  Trade 2 (trade_num=2): entry → TP2 | SL  (lot = half of 2% risk)

  When TP1 hit  → Trade 1 closes WIN; Trade 2 SL moves to entry (breakeven)
  When SL hit   → both trades close LOSS (via close_pair_partner)
  Trade 2 final → WIN (TP2) or BREAKEVEN (SL=entry touched)

P&L = lot_size × pip_move × pip_value_per_lot  (MT5-style).
"""
import traceback
from datetime import datetime, timezone
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
    """Scan every 5min candle since each trade opened for a TP or SL touch.

    Dual-trade path:
      Trade 1 WIN  → TP1 hit; Trade 2 SL moved to entry; Discord partial-win alert
      LOSS         → both trades closed; Discord both-SL alert
      Trade 2 WIN  → TP2 hit; normal WIN alert
      Trade 2 BE   → breakeven (SL=entry touched); Discord breakeven alert

    Duplicate-safe: close_signal() returns False if already closed by another run.
    """
    df5 = timeframes["5min"]
    last_price = float(df5["close"].iloc[-1])

    for s in storage.open_signals(db):
        window = _candles_since(df5, s.get("created_at"))
        if window.empty:
            window = df5.tail(1)

        hit        = None
        close_price = last_price
        is_be      = s["breakeven_active"]

        for _, row in window.iterrows():
            hi, lo = float(row["high"]), float(row["low"])
            if s["direction"] == "BUY":
                if lo <= s["sl"]:
                    hit         = "BREAKEVEN" if is_be else "LOSS"
                    close_price = s["sl"]; break
                if hi >= s["tp"]:
                    hit, close_price = "WIN", s["tp"]; break
            else:  # SELL
                if hi >= s["sl"]:
                    hit         = "BREAKEVEN" if is_be else "LOSS"
                    close_price = s["sl"]; break
                if lo <= s["tp"]:
                    hit, close_price = "WIN", s["tp"]; break

        if not hit:
            continue

        trade_num = s["trade_num"]
        pair_id   = s["pair_id"]
        is_paired = pair_id is not None

        # ── Trade 1 WIN (TP1 hit) ─────────────────────────────────────────
        if hit == "WIN" and trade_num == 1 and is_paired:
            if not storage.close_signal(db, s["id"], "WIN", close_price):
                print(f"[{symbol}] T1 #{s['id']} already closed by another run.")
                continue
            learner.update_weights(db, s["contributors"], s["direction"], won=True)
            new_bal, pnl = storage.record_demo_trade(
                db, s["id"], s["direction"], "WIN",
                entry=s["entry"], sl=s["sl"], close_price=close_price,
                lot_size=s.get("lot_size"))
            moved = storage.move_pair_to_breakeven(db, pair_id, s["id"], s["entry"])
            discord_poster.post_tp1_win(
                s["id"], s["direction"], s["entry"], close_price,
                s.get("tp2"), pnl, new_bal,
                moved_t2=moved, symbol=symbol)
            pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"${pnl:.2f}"
            print(f"[{symbol}] T1 #{s['id']} TP1 WIN @ {close_price} "
                  f"| Lots {s.get('lot_size')} | P&L {pnl_str} | Balance ${new_bal:.2f} "
                  f"| T2 SL→entry: {moved}")

        # ── SL hit (before TP1): close BOTH trades ────────────────────────
        elif hit == "LOSS":
            if not storage.close_signal(db, s["id"], "LOSS", close_price):
                print(f"[{symbol}] #{s['id']} already closed by another run.")
                continue
            new_bal, pnl1 = storage.record_demo_trade(
                db, s["id"], s["direction"], "LOSS",
                entry=s["entry"], sl=s["sl"], close_price=close_price,
                lot_size=s.get("lot_size"))
            total_pnl = pnl1

            if is_paired:
                partner = storage.close_pair_partner(db, pair_id, s["id"], "LOSS", close_price)
                if partner:
                    new_bal, pnl2 = storage.record_demo_trade(
                        db, partner["id"], partner["direction"], "LOSS",
                        entry=partner["entry"], sl=partner["sl"],
                        close_price=close_price, lot_size=partner.get("lot_size"))
                    total_pnl = round(pnl1 + pnl2, 2)
                learner.update_weights(db, s["contributors"], s["direction"], won=False)
                discord_poster.post_pair_sl(
                    s["id"], s["direction"], close_price,
                    total_pnl, new_bal, symbol=symbol)
                print(f"[{symbol}] Pair #{pair_id} BOTH LOSS @ {close_price} "
                      f"| Total P&L ${total_pnl:.2f} | Balance ${new_bal:.2f}")
            else:
                # Legacy single trade
                learner.update_weights(db, s["contributors"], s["direction"], won=False)
                discord_poster.post_result(
                    s["id"], s["direction"], "LOSS", s["entry"], close_price,
                    storage.stats(db), symbol=symbol,
                    pnl=total_pnl, new_balance=new_bal, lot_size=s.get("lot_size"))
                print(f"[{symbol}] #{s['id']} LOSS @ {close_price} "
                      f"| P&L ${total_pnl:.2f} | Balance ${new_bal:.2f}")

        # ── Trade 2 WIN (TP2) or BREAKEVEN ────────────────────────────────
        else:
            if not storage.close_signal(db, s["id"], hit, close_price):
                print(f"[{symbol}] T2 #{s['id']} already closed by another run.")
                continue
            if hit == "WIN":
                learner.update_weights(db, s["contributors"], s["direction"], won=True)
            new_bal, pnl = storage.record_demo_trade(
                db, s["id"], s["direction"], hit,
                entry=s["entry"], sl=s["sl"], close_price=close_price,
                lot_size=s.get("lot_size"))
            discord_poster.post_result(
                s["id"], s["direction"], hit, s["entry"], close_price,
                storage.stats(db), symbol=symbol,
                pnl=pnl, new_balance=new_bal, lot_size=s.get("lot_size"),
                trade_num=trade_num)
            pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"${pnl:.2f}"
            print(f"[{symbol}] T2 #{s['id']} {hit} @ {close_price} "
                  f"| Lots {s.get('lot_size')} | P&L {pnl_str} | Balance ${new_bal:.2f}")


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
            print(f"[{symbol}] Cooldown active — {remaining:.1f}h remaining.")
            return

    sig = signal_engine.generate_signal(db, timeframes)
    if sig is None:
        print(f"[{symbol}] No valid signal this cycle.")
        return

    # Lot size = half of 2% risk per trade (total risk = 2%)
    demo_stats   = storage.get_demo_stats(db)
    demo_balance = demo_stats["balance"]
    sig["lot_size"] = storage.calc_lot_size(
        demo_balance, sig["entry"], sig["sl"],
        risk_pct=config.DEMO_RISK_PCT / 2)

    tp2 = sig.get("tp2")
    if tp2:
        id1, id2 = storage.log_signal_pair(db, sig)
        discord_poster.post_signal(
            sig, id1, id2=id2, symbol=symbol, demo_balance=demo_balance)
        print(f"[{symbol}] Dual-trade pair #{id1}+{id2}: {sig['direction']} "
              f"@ {sig['entry']} | Lots {sig['lot_size']} each "
              f"| TP1 {sig['tp1']} | TP2 {tp2} | SL {sig['sl']} "
              f"(conf {sig['confidence']}%, R:R 1:{sig['rr']})")
    else:
        # No TP2: single trade (rare edge case)
        sig["lot_size"] = storage.calc_lot_size(demo_balance, sig["entry"], sig["sl"])
        sid = storage.log_signal_pair(db, sig)[0]
        discord_poster.post_signal(sig, sid, symbol=symbol, demo_balance=demo_balance)
        print(f"[{symbol}] Single-trade #{sid}: {sig['direction']} @ {sig['entry']} "
              f"| Lots {sig['lot_size']} | SL {sig['sl']}")


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
