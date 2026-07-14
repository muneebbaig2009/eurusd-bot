"""Central configuration. All secrets come from environment variables."""
import os
import re

# --- Secrets (set these as environment variables / GitHub Secrets) ---
TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# --- Trading instruments ---
# Each pair runs independently: its own database, its own dashboard JSON,
# and its own self-learning technique weights.
PAIRS = ["EUR/USD", "GBP/USD"]

# Backwards-compatible single symbol (first pair). Most code now takes a `symbol`
# argument explicitly; this is only a default.
SYMBOL = PAIRS[0]


def slug(symbol: str) -> str:
    """Turn 'EUR/USD' into 'eurusd' for filenames."""
    return re.sub(r"[^a-z0-9]", "", symbol.lower())


def db_path(symbol: str) -> str:
    """Per-pair SQLite file, e.g. signals_eurusd.db."""
    return f"signals_{slug(symbol)}.db"


def data_json_path(symbol: str) -> str:
    """Per-pair dashboard JSON, e.g. docs/data_eurusd.json."""
    return f"docs/data_{slug(symbol)}.json"


# Timeframes we pull from Twelve Data. (3h is resampled from 1h separately.)
TIMEFRAMES = {
    "5min": "5min",
    "1h": "1h",
    "1day": "1day",
}

# How many candles to request per timeframe
CANDLE_COUNT = 200

# --- Signal logic ---
# Combined weighted score must exceed this (absolute) to fire a signal.
SIGNAL_THRESHOLD = 1.5

# ATR multipliers for stop-loss (volatility based)
SL_ATR_MULT = 1.5

# Two take-profit targets: a near target (TP1) and a far target (TP2).
# TP1 is set wider than SL so the primary target carries positive risk-reward.
TP1_ATR_MULT = 2.25
TP2_ATR_MULT = 3.5

# Kept for backwards compatibility (TP1 is the primary target used for win/loss).
TP_ATR_MULT = TP1_ATR_MULT

# Confidence floor/ceiling so the % never reads as absolute certainty.
CONF_MIN = 40
CONF_MAX = 95

# Minimum confidence % required to post a signal (filters low-conviction setups).
MIN_CONFIDENCE = 55

# Minimum ADX value to allow a signal (filters choppy, non-trending markets).
MIN_ADX = 12

# Bars to wait after a signal closes before allowing a new entry.
# Prevents chain-losses from immediately re-entering after a quick SL hit.
SIGNAL_COOLDOWN_BARS = 4

# Timeframes that must agree for a signal to be valid (multi-tf confirmation)
CONFIRM_TIMEFRAMES = ["1h", "1day"]

# Primary timeframe used for entry price + ATR
PRIMARY_TF = "1h"

# --- Learning ---
# Starting weight for every technique
DEFAULT_WEIGHT = 1.0
# How much to nudge a weight on a win / loss
LEARN_RATE = 0.05
# Keep weights inside this band so nothing dominates or dies completely
MIN_WEIGHT = 0.2
MAX_WEIGHT = 3.0
