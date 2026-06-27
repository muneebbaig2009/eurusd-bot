"""Main entry point. Runs one full cycle:
  1. Check open signals -> did they hit TP or SL? Close + learn + post result.
  2. If no open signal, generate a new one. If valid, log + post.

Run this on a schedule (GitHub Actions cron every ~5 min, or local loop).
"""
import traceback
import config
import storage
import data_feed
import signal_engine
import learner
import discord_poster
import exporter


def check_open_signals(timeframes):
    """Use the latest 5min candle high/low to decide if TP or SL was hit."""
    df5 = timeframes["5min"]
    recent_high = float(df5["high"].iloc[-1])
    recent_low = float(df5["low"].iloc[-1])
    last_price = float(df5["close"].iloc[-1])

    for s in storage.open_signals():
        hit = None
        close_price = last_price

        if s["direction"] == "BUY":
            if recent_low <= s["sl"]:
                hit, close_price = "LOSS", s["sl"]
            elif recent_high >= s["tp"]:
                hit, close_price = "WIN", s["tp"]
        else:  # SELL
            if recent_high >= s["sl"]:
                hit, close_price = "LOSS", s["sl"]
            elif recent_low <= s["tp"]:
                hit, close_price = "WIN", s["tp"]

        if hit:
            storage.close_signal(s["id"], hit, close_price)
            learner.update_weights(s["contributors"], s["direction"], won=(hit == "WIN"))
            discord_poster.post_result(
                s["id"], s["direction"], hit, s["entry"], close_price, storage.stats()
            )
            print(f"[result] Signal #{s['id']} -> {hit} @ {close_price}")


def try_new_signal(timeframes):
    if storage.has_open_signal():
        print("[signal] Open signal exists; not generating a new one.")
        return
    sig = signal_engine.generate_signal(timeframes)
    if sig is None:
        print("[signal] No valid signal this cycle.")
        return
    sid = storage.log_signal(
        sig["direction"], sig["entry"], sig["tp"], sig["sl"],
        sig["score"], sig["contributors"],
    )
    discord_poster.post_signal(sig, sid)
    print(f"[signal] Posted #{sid}: {sig['direction']} @ {sig['entry']}")


def main():
    storage.init_db()
    try:
        timeframes = data_feed.get_all_timeframes()
    except Exception as e:
        print(f"[error] Data fetch failed: {e}")
        traceback.print_exc()
        return

    check_open_signals(timeframes)
    try_new_signal(timeframes)
    exporter.export()
    print("[done] Cycle complete. Weights:", storage.all_weights())


if __name__ == "__main__":
    main()
