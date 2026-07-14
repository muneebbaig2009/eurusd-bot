"""Backtest the strategy over historical candles — no lookahead.

How it works:
  - Fetch a long history of 1h and 1day candles.
  - Walk forward one 1h candle at a time. At each step, the engine only sees
    candles up to *that* moment (a slice), exactly like live trading.
  - When a signal fires, simulate the trade forward using subsequent 1h candles
    to see whether TP1 or SL is hit first.
  - Record every trade and print performance stats.

Run:  python backtest.py                 (uses config.PAIRS[0])
      python backtest.py GBP/USD         (specific pair)
      python backtest.py EUR/USD 1500     (pair + how many 1h candles of history)

This uses an in-memory database so it never touches your live signals_*.db,
and it does NOT let weights learn during the test (weights stay at default),
so you measure the raw strategy, not a moving target. That keeps results honest.
"""
import sys
import os
import importlib.metadata  # noqa: F401
import tempfile
import pandas as pd

import config
import data_feed
import signal_engine
import storage

# Throwaway temp-file DB, initialised once with default weights. Weights are NOT
# updated during the backtest, so you measure the raw strategy against history
# rather than a moving target. That keeps results honest.
_bt = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_bt.close()
BT_DB = _bt.name
storage.init_db(BT_DB)


def _slice_timeframes(df_1h, df_1day, upto_time):
    """Build a {tf: df} dict containing only candles at/before upto_time."""
    h1 = df_1h[df_1h.index <= upto_time]
    d1 = df_1day[df_1day.index <= upto_time]
    # Need enough candles for the indicators (200 EMA etc.)
    if len(h1) < 210 or len(d1) < 60:
        return None
    tf = {"1h": h1, "1day": d1}
    if "3h" in config.CONFIRM_TIMEFRAMES or config.PRIMARY_TF == "3h":
        tf["3h"] = data_feed.resample_3h(h1)
    return tf


def _simulate_trade(sig, future_1h):
    """Given a signal and the 1h candles AFTER entry, return 'WIN'/'LOSS'/'OPEN'
    and the bars-held count. First level touched wins."""
    for i, (_, row) in enumerate(future_1h.iterrows(), start=1):
        hi, lo = float(row["high"]), float(row["low"])
        if sig["direction"] == "BUY":
            if lo <= sig["sl"]:
                return "LOSS", i
            if hi >= sig["tp"]:
                return "WIN", i
        else:
            if hi >= sig["sl"]:
                return "LOSS", i
            if lo <= sig["tp"]:
                return "WIN", i
    return "OPEN", len(future_1h)


def run_backtest(symbol, n_1h=1500):
    print(f"\n=== Backtest: {symbol} ({n_1h} 1h candles) ===")
    print("Fetching history...")
    df_1h = data_feed.fetch_candles(symbol, "1h", outputsize=min(n_1h, 5000))
    df_1day = data_feed.fetch_candles(symbol, "1day", outputsize=400)

    times = df_1h.index
    trades = []
    open_until = None  # don't open a new trade while one is "live" (matches live bot)

    # Walk forward. Leave room at the end to simulate trades.
    for pos in range(210, len(times) - 1):
        now = times[pos]
        if open_until is not None and now <= open_until:
            continue

        tf = _slice_timeframes(df_1h, df_1day, now)
        if tf is None:
            continue

        sig = signal_engine.generate_signal(BT_DB, tf)
        if sig is None:
            continue

        future = df_1h.iloc[pos + 1:]
        result, bars = _simulate_trade(sig, future)
        if result == "OPEN":
            continue  # trade never resolved within the data window; skip

        trades.append({
            "time": now, "direction": sig["direction"], "entry": sig["entry"],
            "sl": sig["sl"], "tp": sig["tp"], "result": result,
            "bars_held": bars, "confidence": sig["confidence"],
        })
        # Block new signals until trade closes AND cooldown expires
        close_idx = bars - 1
        cooldown_idx = min(close_idx + config.SIGNAL_COOLDOWN_BARS, len(future) - 1)
        open_until = future.index[cooldown_idx]

    _report(symbol, trades)
    return trades


def _report(symbol, trades):
    if not trades:
        print("No resolved trades in this window. Try more history or a lower threshold.")
        return

    wins = sum(1 for t in trades if t["result"] == "WIN")
    losses = sum(1 for t in trades if t["result"] == "LOSS")
    total = wins + losses
    win_rate = wins / total * 100 if total else 0

    buys = [t for t in trades if t["direction"] == "BUY"]
    sells = [t for t in trades if t["direction"] == "SELL"]
    buy_wins = sum(1 for t in buys if t["result"] == "WIN")
    sell_wins = sum(1 for t in sells if t["result"] == "WIN")

    # Expectancy in R multiples: a win = +rr (from config), a loss = -1
    rr = config.TP1_ATR_MULT / config.SL_ATR_MULT
    expectancy = (wins * rr - losses * 1) / total if total else 0

    avg_hold = sum(t["bars_held"] for t in trades) / total if total else 0

    print(f"\n--- Results: {symbol} ---")
    print(f"Total trades:   {total}")
    print(f"Wins / Losses:  {wins} / {losses}")
    print(f"Win rate:       {win_rate:.1f}%")
    print(f"BUY trades:     {len(buys)}  (win rate {(buy_wins/len(buys)*100 if buys else 0):.1f}%)")
    print(f"SELL trades:    {len(sells)}  (win rate {(sell_wins/len(sells)*100 if sells else 0):.1f}%)")
    print(f"Risk:Reward:    1:{rr:.2f}")
    print(f"Expectancy:     {expectancy:+.3f} R per trade")
    print(f"Avg bars held:  {avg_hold:.1f} (1h candles)")

    if expectancy > 0:
        print("\n=> Positive expectancy on this window. Promising, but validate on more data.")
    else:
        print("\n=> Negative expectancy: this strategy would lose money over this window.")
    print("   (Backtest excludes spread & slippage, so live results are worse.)")


if __name__ == "__main__":
    symbol = sys.argv[1] if len(sys.argv) > 1 else config.PAIRS[0]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 1500
    try:
        run_backtest(symbol, n)
    finally:
        try:
            os.remove(BT_DB)
        except OSError:
            pass
