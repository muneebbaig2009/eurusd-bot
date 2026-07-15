"""Central configuration. All secrets come from environment variables."""
import os
import re

# --- Secrets (set these as environment variables / GitHub Secrets) ---
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# --- MT5 credentials ---
MT5_LOGIN    = int(os.environ.get("MT5_LOGIN", 0) or 0)
MT5_PASSWORD = os.environ.get("MT5_PASSWORD", "")
MT5_SERVER   = os.environ.get("MT5_SERVER",   "")

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


# How many candles to request per timeframe
CANDLE_COUNT = 200

# --- Signal logic ---
# Combined weighted score must exceed this (absolute) to fire a signal.
SIGNAL_THRESHOLD = 1.0

# ATR multipliers for stop-loss (volatility based)
SL_ATR_MULT = 2.0

# Two take-profit targets: a near target (TP1) and a far target (TP2).
# TP1 is set wider than SL so the primary target carries positive risk-reward.
TP1_ATR_MULT = 1.5
TP2_ATR_MULT = 3.5

# Kept for backwards compatibility (TP1 is the primary target used for win/loss).
TP_ATR_MULT = TP1_ATR_MULT

# Confidence floor/ceiling so the % never reads as absolute certainty.
CONF_MIN = 40
CONF_MAX = 95

# Minimum confidence % required to post a signal (filters low-conviction setups).
MIN_CONFIDENCE = 60

# Minimum ADX value to allow a signal (filters choppy, non-trending markets).
MIN_ADX = 12

# Bars to wait after a signal closes before allowing a new entry.
# Prevents chain-losses from immediately re-entering after a quick SL hit.
SIGNAL_COOLDOWN_BARS = 4

# Timeframes that must agree for a signal to be valid (multi-tf confirmation)
CONFIRM_TIMEFRAMES = ["1h", "1day"]

# Primary timeframe used for entry price + ATR
PRIMARY_TF = "1h"

# --- Per-pair strategy overrides ---
# EUR/USD: backtest-optimised, lower volatility (~30-50 pip/day), tight spreads
# GBP/USD: ~40% more volatile (~60-90 pip/day), wider spreads, momentum-driven
#   → wider ATR stops to handle noise, wider TP for momentum moves,
#     stricter threshold and ADX to filter lower-quality setups
PAIR_CONFIG = {
    "EUR/USD": {
        "SL_ATR_MULT":          2.0,
        "TP1_ATR_MULT":         1.5,
        "TP2_ATR_MULT":         3.5,
        "SIGNAL_THRESHOLD":     1.0,
        "MIN_CONFIDENCE":       60,
        "MIN_ADX":              12,
        "SIGNAL_COOLDOWN_BARS": 4,
    },
    "GBP/USD": {
        "SL_ATR_MULT":          2.5,   # wider: absorbs GBP/USD noise
        "TP1_ATR_MULT":         2.0,   # wider: captures momentum swings
        "TP2_ATR_MULT":         4.0,
        "SIGNAL_THRESHOLD":     1.2,   # stricter: GBP/USD more unpredictable
        "MIN_CONFIDENCE":       65,    # stricter
        "MIN_ADX":              15,    # stronger trend required
        "SIGNAL_COOLDOWN_BARS": 6,     # longer cooldown between signals
    },
}


def get_pair_config(symbol: str) -> dict:
    """Return merged strategy parameters for a trading pair."""
    base = {
        "SL_ATR_MULT":          SL_ATR_MULT,
        "TP1_ATR_MULT":         TP1_ATR_MULT,
        "TP2_ATR_MULT":         TP2_ATR_MULT,
        "SIGNAL_THRESHOLD":     SIGNAL_THRESHOLD,
        "MIN_CONFIDENCE":       MIN_CONFIDENCE,
        "MIN_ADX":              MIN_ADX,
        "SIGNAL_COOLDOWN_BARS": SIGNAL_COOLDOWN_BARS,
        "CONF_MIN":             CONF_MIN,
        "CONF_MAX":             CONF_MAX,
        "CONFIRM_TIMEFRAMES":   CONFIRM_TIMEFRAMES,
        "PRIMARY_TF":           PRIMARY_TF,
    }
    return {**base, **PAIR_CONFIG.get(symbol, {})}

# --- Demo account ---
DEMO_INITIAL_BALANCE = 100.0   # starting balance in USD
DEMO_RISK_PCT        = 0.02    # 2% of balance risked per trade (used to compute lot size)
DEMO_RISK_PER_TRADE  = round(DEMO_INITIAL_BALANCE * DEMO_RISK_PCT, 2)  # $2.00 at start

# --- MT5 Lot Sizing (EUR/USD and GBP/USD both have USD as quote currency) ---
PIP_SIZE           = 0.0001   # 1 pip = 0.0001 for 5-digit EUR/USD, GBP/USD
PIP_VALUE_PER_LOT  = 10.0    # USD per pip per standard lot (100,000 units)
MIN_LOT            = 0.01     # micro lot (1,000 units) — smallest practical size
LOT_STEP           = 0.01     # lot increment
MAX_LOT            = 100.0    # safety cap

# --- Learning ---
# Starting weight for every technique
DEFAULT_WEIGHT = 1.0
# How much to nudge a weight on a win / loss
LEARN_RATE = 0.05
# Keep weights inside this band so nothing dominates or dies completely
MIN_WEIGHT = 0.2
MAX_WEIGHT = 3.0
