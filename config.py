"""Central configuration. All secrets come from environment variables."""
import os

# --- Secrets (set these as environment variables / GitHub Secrets) ---
TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# --- Trading instrument ---
SYMBOL = "EUR/USD"

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

# --- Storage ---
DB_PATH = os.environ.get("DB_PATH", "signals.db")
