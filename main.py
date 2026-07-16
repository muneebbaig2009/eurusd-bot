"""Main entry point. Runs one full cycle for every configured pair.

Always uses MT5 as the data and execution source — the MT5 terminal must be
open and logged in before calling run_pair().

Trade lifecycle: OPEN -> WIN (TP hit) | LOSS (SL hit)
Lot size is calculated at signal creation (2% of balance) and stored.
P&L = lot_size x pip_move x pip_value_per_lot  (MT5-style).
"""
import traceback
from datetime import datetime, timezone
import config
import storage
import signal_engine
import learner
import discord_poster
import exporter


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


def check_open_signals(symbol, db):
    """Query MT5 terminal to detect closed positions."""
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


def _session_active(pair_cfg: dict) -> bool:
    """Return True if the current UTC hour is within the configured trading session."""
    sess = pair_cfg.get("SESSION_FILTER", "all")
    if not sess or sess == "all":
        return True
    now_hour = datetime.now(timezone.utc).hour
    hours = config.SESSION_HOURS.get(sess, set(range(24)))
    return now_hour in hours


def try_new_signal(symbol, db, timeframes):
    """Returns a cycle_status dict describing why a signal was or wasn't generated."""
    status = {"checked_at": datetime.now(timezone.utc).isoformat()}

    if storage.has_open_signal(db):
        status.update(signal_result="blocked_open",
                      reason="Open position exists — waiting for it to close")
        print(f"[{symbol}] Open signal exists; not generating a new one.")
        return status

    pair_cfg = config.get_pair_config(symbol)

    # Session filter: only signal during configured market hours
    if not _session_active(pair_cfg):
        sess = pair_cfg.get("SESSION_FILTER", "all")
        status.update(signal_result="blocked_session",
                      reason=f"Outside {sess} session (UTC hour {datetime.now(timezone.utc).hour})")
        print(f"[{symbol}] Outside trading session ({sess}) — skipping signal check.")
        return status

    last_close = storage.last_close_time(db)
    if last_close is not None:
        now_utc = datetime.now(timezone.utc)
        if last_close.tzinfo is None:
            last_close = last_close.replace(tzinfo=timezone.utc)
        elapsed  = (now_utc - last_close).total_seconds() / 3600
        cooldown = config.cooldown_hours(symbol)   # bars × bar_size_hours
        if elapsed < cooldown:
            remaining = round(cooldown - elapsed, 1)
            status.update(signal_result="blocked_cooldown",
                          cooldown_remaining_h=remaining,
                          reason=f"Cooldown: {remaining}h remaining after last close")
            print(f"[{symbol}] Cooldown active — {remaining}h remaining.")
            return status

    sig = signal_engine.generate_signal(db, timeframes, cfg=pair_cfg)
    if sig is None:
        status.update(signal_result="no_conditions",
                      reason="No signal conditions met this cycle (score/ADX/confidence below thresholds)")
        print(f"[{symbol}] No valid signal this cycle.")
        return status

    import mt5_executor
    acct    = mt5_executor.get_account_info()
    balance = acct.get("balance", config.DEMO_INITIAL_BALANCE) if acct else config.DEMO_INITIAL_BALANCE
    sig["lot_size"] = storage.calc_lot_size(balance, sig["entry"], sig["sl"])
    sid = storage.log_signal(db, sig)
    print(f"[{symbol}] Signal #{sid}: {sig['direction']} @ {sig['entry']} "
          f"| Lots {sig['lot_size']} (2% of ${balance:,.2f}, conf {sig['confidence']}%, R:R 1:{sig['rr']})")
    tp = sig.get("tp1") or sig["tp"]
    ticket = mt5_executor.open_trade(
        symbol, sig["direction"], sig["lot_size"],
        sl=sig["sl"], tp=tp, magic=sid)
    if ticket:
        storage.set_mt5_ticket(db, sid, ticket)
        # Send Discord AFTER order is confirmed — includes the MT5 ticket
        discord_poster.post_signal(sig, sid, symbol=symbol,
                                   demo_balance=balance, mt5_ticket=ticket)
        status.update(signal_result="generated", signal_id=sid,
                      direction=sig["direction"], entry=sig["entry"],
                      lot_size=sig["lot_size"], confidence=sig["confidence"],
                      reason=f"Signal #{sid} {sig['direction']} @ {sig['entry']} | {sig['lot_size']}L")
    else:
        print(f"[{symbol}] MT5 order failed — cancelling signal #{sid}")
        storage.close_signal(db, sid, "LOSS", sig["entry"])
        status.update(signal_result="order_failed",
                      reason="MT5 order rejected by broker — signal cancelled")
    return status


def run_pair(symbol):
    db = config.db_path(symbol)
    storage.init_db(db)
    try:
        import mt5_feed
        timeframes = mt5_feed.get_all_timeframes(symbol)
    except Exception as e:
        print(f"[{symbol}] Data fetch failed: {e}")
        traceback.print_exc()
        exporter.export(symbol, cycle_status={
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "signal_result": "data_error",
            "reason": f"MT5 data fetch failed: {e}",
        })
        return
    check_open_signals(symbol, db)
    cycle_status = try_new_signal(symbol, db, timeframes)

    # Attach current price from primary timeframe
    try:
        primary_tf = config.get_pair_config(symbol).get("PRIMARY_TF", config.PRIMARY_TF)
        df = timeframes.get(primary_tf)
        if df is not None and not df.empty:
            cycle_status["current_price"] = round(float(df["close"].iloc[-1]), 5)
    except Exception:
        pass

    exporter.export(symbol, cycle_status=cycle_status)
    print(f"[{symbol}] Cycle complete. Weights:", storage.all_weights(db))


def main():
    for symbol in config.PAIRS:
        run_pair(symbol)


if __name__ == "__main__":
    main()
