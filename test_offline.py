"""Offline test: synthetic candles through the full multi-pair pipeline."""
import os
import numpy as np
import pandas as pd
import config

import storage, signal_engine, learner
from techniques import get_votes

TEST_DB = "test.db"


def make_candles(n=250, trend=0.0001, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-01-01", periods=n, freq="1h")
    price = 1.10 + np.cumsum(rng.normal(trend, 0.0008, n))
    close = pd.Series(price, index=idx)
    high = close + rng.uniform(0, 0.0006, n)
    low = close - rng.uniform(0, 0.0006, n)
    open_ = close.shift(1).fillna(close.iloc[0])
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close}, index=idx)


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    storage.init_db(TEST_DB)

    df = make_candles()
    print("Votes:", get_votes(df))

    timeframes = {
        "5min": make_candles(seed=2),
        "1h": df,
        "3h": make_candles(seed=3),
        "1day": make_candles(trend=0.0002, seed=4),
    }

    sig = signal_engine.generate_signal(TEST_DB, timeframes)
    print("Signal:", sig)

    if sig:
        sig["lot_size"] = storage.calc_lot_size(
            config.DEMO_INITIAL_BALANCE, sig["entry"], sig["sl"],
            risk_pct=config.DEMO_RISK_PCT / 2)
        id1, id2 = storage.log_signal_pair(TEST_DB, sig)
        print("Logged pair:", id1, id2, "| weights before:", storage.all_weights(TEST_DB))
        storage.close_signal(TEST_DB, id1, "WIN", sig["tp1"] or sig["tp"])
        storage.close_signal(TEST_DB, id2, "WIN", sig["tp2"] or sig["tp"]) if id2 else None
        learner.update_weights(TEST_DB, sig["contributors"], sig["direction"], won=True)
        print("Weights after WIN:", storage.all_weights(TEST_DB))
        print("Stats:", storage.stats(TEST_DB))

    print("\nAll modules executed successfully.")
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)


if __name__ == "__main__":
    main()
