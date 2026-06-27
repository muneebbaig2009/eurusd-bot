"""Combine weighted votes across timeframes into a final BUY/SELL signal + TP/SL."""
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
        # fallback: use a small fraction of price
        return df["close"].iloc[-1] * 0.001
    return float(atr.iloc[-1])


def generate_signal(timeframes: dict):
    """
    timeframes: {tf_name: DataFrame}
    Returns a signal dict or None.
    Requires CONFIRM_TIMEFRAMES to agree in direction.
    """
    tf_scores = {}
    tf_votes = {}
    for tf in config.CONFIRM_TIMEFRAMES:
        if tf not in timeframes:
            return None
        s, v = score_timeframe(timeframes[tf])
        tf_scores[tf] = s
        tf_votes[tf] = v

    # All confirming timeframes must share a sign and primary must clear threshold
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
        tp = entry + config.TP_ATR_MULT * atr
        sl = entry - config.SL_ATR_MULT * atr
    else:
        tp = entry - config.TP_ATR_MULT * atr
        sl = entry + config.SL_ATR_MULT * atr

    # contributors = primary timeframe votes (what we'll learn from)
    contributors = tf_votes[config.PRIMARY_TF]

    return {
        "direction": direction,
        "entry": round(entry, 5),
        "tp": round(tp, 5),
        "sl": round(sl, 5),
        "score": round(primary_score, 3),
        "contributors": contributors,
    }
