"""Offline test: generate synthetic candles, run the full pipeline, no API/Discord needed."""
import numpy as np
import pandas as pd
import config
config.DB_PATH = "test.db"  # isolate test DB

import storage, signal_engine, learner
from techniques import get_votes


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
    import os
    if os.path.exists("test.db"):
        os.remove("test.db")
    storage.init_db()

    df = make_candles()
    print("Votes:", get_votes(df))

    timeframes = {
        "5min": make_candles(seed=2),
        "1h": df,
        "3h": make_candles(seed=3),
        "1day": make_candles(trend=0.0002, seed=4),
    }

    sig = signal_engine.generate_signal(timeframes)
    print("Signal:", sig)

    if sig:
        sid = storage.log_signal(sig["direction"], sig["entry"], sig["tp"],
                                 sig["sl"], sig["score"], sig["contributors"])
        print("Logged signal id:", sid)
        print("Weights before:", storage.all_weights())
        # Simulate a WIN
        storage.close_signal(sid, "WIN", sig["tp"])
        learner.update_weights(sig["contributors"], sig["direction"], won=True)
        print("Weights after WIN:", storage.all_weights())
        print("Stats:", storage.stats())

    print("\nAll modules executed successfully.")
    os.remove("test.db")


if __name__ == "__main__":
    main()
