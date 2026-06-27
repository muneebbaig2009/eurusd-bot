"""Combine weighted votes across timeframes into a final BUY/SELL signal.

Produces a richer signal: direction, entry, SL, TP1, TP2, confidence %,
risk-reward ratio, trend strength, timeframe, and the contributing techniques.
"""
import importlib.metadata  # noqa: F401  (required before importing pandas_ta fork)
import pandas_ta as ta
import config
import storage
from techniques import get_votes, TECHNIQUES


def score_timeframe(df) -> tuple:
    """Return (weighted_score, {technique: vote}) for one timeframe."""
    votes = get_votes(df)
    score = 0.0
    for tech, vote in votes.items():
        weight = storage.get_weight(tech)
        score += weight * vote
    return score, votes


def compute_atr(df, length=14) -> float:
    atr = ta.atr(df["high"], df["low"], df["close"], length=length)
    if atr is None or atr.dropna().empty:
        return df["close"].iloc[-1] * 0.001
    return float(atr.iloc[-1])


def trend_strength(df) -> int:
    """0-100 score of how strongly price is trending, via ADX (falls back to 50)."""
    adx = ta.adx(df["high"], df["low"], df["close"], length=14)
    if adx is None or adx.dropna().empty:
        return 50
    # ADX column is typically the first; clamp 0-100
    val = float(adx.iloc[-1, 0])
    return int(max(0, min(100, round(val))))


def confidence_pct(primary_score) -> int:
    """Map the weighted score to a readable confidence %, normalised against
    the current sum of technique weights so it adapts as weights learn."""
    total_weight = sum(storage.get_weight(t) for t in TECHNIQUES)
    if total_weight <= 0:
        return config.CONF_MIN
    # ratio of how much of the available weight pointed one way
    ratio = abs(primary_score) / total_weight  # 0..1
    span = config.CONF_MAX - config.CONF_MIN
    return int(round(config.CONF_MIN + ratio * span))


def generate_signal(timeframes: dict):
    """timeframes: {tf_name: DataFrame}. Returns a rich signal dict or None."""
    tf_scores = {}
    tf_votes = {}
    for tf in config.CONFIRM_TIMEFRAMES:
        if tf not in timeframes:
            return None
        s, v = score_timeframe(timeframes[tf])
        tf_scores[tf] = s
        tf_votes[tf] = v

    signs = {1 if s > 0 else -1 if s < 0 else 0 for s in tf_scores.values()}
    if len(signs) != 1 or 0 in signs:
        return None

    primary_score = tf_scores[config.PRIMARY_TF]
    if abs(primary_score) < config.SIGNAL_THRESHOLD:
        return None

    direction = "BUY" if primary_score > 0 else "SELL"
    primary_df = timeframes[config.PRIMARY_TF]
    entry = float(primary_df["close"].iloc[-1])
    atr = compute_atr(primary_df)

    if direction == "BUY":
        tp1 = entry + config.TP1_ATR_MULT * atr
        tp2 = entry + config.TP2_ATR_MULT * atr
        sl = entry - config.SL_ATR_MULT * atr
    else:
        tp1 = entry - config.TP1_ATR_MULT * atr
        tp2 = entry - config.TP2_ATR_MULT * atr
        sl = entry + config.SL_ATR_MULT * atr

    # Risk-reward to TP1 = reward distance / risk distance
    reward = abs(tp1 - entry)
    risk = abs(entry - sl)
    rr = round(reward / risk, 2) if risk > 0 else 0.0

    contributors = tf_votes[config.PRIMARY_TF]

    return {
        "direction": direction,
        "entry": round(entry, 5),
        "sl": round(sl, 5),
        "tp": round(tp1, 5),          # tp == tp1, used for win/loss checking
        "tp1": round(tp1, 5),
        "tp2": round(tp2, 5),
        "score": round(primary_score, 3),
        "confidence": confidence_pct(primary_score),
        "rr": rr,
        "trend_strength": trend_strength(primary_df),
        "timeframe": config.PRIMARY_TF,
        "contributors": contributors,
    }
