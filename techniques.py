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
    if val < 35:
        return 1      # oversold -> buy
    if val > 65:
        return -1     # overbought -> sell
    return 0


def ma_cross_vote(df: pd.DataFrame) -> int:
    """Vote ONLY on fresh MA crossovers within the last 3 bars.
    Returns 0 when no recent cross — eliminates the standing-trend SELL bias."""
    fast = ta.sma(df["close"], length=20)
    slow = ta.sma(df["close"], length=50)
    if fast is None or slow is None or len(fast) < 55:
        return 0
    for i in [-1, -2, -3]:
        curr = fast.iloc[i] - slow.iloc[i]
        prev = fast.iloc[i - 1] - slow.iloc[i - 1]
        if curr > 0 and prev <= 0:
            return 1   # fresh bullish cross
        if curr < 0 and prev >= 0:
            return -1  # fresh bearish cross
    return 0


def macd_vote(df: pd.DataFrame) -> int:
    macd = ta.macd(df["close"])
    if macd is None or macd.empty:
        return 0
    # Require MACD histogram to have turned (not just crossed), for quality
    hist = macd.iloc[:, 1]   # MACDh column
    if hist.dropna().empty or len(hist.dropna()) < 2:
        return 0
    current = float(hist.dropna().iloc[-1])
    previous = float(hist.dropna().iloc[-2])
    if current > 0 and previous <= 0:
        return 1   # histogram just turned positive
    if current < 0 and previous >= 0:
        return -1  # histogram just turned negative
    # Standing: smaller weight but still directional
    if current > 0:
        return 1
    if current < 0:
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
        return 1      # price at lower band -> mean-reversion buy
    if price >= upper:
        return -1     # price at upper band -> mean-reversion sell
    return 0


def ema_trend_vote(df: pd.DataFrame) -> int:
    """Price vs 200 EMA with a 0.05% buffer zone to avoid noise near the EMA."""
    ema = ta.ema(df["close"], length=200)
    if ema is None or ema.dropna().empty:
        return 0
    price = df["close"].iloc[-1]
    ema_val = float(ema.dropna().iloc[-1])
    buffer = ema_val * 0.0005   # 0.05% — about 5-7 pips on EUR/USD
    if price > ema_val + buffer:
        return 1
    if price < ema_val - buffer:
        return -1
    return 0  # price in noise zone — no vote


def stoch_vote(df: pd.DataFrame) -> int:
    """Stochastic %K/%D: vote at overbought/oversold extremes only."""
    stoch = ta.stoch(df["high"], df["low"], df["close"])
    if stoch is None or stoch.empty:
        return 0
    k = float(stoch.iloc[-1, 0])
    d = float(stoch.iloc[-1, 1])
    if k < 25 and d < 25:
        return 1    # oversold — buy pressure building
    if k > 75 and d > 75:
        return -1   # overbought — sell pressure building
    return 0


def supertrend_vote(df: pd.DataFrame) -> int:
    """Supertrend direction: +1 when price is above the supertrend line, -1 below."""
    try:
        st = ta.supertrend(df["high"], df["low"], df["close"], length=10, multiplier=3.0)
    except Exception:
        return 0
    if st is None or st.empty:
        return 0
    direction_cols = [c for c in st.columns if "SUPERTd" in c]
    if not direction_cols:
        return 0
    val = st[direction_cols[0]].dropna()
    if val.empty:
        return 0
    last = float(val.iloc[-1])
    if last > 0:
        return 1
    if last < 0:
        return -1
    return 0


# Registry: name -> function. Names are used as DB keys for weights.
TECHNIQUES = {
    "rsi": rsi_vote,
    "ma_cross": ma_cross_vote,
    "macd": macd_vote,
    "bbands": bbands_vote,
    "ema_trend": ema_trend_vote,
    "stoch": stoch_vote,
    "supertrend": supertrend_vote,
}

# Oscillator techniques — at least one must confirm direction to fire a signal.
OSCILLATOR_TECHNIQUES = {"rsi", "bbands", "stoch"}


def get_votes(df: pd.DataFrame) -> dict:
    """Return {technique_name: vote} for one timeframe's candles."""
    votes = {}
    for name, fn in TECHNIQUES.items():
        try:
            votes[name] = fn(df)
        except Exception:
            votes[name] = 0
    return votes
