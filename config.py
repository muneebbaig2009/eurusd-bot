"""Central configuration. All secrets come from environment variables or .env file."""
import os
import re
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass

# --- Secrets (set in .env file or as environment variables / GitHub Secrets) ---
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


# How many candles to request per timeframe (used as default)
CANDLE_COUNT = 300

# Per-timeframe bar counts — shorter timeframes need more bars for EMA(200) warmup
CANDLE_COUNT_MAP = {
    "15m":  300,
    "30m":  300,
    "1h":   300,
    "1day": 300,
}

# --- Signal logic ---
# Combined weighted score must exceed this (absolute) to fire a signal.
SIGNAL_THRESHOLD = 1.5

# ATR multipliers for stop-loss (volatility based)
SL_ATR_MULT = 1.5

# Two take-profit targets: a near target (TP1) and a far target (TP2).
TP1_ATR_MULT = 1.5
TP2_ATR_MULT = 3.0

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
# Interpreted as number of primary-timeframe bars (converted to hours in main.py).
SIGNAL_COOLDOWN_BARS = 4

# ── Trading mode ─────────────────────────────────────────────────────────────
# 360-day intraday vs interday comparison (2026-07-16) — run A:
#   Intraday (overlap session, M30+H1) composite score: +27.2
#   Interday (swing, H4+D1)           composite score: -43.7
#   Interday gen_ratio 0.31 (poor generalisation) vs intraday 1.49 (improved OOS)
#   Interday failed spectacularly in test: PF 0.42, DD 99.9%, ROI -88.5%
#
# 1000-day study (2026-07-16) — run B: train 700d / test 300d, 200+100 random configs
#   Data range: 2023-10-10 → 2026-07-16 (EURUSD+GBPUSD, 34k M30 bars)
#   Intraday OOS score: -36.38  |  Interday OOS score: -47.79  → INTRADAY WINS
#   Interday gen_ratio -2.41: catastrophic overfitting confirmed for the 2nd time
#   Interday OOS: PF 0.74, ROI -111%, DD 116% — account wipeout in test period
#
# Position management study (5 configs, full 1000d):
#   ALL five configs produced identical outcomes (n=51, PF=0.45, DD=14%)
#   Root cause: 0.054 trades/day is well below single-position capacity;
#   minimum lot floor ($0.01) means lot scaling has no effect on a $100 account.
#   CONFIRMED: single position per pair is the correct and sufficient setting.
#
# Session: London-NY overlap (12-17 UTC) independently re-discovered 3x as best
# Entry TF: M30 independently re-discovered 3x as best entry timeframe
# Decision: intraday confirmed. Existing 885baff targeted config is the best found.
TRADING_MODE = "intraday"

# Safety: close any trade that has been open longer than this (hours).
# Prevents accidental overnight holding for an intraday strategy.
# Set to 0 to disable.
MAX_HOLD_HOURS = 8

# Default timeframe stack — overridden per pair below
CONFIRM_TIMEFRAMES = ["30m", "1h"]
PRIMARY_TF         = "30m"

# Bar size in hours for each supported timeframe (used to compute cooldown hours)
TF_BAR_HOURS = {
    "5min": 1/12,
    "15m":  0.25,
    "30m":  0.5,
    "1h":   1.0,
    "1day": 24.0,
}

# Session filter — only signal during the London-NY overlap (12:00-16:59 UTC).
# 360-day backtest: EUR PF jumped from 1.17 → 1.73 with overlap filter applied.
# Set to None or "all" to disable.
SESSION_FILTER = "overlap"

# Session windows (UTC hours; bar open hour must be in the set to allow signal)
SESSION_HOURS = {
    "all":       set(range(24)),
    "london":    set(range(7, 17)),
    "ny":        set(range(12, 21)),
    "london_ny": set(range(7, 21)),
    "overlap":   set(range(12, 17)),   # London-NY overlap — highest quality signals
    "morning":   {6, 7, 8},           # London open (11:00-14:00 PKT / 06:00-09:00 UTC)
    "dual":      {6, 7, 8} | set(range(12, 17)),  # morning + overlap combined
}

