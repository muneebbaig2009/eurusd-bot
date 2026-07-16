"""Combine weighted votes across timeframes into a final BUY/SELL signal.

Produces a richer signal: direction, entry, SL, TP1, TP2, confidence %,
risk-reward ratio, trend strength, timeframe, and the contributing techniques.

All strategy parameters come from a `cfg` dict (from config.get_pair_config())
so each pair can have its own thresholds, ATR multipliers, ADX floor, etc.
"""
import importlib.metadata  # noqa: F401  (required before importing pandas_ta fork)
import pandas_ta as ta
import config
import storage
from techniques import get_votes, TECHNIQUES


def score_timeframe(db, df) -> tuple:
    """Return (weighted_score, {technique: vote}) for one timeframe."""
    votes = get_votes(df)
    score = 0.0
    for tech, vote in votes.items():
        weight = storage.get_weight(db, tech)
        score += weight * vote
    return score, votes


def compute_atr(df, length=10) -> float:
    atr = ta.atr(df["high"], df["low"], df["close"], length=length)
    if atr is None or atr.dropna().empty:
        return df["close"].iloc[-1] * 0.001
    return float(atr.iloc[-1])


def trend_strength(df, length=14) -> int:
    """0-100 score of how strongly price is trending, via ADX (falls back to 50)."""
    adx = ta.adx(df["high"], df["low"], df["close"], length=length)
    if adx is None or adx.dropna().empty:
        return 50
    val = float(adx.iloc[-1, 0])
    return int(max(0, min(100, round(val))))


def confidence_pct(db, primary_score, cfg, active_votes: dict = None) -> int:
    """Map the weighted score to confidence %, normalised against active
    (non-zero voting) technique weights only.  Neutral techniques sitting at
    vote=0 have no opinion and should not dilute confidence."""
    if active_votes:
        active_weight = sum(
            storage.get_weight(db, t) for t, v in active_votes.items() if v != 0
        )
    else:
        active_weight = sum(storage.get_weight(db, t) for t in TECHNIQUES)
    if active_weight <= 0:
        return cfg["CONF_MIN"]
    ratio = abs(primary_score) / active_weight
    span  = cfg["CONF_MAX"] - cfg["CONF_MIN"]
    return int(round(cfg["CONF_MIN"] + ratio * span))


def generate_signal(db, timeframes: dict, cfg: dict = None):
    """timeframes: {tf_name: DataFrame}. cfg: per-pair strategy params from
    config.get_pair_config(). Returns a rich signal dict or None."""
    if cfg is None:
        cfg = config.get_pair_config(config.SYMBOL)

    tf_scores = {}
    tf_votes  = {}
    for tf in cfg["CONFIRM_TIMEFRAMES"]:
        if tf not in timeframes:
            return None
        s, v = score_timeframe(db, timeframes[tf])
        tf_scores[tf] = s
        tf_votes[tf]  = v

    signs = {1 if s > 0 else -1 if s < 0 else 0 for s in tf_scores.values()}
    if len(signs) != 1 or 0 in signs:
        return None

    primary_score = tf_scores[cfg["PRIMARY_TF"]]
    if abs(primary_score) < cfg["SIGNAL_THRESHOLD"]:
        return None

    direction  = "BUY" if primary_score > 0 else "SELL"
    primary_df = timeframes[cfg["PRIMARY_TF"]]

    adx_period = cfg.get("ADX_PERIOD", 14)
    adx = trend_strength(primary_df, length=adx_period)
    if adx < cfg["MIN_ADX"]:
        return None

    conf = confidence_pct(db, primary_score, cfg, active_votes=tf_votes[cfg["PRIMARY_TF"]])
    if conf < cfg["MIN_CONFIDENCE"]:
        return None

    entry = float(primary_df["close"].iloc[-1])
    atr   = compute_atr(primary_df)

    if direction == "BUY":
        tp1 = entry + cfg["TP1_ATR_MULT"] * atr
        tp2 = entry + cfg["TP2_ATR_MULT"] * atr
        sl  = entry - cfg["SL_ATR_MULT"]  * atr
    else:
        tp1 = entry - cfg["TP1_ATR_MULT"] * atr
        tp2 = entry - cfg["TP2_ATR_MULT"] * atr
        sl  = entry + cfg["SL_ATR_MULT"]  * atr

    reward = abs(tp1 - entry)
    risk   = abs(entry - sl)
    rr     = round(reward / risk, 2) if risk > 0 else 0.0

    contributors = tf_votes[cfg["PRIMARY_TF"]]

    return {
        "direction":      direction,
        "entry":          round(entry, 5),
        "sl":             round(sl,    5),
        "tp":             round(tp1,   5),
        "tp1":            round(tp1,   5),
        "tp2":            round(tp2,   5),
        "score":          round(primary_score, 3),
        "confidence":     conf,
        "rr":             rr,
        "trend_strength": adx,
        "timeframe":      cfg["PRIMARY_TF"],
        "contributors":   contributors,
    }
