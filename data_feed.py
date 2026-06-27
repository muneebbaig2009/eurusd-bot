"""Pull EURUSD OHLC candles from Twelve Data and resample 3h from 1h."""
import requests
import pandas as pd
import config


def fetch_candles(interval: str, outputsize: int = config.CANDLE_COUNT) -> pd.DataFrame:
    """Fetch OHLC candles for a given interval. Returns a DataFrame sorted oldest->newest."""
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": config.SYMBOL,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": config.TWELVE_DATA_API_KEY,
        "format": "JSON",
    }
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") == "error":
        raise RuntimeError(f"Twelve Data error ({interval}): {data.get('message')}")

    values = data.get("values", [])
    if not values:
        raise RuntimeError(f"No candle data returned for {interval}")

    df = pd.DataFrame(values)
    df["datetime"] = pd.to_datetime(df["datetime"])
    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col])
    df = df.sort_values("datetime").reset_index(drop=True)
    df = df.set_index("datetime")
    return df[["open", "high", "low", "close"]]


def resample_3h(df_1h: pd.DataFrame) -> pd.DataFrame:
    """Build 3-hour candles from 1-hour candles."""
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    df_3h = df_1h.resample("3h").agg(agg).dropna()
    return df_3h


def get_all_timeframes() -> dict:
    """Return {tf_name: DataFrame} for 5min, 1h, 3h, 1day."""
    out = {}
    for name, interval in config.TIMEFRAMES.items():
        out[name] = fetch_candles(interval)
    out["3h"] = resample_3h(out["1h"])
    return out