# --- Per-pair strategy overrides ---
# 360-day grid-search optimisation (2026-07-17) — 36 SL×TP×Threshold combos:
#   Ranking metric: (avg_ROI/avg_MaxDD) × avg_PF  (favours return per unit drawdown)
#   Best combo: SL=1.5, TP=4.0, Threshold=2.0  →  R:R 2.67
#     EURUSD: ROI +63.62%  PF 1.333  WR 23.4%  MaxDD 10.30%  Ret/DD 6.18
#     GBPUSD: ROI +58.72%  PF 1.256  WR 24.4%  MaxDD 24.84%  Ret/DD 2.36
#     Combined score 4.51 (vs 0.65 for previous TP=0.75 config)
#   Key insight: threshold=2.0 dominates — high-conviction signals only.
#   Low WR (~23%) is fine because each win covers 2.67 losses.
#   MAX_HOLD_HOURS kept at 8h — timeout trades close in partial profit on average.
PAIR_CONFIG = {
    "EUR/USD": {
        "PRIMARY_TF":           "30m",
        "CONFIRM_TIMEFRAMES":   ["30m", "1h"],
        "SL_ATR_MULT":          1.5,
        "TP1_ATR_MULT":         4.0,   # R:R 2.67 — grid-search winner 2026-07-17
        "TP2_ATR_MULT":         6.0,
        "SIGNAL_THRESHOLD":     2.0,   # raised from 1.2 — high-conviction only
        "MIN_CONFIDENCE":       55,
        "MIN_ADX":              15,
        "ADX_PERIOD":           14,
        "SIGNAL_COOLDOWN_BARS": 2,
        "SESSION_FILTER":       "overlap",
    },
    "GBP/USD": {
        "PRIMARY_TF":           "30m",
        "CONFIRM_TIMEFRAMES":   ["30m", "1h"],
        "SL_ATR_MULT":          1.5,
        "TP1_ATR_MULT":         4.0,   # R:R 2.67 — grid-search winner 2026-07-17
        "TP2_ATR_MULT":         6.0,
        "SIGNAL_THRESHOLD":     2.0,
        "MIN_CONFIDENCE":       55,
        "MIN_ADX":              15,
        "ADX_PERIOD":           14,
        "SIGNAL_COOLDOWN_BARS": 2,
        "SESSION_FILTER":       "overlap"
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
        "ADX_PERIOD":           14,
        "SIGNAL_COOLDOWN_BARS": SIGNAL_COOLDOWN_BARS,
        "CONF_MIN":             CONF_MIN,
        "CONF_MAX":             CONF_MAX,
        "CONFIRM_TIMEFRAMES":   CONFIRM_TIMEFRAMES,
        "PRIMARY_TF":           PRIMARY_TF,
        "SESSION_FILTER":       SESSION_FILTER,
        "TRADING_MODE":         TRADING_MODE,
        "MAX_HOLD_HOURS":       MAX_HOLD_HOURS,
    }
    return {**base, **PAIR_CONFIG.get(symbol, {})}


def cooldown_hours(symbol: str) -> float:
    """Cooldown in hours = SIGNAL_COOLDOWN_BARS × bar_size for the pair's primary TF."""
    cfg = get_pair_config(symbol)
    bar_h = TF_BAR_HOURS.get(cfg["PRIMARY_TF"], 1.0)
    return cfg["SIGNAL_COOLDOWN_BARS"] * bar_h

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

# ── Adaptive Strategy Optimization Engine ────────────────────────────────────
# Master switch — set False to disable ALL adaptive behaviour instantly.
ADAPTIVE_ENABLED = True

# Minimum closed-trade count before any parameter is updated.
ADAPTIVE_MIN_SAMPLE = 30

# Run batch parameter optimization every N closed trades.
ADAPTIVE_OPTIMIZE_EVERY = 50

# Run batch weight rebalancing every N closed trades.
ADAPTIVE_WEIGHT_BATCH = 50

# Minimum relative improvement required to accept a parameter change (5 %).
ADAPTIVE_MIN_IMPROVEMENT = 0.05

# Maximum fractional change per update cycle (20 % of parameter range).
ADAPTIVE_MAX_CHANGE_PCT = 0.20

# Shadow validation period: live trades to monitor after each param change.
ADAPTIVE_SHADOW_TRADES = 25

# Rollback if shadow avg_R drops more than this vs pre-change baseline.
ADAPTIVE_ROLLBACK_THRESHOLD = -0.15   # −15 %

# EWMA decay factor for rolling statistics (higher = more weight on recents).
ADAPTIVE_EWMA_ALPHA = 0.94

# Rolling window size for in-memory statistics.
ADAPTIVE_WINDOW = 100
