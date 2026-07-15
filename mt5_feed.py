"""Fetch OHLC candles from a running MT5 terminal (used when EXECUTION_MODE='mt5').
Drop-in replacement for data_feed.get_all_timeframes() — same return shape.
"""
import pandas as pd
import MetaTrader5 as mt5
import config

_TF_MAP = {
    "5min": mt5.TIMEFRAME_M5,
    "1h":   mt5.TIMEFRAME_H1,
    "1day": mt5.TIMEFRAME_D1,
}


def mt5_symbol(symbol: str) -> str:
    """'EUR/USD' -> 'EURUSD'"""
    return symbol.replace("/", "")


def fetch_candles(symbol: str, interval: str,
                  outputsize: int = config.CANDLE_COUNT) -> pd.DataFrame:
    """Pull `outputsize` candles from MT5. Returns DataFrame sorted oldest→newest."""
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


def resample_3h(df_1h: pd.DataFrame) -> pd.DataFrame:
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    return df_1h.resample("3h").agg(agg).dropna()


def get_all_timeframes(symbol: str) -> dict:
    """Return {tf_name: DataFrame} for 5min, 1h, 3h, 1day — sourced from MT5."""
    out = {}
    for name in ("5min", "1h", "1day"):
        out[name] = fetch_candles(symbol, name)
    out["3h"] = resample_3h(out["1h"])
    return out
