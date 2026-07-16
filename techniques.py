"""Each technique looks at a candle DataFrame and votes: +1 buy, -1 sell, 0 neutral.

Add new techniques by writing a function (df, params=None) -> int and
registering it in TECHNIQUES.  The adaptive engine handles parameter
tuning; the learning system handles weighting.

All vote functions accept an optional `params` dict so the adaptive engine
can supply alternative values without breaking any existing caller that
omits the argument.
"""
import importlib.metadata  # noqa: F401  (required before importing pandas_ta fork)
import pandas as pd
import pandas_ta as ta


# ── Vote functions ────────────────────────────────────────────────────────────

def rsi_vote(df: pd.DataFrame, params: dict = None) -> int:
    p      = params or {}
    period = int(p.get("RSI_PERIOD", 14))
    os_lvl = float(p.get("RSI_OS", 35))
    ob_lvl = float(p.get("RSI_OB", 75))

    rsi = ta.rsi(df["close"], length=period)
    if rsi is None or rsi.dropna().empty:
        return 0
    val = float(rsi.iloc[-1])
    if val < os_lvl:
        return 1       # oversold → buy
    if val > ob_lvl:
        return -1      # overbought → sell
    return 0


def ma_cross_vote(df: pd.DataFrame, params: dict = None) -> int:
    """Vote ONLY on fresh MA crossovers within the last 3 bars.
    Returns 0 when no recent cross — eliminates the standing-trend SELL bias.
    """
    p    = params or {}
    fast_p = int(p.get("MA_FAST", 8))
    slow_p = int(p.get("MA_SLOW", 21))

    fast = ta.sma(df["close"], length=fast_p)
    slow = ta.sma(df["close"], length=slow_p)
    if fast is None or slow is None or len(fast) < slow_p + 5:
        return 0
    for i in [-1, -2, -3]:
        curr = fast.iloc[i] - slow.iloc[i]
        prev = fast.iloc[i - 1] - slow.iloc[i - 1]
        if curr > 0 and prev <= 0:
            return 1   # fresh bullish cross
        if curr < 0 and prev >= 0:
            return -1  # fresh bearish cross
    return 0


def macd_vote(df: pd.DataFrame, params: dict = None) -> int:
    p      = params or {}
    fast_p = int(p.get("MACD_FAST", 10))
    slow_p = int(p.get("MACD_SLOW", 21))
    sig_p  = int(p.get("MACD_SIGNAL", 9))

    macd = ta.macd(df["close"], fast=fast_p, slow=slow_p, signal=sig_p)
    if macd is None or macd.empty:
        return 0
    hist = macd.iloc[:, 1]   # MACDh column
    if hist.dropna().empty or len(hist.dropna()) < 2:
        return 0
    current  = float(hist.dropna().iloc[-1])
    previous = float(hist.dropna().iloc[-2])
    if current > 0 and previous <= 0:
        return 1    # histogram just turned positive
    if current < 0 and previous >= 0:
        return -1   # histogram just turned negative
    # Standing directional bias (lower weight via adaptive weighting)
    return 1 if current > 0 else (-1 if current < 0 else 0)


def bbands_vote(df: pd.DataFrame, params: dict = None) -> int:
    p      = params or {}
    period = int(p.get("BB_PERIOD", 15))
    std    = float(p.get("BB_STD", 1.5))

    bb = ta.bbands(df["close"], length=period, std=std)
    if bb is None or bb.empty:
        return 0
    lower = float(bb.iloc[-1, 0])
    upper = float(bb.iloc[-1, 2])
    price = float(df["close"].iloc[-1])
    if price <= lower:
        return 1     # price at lower band → mean-reversion buy
    if price >= upper:
        return -1    # price at upper band → mean-reversion sell
    return 0


def ema_trend_vote(df: pd.DataFrame, params: dict = None) -> int:
    """Price vs EMA with a configurable buffer zone to avoid noise near the line."""
    p      = params or {}
    period = int(p.get("EMA_PERIOD", 200))
    buffer = float(p.get("EMA_BUFFER", 0.0003))

    ema = ta.ema(df["close"], length=period)
    if ema is None or ema.dropna().empty:
        return 0
    price   = float(df["close"].iloc[-1])
    ema_val = float(ema.dropna().iloc[-1])
    zone    = ema_val * buffer
    if price > ema_val + zone:
        return 1
    if price < ema_val - zone:
        return -1
    return 0   # price in noise zone — no vote


def stoch_vote(df: pd.DataFrame, params: dict = None) -> int:
    """Stochastic %K/%D: configurable oversold / overbought bands."""
    p     = params or {}
    os_l  = float(p.get("STOCH_OS", 30))
    ob_l  = float(p.get("STOCH_OB", 70))

    stoch = ta.stoch(df["high"], df["low"], df["close"])
    if stoch is None or stoch.empty:
        return 0
    k = float(stoch.iloc[-1, 0])
    d = float(stoch.iloc[-1, 1])
    if k < os_l and d < os_l:
        return 1    # oversold — buy pressure building
    if k > ob_l and d > ob_l:
        return -1   # overbought — sell pressure building
    return 0


