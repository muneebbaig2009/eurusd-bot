"""Each technique looks at a candle DataFrame and votes: +1 buy, -1 sell, 0 neutral.

Add new techniques here by writing a function (df) -> int and registering it
in TECHNIQUES. The learning system handles weighting automatically.
"""
import importlib.metadata  # noqa: F401  (required before importing pandas_ta fork)
import pandas as pd
import pandas_ta as ta


def rsi_vote(df: pd.DataFrame) -> int:
    rsi = ta.rsi(df["close"], length=14)
    if rsi is None or rsi.dropna().empty:
        return 0
    val = rsi.iloc[-1]
    if val < 30:
        return 1      # oversold -> buy
    if val > 70:
        return -1     # overbought -> sell
    return 0


def ma_cross_vote(df: pd.DataFrame) -> int:
    fast = ta.sma(df["close"], length=20)
    slow = ta.sma(df["close"], length=50)
    if fast is None or slow is None:
        return 0
    if fast.iloc[-1] > slow.iloc[-1] and fast.iloc[-2] <= slow.iloc[-2]:
        return 1      # fresh bullish cross
    if fast.iloc[-1] < slow.iloc[-1] and fast.iloc[-2] >= slow.iloc[-2]:
        return -1
    # also vote on standing trend (weaker, but the weight system handles it)
    if fast.iloc[-1] > slow.iloc[-1]:
        return 1
    return -1


def macd_vote(df: pd.DataFrame) -> int:
    macd = ta.macd(df["close"])
    if macd is None or macd.empty:
        return 0
    macd_line = macd.iloc[-1, 0]
    signal_line = macd.iloc[-1, 2]
    if macd_line > signal_line:
        return 1
    if macd_line < signal_line:
        return -1
    return 0


def bbands_vote(df: pd.DataFrame) -> int:
    bb = ta.bbands(df["close"], length=20)
    if bb is None or bb.empty:
        return 0
    lower = bb.iloc[-1, 0]
    upper = bb.iloc[-1, 2]
    price = df["close"].iloc[-1]
    if price <= lower:
        return 1      # price at lower band -> bounce buy
    if price >= upper:
        return -1
    return 0


def ema_trend_vote(df: pd.DataFrame) -> int:
    ema = ta.ema(df["close"], length=200)
    if ema is None or ema.dropna().empty:
        return 0
    return 1 if df["close"].iloc[-1] > ema.iloc[-1] else -1


# Registry: name -> function. Names are used as DB keys for weights.
TECHNIQUES = {
    "rsi": rsi_vote,
    "ma_cross": ma_cross_vote,
    "macd": macd_vote,
    "bbands": bbands_vote,
    "ema_trend": ema_trend_vote,
}


def get_votes(df: pd.DataFrame) -> dict:
    """Return {technique_name: vote} for one timeframe's candles."""
    votes = {}
    for name, fn in TECHNIQUES.items():
        try:
            votes[name] = fn(df)
        except Exception:
            votes[name] = 0
    return votes
