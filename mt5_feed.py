"""Fetch OHLC candles from a running MT5 terminal (used when EXECUTION_MODE='mt5').
Drop-in replacement for data_feed.get_all_timeframes() — same return shape.
"""
import pandas as pd
import MetaTrader5 as mt5
import config

_TF_MAP = {
    "5min": mt5.TIMEFRAME_M5,
    "15m":  mt5.TIMEFRAME_M15,
    "30m":  mt5.TIMEFRAME_M30,
    "1h":   mt5.TIMEFRAME_H1,
    "1day": mt5.TIMEFRAME_D1,
}


def mt5_symbol(symbol: str) -> str:
    """'EUR/USD' -> 'EURUSD'"""
    return symbol.replace("/", "")


def fetch_candles(symbol: str, interval: str,
                  outputsize: int = None) -> pd.DataFrame:
    """Pull candles from MT5. Bar count defaults to CANDLE_COUNT_MAP[interval]."""
    if outputsize is None:
        outputsize = config.CANDLE_COUNT_MAP.get(interval, config.CANDLE_COUNT)
    tf  = _TF_MAP[interval]
    sym = mt5_symbol(symbol)
    rates = mt5.copy_rates_from_pos(sym, tf, 0, outputsize)
    if rates is None or len(rates) == 0:
        raise RuntimeError(
            f"MT5: no candle data for {sym} {interval}. Error: {mt5.last_error()}"
        )
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df = df.set_index("time")
    return df[["open", "high", "low", "close"]]


def get_all_timeframes(symbol: str) -> dict:
    """Return {tf_name: DataFrame} for every timeframe the pair's config needs."""
    pair_cfg = config.get_pair_config(symbol)
    needed   = set(pair_cfg.get("CONFIRM_TIMEFRAMES", ["1h", "1day"]))
    out = {}
    for name in sorted(needed):
        out[name] = fetch_candles(symbol, name)
    return out
