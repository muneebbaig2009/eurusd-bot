"""Pull OHLC candles from Twelve Data for a given pair, resample 3h from 1h."""
import requests
import pandas as pd
import config


def fetch_candles(symbol: str, interval: str,
                  outputsize: int = config.CANDLE_COUNT) -> pd.DataFrame:
    """Fetch OHLC candles for a symbol+interval. Sorted oldest->newest."""
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": config.TWELVE_DATA_API_KEY,
        "format": "JSON",
    }
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") == "error":
        raise RuntimeError(f"Twelve Data error ({symbol} {interval}): {data.get('message')}")

    values = data.get("values", [])
    if not values:
        raise RuntimeError(f"No candle data returned for {symbol} {interval}")

    df = pd.DataFrame(values)
    df["datetime"] = pd.to_datetime(df["datetime"])
    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col])
    df = df.sort_values("datetime").reset_index(drop=True)
    df = df.set_index("datetime")
    return df[["open", "high", "low", "close"]]


def resample_3h(df_1h: pd.DataFrame) -> pd.DataFrame:
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    return df_1h.resample("3h").agg(agg).dropna()


def get_all_timeframes(symbol: str) -> dict:
    """Return {tf_name: DataFrame} for 5min, 1h, 3h, 1day for one pair."""
    out = {}
    for name, interval in config.TIMEFRAMES.items():
        out[name] = fetch_candles(symbol, interval)
    out["3h"] = resample_3h(out["1h"])
    return out