def supertrend_vote(df: pd.DataFrame, params: dict = None) -> int:
    """Supertrend direction: +1 when price is above the line, -1 below."""
    p      = params or {}
    length = int(p.get("ST_LENGTH", 7))
    mult   = float(p.get("ST_MULT", 3.0))

    try:
        st = ta.supertrend(df["high"], df["low"], df["close"],
                           length=length, multiplier=mult)
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
    return 1 if last > 0 else (-1 if last < 0 else 0)


# ── Registry ──────────────────────────────────────────────────────────────────

TECHNIQUES: dict[str, callable] = {
    "rsi":        rsi_vote,
    "ma_cross":   ma_cross_vote,
    "macd":       macd_vote,
    "bbands":     bbands_vote,
    "ema_trend":  ema_trend_vote,
    "stoch":      stoch_vote,
    "supertrend": supertrend_vote,
}

OSCILLATOR_TECHNIQUES: set[str] = {"rsi", "bbands", "stoch"}


def get_votes(df: pd.DataFrame, params: dict = None) -> dict:
    """Return {technique_name: vote} for one timeframe's candles.

    params  Optional adaptive parameter dict from AdaptiveParamStore.get_all().
            When None each function uses its original hard-coded defaults.
    """
    votes = {}
    for name, fn in TECHNIQUES.items():
        try:
            votes[name] = fn(df, params)
        except Exception:
            votes[name] = 0
    return votes


def get_indicator_values(df: pd.DataFrame, params: dict = None) -> dict:
    """Compute raw indicator values for trade context recording.

    Returns a flat dict of floats / ints — None for any value that could
    not be computed.  Called once per signal, results stored in trade_context.
    """
    p      = params or {}
    values = {}

    # RSI
    try:
        rsi = ta.rsi(df["close"], length=int(p.get("RSI_PERIOD", 14)))
        values["rsi"] = float(rsi.iloc[-1]) if rsi is not None and not rsi.dropna().empty else None
    except Exception:
        values["rsi"] = None

    # ATR (fixed period — used for SL/TP sizing, not adaptive)
    try:
        atr = ta.atr(df["high"], df["low"], df["close"], length=10)
        values["atr"] = float(atr.iloc[-1]) if atr is not None and not atr.dropna().empty else None
    except Exception:
        values["atr"] = None

    # MACD histogram
    try:
        macd = ta.macd(df["close"],
                       fast=int(p.get("MACD_FAST", 10)),
                       slow=int(p.get("MACD_SLOW", 21)),
                       signal=int(p.get("MACD_SIGNAL", 9)))
        values["macd_hist"] = (
            float(macd.iloc[-1, 1])
            if macd is not None and not macd.empty
            else None
        )
    except Exception:
        values["macd_hist"] = None

    # EMA trend direction
    try:
        period = int(p.get("EMA_PERIOD", 200))
        ema    = ta.ema(df["close"], length=period)
        if ema is not None and not ema.dropna().empty:
            price   = float(df["close"].iloc[-1])
            ema_val = float(ema.dropna().iloc[-1])
            values["ema_above"] = int(price > ema_val)
        else:
            values["ema_above"] = None
    except Exception:
        values["ema_above"] = None

    # Stochastic K / D
    try:
        stoch = ta.stoch(df["high"], df["low"], df["close"])
        if stoch is not None and not stoch.empty:
            values["stoch_k"] = float(stoch.iloc[-1, 0])
            values["stoch_d"] = float(stoch.iloc[-1, 1])
        else:
            values["stoch_k"] = values["stoch_d"] = None
    except Exception:
        values["stoch_k"] = values["stoch_d"] = None

    # Bollinger %B  (where is price within the bands? 0=lower, 1=upper)
    try:
        bb = ta.bbands(df["close"],
                       length=int(p.get("BB_PERIOD", 15)),
                       std=float(p.get("BB_STD", 1.5)))
        if bb is not None and not bb.empty:
            lower = float(bb.iloc[-1, 0])
            upper = float(bb.iloc[-1, 2])
            price = float(df["close"].iloc[-1])
            span  = upper - lower
            values["bb_pct"] = round((price - lower) / span, 4) if span > 0 else 0.5
        else:
            values["bb_pct"] = None
    except Exception:
        values["bb_pct"] = None

    # Supertrend direction (+1 / -1)
    try:
        st = ta.supertrend(df["high"], df["low"], df["close"],
                           length=int(p.get("ST_LENGTH", 7)),
                           multiplier=float(p.get("ST_MULT", 3.0)))
        if st is not None and not st.empty:
            dcols = [c for c in st.columns if "SUPERTd" in c]
            if dcols:
                v = st[dcols[0]].dropna()
                values["supertrend_dir"] = int(v.iloc[-1]) if not v.empty else None
            else:
                values["supertrend_dir"] = None
        else:
            values["supertrend_dir"] = None
    except Exception:
        values["supertrend_dir"] = None

    return values
