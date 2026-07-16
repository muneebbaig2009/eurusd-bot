"""Combine weighted votes across timeframes into a final BUY/SELL signal.

Produces a richer signal: direction, entry, SL, TP1, TP2, confidence %,
risk-reward ratio, trend strength, timeframe, and the contributing techniques.

All strategy parameters come from a `cfg` dict (from config.get_pair_config())
so each pair can have its own thresholds, ATR multipliers, ADX floor, etc.

Adaptive integration
--------------------
When the adaptive engine is enabled (ADAPTIVE_ENABLED=True in config.py),
generate_signal() reads the current adaptive parameter values from
AdaptiveParamStore and uses them in place of the config defaults.  If the
engine is not available the function falls back gracefully to cfg values.

The returned signal dict may carry a `_ctx` key with raw indicator values
and the adaptive params snapshot — used by the scheduler to record context.
This key is ignored by all existing consumers.
"""
import importlib.metadata  # noqa: F401  (required before importing pandas_ta fork)
import pandas_ta as ta
import config
import storage
from techniques import get_votes, get_indicator_values, TECHNIQUES


# ── Adaptive param loader ─────────────────────────────────────────────────────

def _load_adaptive_params(db: str) -> dict:
    """Return current adaptive params from the store, or {} if unavailable."""
    if not getattr(config, "ADAPTIVE_ENABLED", True):
        return {}
    try:
        from adaptive.param_store import get_store
        return get_store(db).get_all()
    except Exception:
        return {}


def _adaptive_val(adaptive: dict, key: str, cfg_fallback):
    """Return adaptive value if present and valid, else cfg_fallback."""
    v = adaptive.get(key)
    return v if v is not None else cfg_fallback


# ── Core signal helpers ───────────────────────────────────────────────────────

def score_timeframe(db, df, adaptive_params: dict = None) -> tuple:
    """Return (weighted_score, {technique: vote}) for one timeframe.

    adaptive_params  Optional dict from AdaptiveParamStore.get_all().
                     Passed through to get_votes() so each indicator can
                     use its current adaptive thresholds/periods.
    """
    votes = get_votes(df, params=adaptive_params)
    score = 0.0
    for tech, vote in votes.items():
        weight = storage.get_weight(db, tech)
        score += weight * vote
    return score, votes


def compute_atr(df, length: int = 10) -> float:
    atr = ta.atr(df["high"], df["low"], df["close"], length=length)
    if atr is None or atr.dropna().empty:
        return float(df["close"].iloc[-1]) * 0.001
    return float(atr.iloc[-1])


def trend_strength(df, length: int = 14) -> int:
    """0-100 ADX score.  Falls back to 50 on computation failure."""
    adx = ta.adx(df["high"], df["low"], df["close"], length=length)
    if adx is None or adx.dropna().empty:
        return 50
    val = float(adx.iloc[-1, 0])
    return int(max(0, min(100, round(val))))


def confidence_pct(db, primary_score: float, cfg: dict,
                   active_votes: dict = None) -> int:
    """Map the weighted score to confidence %, normalised against active
    (non-zero voting) technique weights only.  Neutral techniques sitting at
    vote=0 have no opinion and should not dilute confidence.
    """
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


# ── Main entry point ──────────────────────────────────────────────────────────

def generate_signal(db, timeframes: dict, cfg: dict = None):
    """Generate a BUY / SELL signal or return None.

    timeframes  {tf_name: DataFrame}
    cfg         Per-pair strategy params from config.get_pair_config().
                Gate values (threshold / ADX / confidence) are superseded by
                current adaptive values when the engine is enabled.

    The returned dict contains the signal fields used by main.py, plus an
    optional `_ctx` sub-dict with indicator values for context recording.
    """
    if cfg is None:
        cfg = config.get_pair_config(config.SYMBOL)

    # ── Load adaptive parameters (graceful fallback to cfg) ───────────────────
    adaptive = _load_adaptive_params(db)

    signal_threshold = _adaptive_val(adaptive, "SIGNAL_THRESHOLD", cfg["SIGNAL_THRESHOLD"])
    min_adx          = _adaptive_val(adaptive, "MIN_ADX",           cfg["MIN_ADX"])
    min_conf         = _adaptive_val(adaptive, "MIN_CONFIDENCE",     cfg["MIN_CONFIDENCE"])
    sl_mult          = _adaptive_val(adaptive, "SL_ATR_MULT",        cfg["SL_ATR_MULT"])
    tp1_mult         = _adaptive_val(adaptive, "TP1_ATR_MULT",       cfg["TP1_ATR_MULT"])
    tp2_mult         = _adaptive_val(adaptive, "TP2_ATR_MULT",       cfg["TP2_ATR_MULT"])

    # ── Score every confirmation timeframe ────────────────────────────────────
    tf_scores: dict[str, float] = {}
    tf_votes:  dict[str, dict]  = {}
    for tf in cfg["CONFIRM_TIMEFRAMES"]:
        if tf not in timeframes:
            return None
        s, v = score_timeframe(db, timeframes[tf], adaptive_params=adaptive)
        tf_scores[tf] = s
        tf_votes[tf]  = v

    # All timeframes must agree in direction
    signs = {1 if s > 0 else -1 if s < 0 else 0 for s in tf_scores.values()}
    if len(signs) != 1 or 0 in signs:
        return None

    primary_score = tf_scores[cfg["PRIMARY_TF"]]
    if abs(primary_score) < signal_threshold:
        return None

    direction  = "BUY" if primary_score > 0 else "SELL"
    primary_df = timeframes[cfg["PRIMARY_TF"]]

    # ── ADX gate ──────────────────────────────────────────────────────────────
    adx_period = int(_adaptive_val(adaptive, "ADX_PERIOD",
                                   cfg.get("ADX_PERIOD", 14)))
    adx = trend_strength(primary_df, length=adx_period)
    if adx < min_adx:
        return None

    # ── Confidence gate ───────────────────────────────────────────────────────
    conf = confidence_pct(db, primary_score, cfg,
                          active_votes=tf_votes[cfg["PRIMARY_TF"]])
    if conf < min_conf:
        return None

    # ── Entry / SL / TP ───────────────────────────────────────────────────────
    entry = float(primary_df["close"].iloc[-1])
    atr   = compute_atr(primary_df)

    if direction == "BUY":
        tp1 = entry + tp1_mult * atr
        tp2 = entry + tp2_mult * atr
        sl  = entry - sl_mult  * atr
    else:
        tp1 = entry - tp1_mult * atr
        tp2 = entry - tp2_mult * atr
        sl  = entry + sl_mult  * atr

    reward = abs(tp1 - entry)
    risk   = abs(entry - sl)
    rr     = round(reward / risk, 2) if risk > 0 else 0.0

    contributors = tf_votes[cfg["PRIMARY_TF"]]

    # ── Collect raw indicator values for context recording ────────────────────
    indicator_values = {}
    try:
        indicator_values = get_indicator_values(primary_df, params=adaptive)
        indicator_values["atr"] = atr
    except Exception:
        pass

    # ── Build weight snapshot for context recording ───────────────────────────
    weights_snapshot = {}
    try:
        weights_snapshot = storage.all_weights(db)
    except Exception:
        pass

    return {
        # ── Standard signal fields (existing consumers) ───────────────────────
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
        # ── Adaptive engine context (ignored by existing consumers) ───────────
        "_ctx": {
            "indicator_values": indicator_values,
            "adaptive_params":  adaptive,
            "weights":          weights_snapshot,
        },
    }
