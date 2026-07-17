"""
360-Day Historical Backtest — Exact Current Strategy Settings
==============================================================
Connects to MT5, fetches 360 days of 30m + 1h data for EUR/USD
and GBP/USD, walks forward bar-by-bar applying the live strategy:
  session  : London-NY overlap (12-16 UTC)
  primary  : 30m  |  confirm: 1h
  threshold: 1.2  |  ADX ≥ 15  |  conf ≥ 55%
  SL       : 1.5×ATR  |  TP1: 0.75×ATR  (R:R 0.5)
  cooldown : 2 bars × 30m = 1h after each close

Starting capital: $100  |  Risk: 2% per trade
"""
import sys, os, json
from datetime import datetime, timezone

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import storage
from signal_engine import score_timeframe, compute_atr, trend_strength, confidence_pct

MT5_SYMBOLS   = {"EUR/USD": "EURUSD", "GBP/USD": "GBPUSD"}
SESSION_HOURS = config.SESSION_HOURS["overlap"]     # {12, 13, 14, 15, 16}
STARTING_BALANCE = 100.0
RISK_PCT         = 0.02
PIP_SIZE         = config.PIP_SIZE
PIP_VALUE_PER_LOT = config.PIP_VALUE_PER_LOT
MIN_LOT          = config.MIN_LOT
LOT_STEP         = config.LOT_STEP
MAX_LOT          = config.MAX_LOT
WARMUP_BARS      = 300
DAYS             = 360


# ── MT5 helpers ───────────────────────────────────────────────────────────────

def init_mt5():
    try:
        import MetaTrader5 as mt5
    except ImportError:
        print("MetaTrader5 package not installed.")
        sys.exit(1)
    if not mt5.initialize():
        print(f"MT5 init failed: {mt5.last_error()}")
        print("Make sure the MT5 terminal is running and logged in.")
        sys.exit(1)
    info = mt5.account_info()
    print(f"MT5: {info.login} @ {info.server}  balance=${info.balance:.2f}  currency={info.currency}")
    return mt5


def fetch_rates(mt5, sym: str, tf_str: str, days: int) -> pd.DataFrame:
    tf_map = {"30m": mt5.TIMEFRAME_M30, "1h": mt5.TIMEFRAME_H1}
    tf     = tf_map[tf_str]
    bars_per_day = 48 if tf_str == "30m" else 24
    total  = days * bars_per_day + WARMUP_BARS + 200
    rates  = mt5.copy_rates_from_pos(sym, tf, 0, total)
    if rates is None or len(rates) == 0:
        raise RuntimeError(f"No data returned for {sym} {tf_str}")
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.rename(columns={"tick_volume": "volume"})
    df = df[["time", "open", "high", "low", "close", "volume"]].set_index("time")
    # Keep only the required lookback from today
    cutoff = df.index[-1] - pd.Timedelta(days=days + WARMUP_BARS // bars_per_day + 10)
    df = df[df.index >= cutoff]
    return df


# ── Trade helpers ─────────────────────────────────────────────────────────────

def lot_from_risk(balance: float, entry: float, sl: float) -> float:
    risk_dollar = balance * RISK_PCT
    sl_pips = abs(entry - sl) / PIP_SIZE
    if sl_pips <= 0:
        return MIN_LOT
    lot = risk_dollar / (sl_pips * PIP_VALUE_PER_LOT)
    lot = max(MIN_LOT, round(lot / LOT_STEP) * LOT_STEP)
    return min(MAX_LOT, lot)


def scan_outcome(df30m: pd.DataFrame, entry_idx: int, direction: str,
                 tp: float, sl: float, max_bars: int = 16) -> dict:
    """Walk forward from entry bar scanning for TP or SL hit."""
    rows = df30m.iloc[entry_idx + 1 : entry_idx + 1 + max_bars]
    for k, (ts, row) in enumerate(rows.iterrows()):
        h, l = row["high"], row["low"]
        if direction == "BUY":
            if l <= sl:
                return {"result": "LOSS", "price": sl, "bars": k + 1, "ts": ts}
            if h >= tp:
                return {"result": "WIN",  "price": tp, "bars": k + 1, "ts": ts}
        else:
            if h >= sl:
                return {"result": "LOSS", "price": sl, "bars": k + 1, "ts": ts}
            if l <= tp:
                return {"result": "WIN",  "price": tp, "bars": k + 1, "ts": ts}
    last_ts, last_row = list(rows.iterrows())[-1] if len(rows) else (df30m.index[entry_idx], None)
    close_p = float(last_row["close"]) if last_row is not None else df30m.iloc[entry_idx]["close"]
    return {"result": "TIMEOUT", "price": close_p, "bars": len(rows), "ts": last_ts}


# ── Main backtest loop ────────────────────────────────────────────────────────

def backtest_pair(symbol: str, df30m: pd.DataFrame, df1h: pd.DataFrame) -> dict:
    cfg = config.get_pair_config(symbol)
    db  = config.db_path(symbol)

    balance     = STARTING_BALANCE
    max_balance = balance
    max_dd      = 0.0
    equity      = [balance]
    trades      = []

    n30  = len(df30m)
    rows = df30m.to_dict("index")   # faster than repeated iloc
    keys = list(rows.keys())        # list of timestamps
    i    = WARMUP_BARS              # current bar index

    while i < n30:
        ts       = keys[i]
        bar_hour = ts.hour

        # Session filter
        if bar_hour not in SESSION_HOURS:
            i += 1
            continue

        # ── Build indicator windows ───────────────────────────────────────
        window_30m = df30m.iloc[max(0, i - WARMUP_BARS) : i + 1]
        h1_slice   = df1h[df1h.index <= ts].tail(WARMUP_BARS)

        if len(h1_slice) < 50:
            i += 1
            continue

        # ── Score both timeframes ─────────────────────────────────────────
        try:
            s30, v30 = score_timeframe(db, window_30m)
            s1h, v1h = score_timeframe(db, h1_slice)
        except Exception:
            i += 1
            continue

        tf_scores = {"30m": s30, "1h": s1h}
        tf_votes  = {"30m": v30, "1h": v1h}

        signs = {1 if s > 0 else -1 if s < 0 else 0 for s in tf_scores.values()}
        if len(signs) != 1 or 0 in signs:
            i += 1
            continue

        primary_score = tf_scores[cfg["PRIMARY_TF"]]
        if abs(primary_score) < cfg["SIGNAL_THRESHOLD"]:
            i += 1
            continue

        # ADX gate
        try:
            adx = trend_strength(window_30m)
        except Exception:
            adx = 50
        if adx < cfg["MIN_ADX"]:
            i += 1
            continue

        # Confidence gate
        try:
            conf = confidence_pct(db, primary_score, cfg,
                                  active_votes=tf_votes[cfg["PRIMARY_TF"]])
        except Exception:
            conf = 0
        if conf < cfg["MIN_CONFIDENCE"]:
            i += 1
            continue

        # ── Signal fires ──────────────────────────────────────────────────
        direction = "BUY" if primary_score > 0 else "SELL"
        entry     = float(window_30m["close"].iloc[-1])
        try:
            atr = compute_atr(window_30m)
        except Exception:
            atr = entry * 0.001

        tp = entry + cfg["TP1_ATR_MULT"] * atr if direction == "BUY" else entry - cfg["TP1_ATR_MULT"] * atr
        sl = entry - cfg["SL_ATR_MULT"]  * atr if direction == "BUY" else entry + cfg["SL_ATR_MULT"]  * atr
        lot = lot_from_risk(balance, entry, sl)

        # ── Scan outcome ──────────────────────────────────────────────────
        out   = scan_outcome(df30m, i, direction, tp, sl, max_bars=16)
        close = out["price"]

        pnl_pips = (close - entry) / PIP_SIZE if direction == "BUY" else (entry - close) / PIP_SIZE
        pnl_usd  = pnl_pips * lot * PIP_VALUE_PER_LOT

        balance  += pnl_usd
        equity.append(round(balance, 4))
        max_balance = max(max_balance, balance)
        dd = (max_balance - balance) / max_balance * 100 if max_balance > 0 else 0
        max_dd = max(max_dd, dd)

        trades.append({
            "time":      str(ts)[:16],
            "direction": direction,
            "entry":     round(entry, 5),
            "tp":        round(tp, 5),
            "sl":        round(sl, 5),
            "lot":       lot,
            "atr":       round(atr, 5),
            "adx":       adx,
            "conf":      conf,
            "score":     round(primary_score, 3),
            "result":    out["result"],
            "close":     round(close, 5),
            "pnl_pips":  round(pnl_pips, 1),
            "pnl_usd":   round(pnl_usd, 4),
            "balance":   round(balance, 4),
        })

        # Advance past trade bars + cooldown
        i += out["bars"] + cfg["SIGNAL_COOLDOWN_BARS"] + 1

    # ── Summary ───────────────────────────────────────────────────────────────
    n      = len(trades)
    wins   = [t for t in trades if t["result"] == "WIN"]
    losses = [t for t in trades if t["result"] == "LOSS"]
    timeouts = [t for t in trades if t["result"] == "TIMEOUT"]

    gp = sum(t["pnl_usd"] for t in trades if t["pnl_usd"] > 0)
    gl = abs(sum(t["pnl_usd"] for t in trades if t["pnl_usd"] < 0))
    pf = round(gp / gl, 3) if gl > 0 else 999.0

    win_rate   = round(len(wins) / n * 100, 1) if n > 0 else 0.0
    net_pnl    = round(balance - STARTING_BALANCE, 4)
    roi_pct    = round(net_pnl / STARTING_BALANCE * 100, 2)
    avg_win    = round(sum(t["pnl_usd"] for t in wins) / len(wins), 4) if wins else 0
    avg_loss   = round(sum(t["pnl_usd"] for t in losses) / len(losses), 4) if losses else 0
    expectancy = round((len(wins) / n) * avg_win + (len(losses) / n) * avg_loss, 4) if n > 0 else 0

    return {
        "symbol":           symbol,
        "n_trades":         n,
        "wins":             len(wins),
        "losses":           len(losses),
        "timeouts":         len(timeouts),
        "win_rate":         win_rate,
        "gross_profit":     round(gp, 4),
        "gross_loss":       round(gl, 4),
        "profit_factor":    pf,
        "net_pnl":          net_pnl,
        "roi_pct":          roi_pct,
        "final_balance":    round(balance, 4),
        "max_drawdown_pct": round(max_dd, 2),
        "avg_win_usd":      avg_win,
        "avg_loss_usd":     avg_loss,
        "expectancy_usd":   expectancy,
        "equity_curve":     equity,
        "trades":           trades,
    }


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    sep = "=" * 62
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(sep)
    print(f"360-Day Backtest  |  Period ending {now}")
    print(f"Starting balance : ${STARTING_BALANCE:.2f}  |  Risk: {int(RISK_PCT*100)}% per trade")
    print(f"Session          : London-NY overlap (12-16 UTC)")
    cfg0 = config.get_pair_config("EUR/USD")
    rr   = round(cfg0["TP1_ATR_MULT"] / cfg0["SL_ATR_MULT"], 2)
    print(f"Primary TF       : {cfg0['PRIMARY_TF']}  |  Confirm: {cfg0['CONFIRM_TIMEFRAMES']}")
    print(f"Threshold        : {cfg0['SIGNAL_THRESHOLD']}  |  ADX >= {cfg0['MIN_ADX']}  |  Conf >= {cfg0['MIN_CONFIDENCE']}%")
    print(f"SL               : {cfg0['SL_ATR_MULT']}xATR  |  TP1: {cfg0['TP1_ATR_MULT']}xATR  (R:R {rr})")
    print(f"Cooldown         : {cfg0['SIGNAL_COOLDOWN_BARS']} x 30m after each close")
    print(sep)

    mt5 = init_mt5()

    all_results = {}
    for symbol in ["EUR/USD", "GBP/USD"]:
        sym = MT5_SYMBOLS[symbol]
        print(f"\n[{symbol}] Fetching {DAYS} days of data …")
        try:
            df30m = fetch_rates(mt5, sym, "30m", DAYS)
            df1h  = fetch_rates(mt5, sym, "1h",  DAYS)
        except RuntimeError as e:
            print(f"  SKIP: {e}")
            continue
        print(f"  30m bars: {len(df30m):,}  |  1h bars: {len(df1h):,}")
        print(f"  Range   : {df30m.index[0].date()} → {df30m.index[-1].date()}")
        print(f"  Backtesting …")

        r = backtest_pair(symbol, df30m, df1h)
        all_results[symbol] = r

        print(f"\n  ─── {symbol} Results ───────────────────────────────")
        print(f"  Trades        : {r['n_trades']}  (W {r['wins']}  L {r['losses']}  T {r['timeouts']})")
        print(f"  Win Rate      : {r['win_rate']}%")
        print(f"  Profit Factor : {r['profit_factor']}")
        print(f"  Net P&L       : ${r['net_pnl']:+.4f}")
        print(f"  ROI           : {r['roi_pct']:+.2f}%")
        print(f"  Final Balance : ${r['final_balance']:.4f}")
        print(f"  Max Drawdown  : {r['max_drawdown_pct']:.2f}%")
        print(f"  Avg Win       : ${r['avg_win_usd']:+.4f}")
        print(f"  Avg Loss      : ${r['avg_loss_usd']:+.4f}")
        print(f"  Expectancy    : ${r['expectancy_usd']:+.4f}/trade")

    mt5.shutdown()

    # ── Combined stats ────────────────────────────────────────────────────────
    if len(all_results) >= 2:
        total_n  = sum(r["n_trades"] for r in all_results.values())
        total_w  = sum(r["wins"]     for r in all_results.values())
        total_gp = sum(r["gross_profit"] for r in all_results.values())
        total_gl = sum(r["gross_loss"]   for r in all_results.values())
        comb_pf  = round(total_gp / total_gl, 3) if total_gl > 0 else 999.0
        comb_wr  = round(total_w / total_n * 100, 1) if total_n > 0 else 0
        comb_pnl = sum(r["net_pnl"] for r in all_results.values())

        print(f"\n{sep}")
        print(f"COMBINED  EUR/USD + GBP/USD  (360 days, ${STARTING_BALANCE:.0f} start per pair)")
        print(sep)
        print(f"  Total Trades  : {total_n}")
        print(f"  Win Rate      : {comb_wr}%")
        print(f"  Profit Factor : {comb_pf}")
        print(f"  Total Net P&L : ${comb_pnl:+.4f}")
        print(f"  Final Balance : ${STARTING_BALANCE + comb_pnl:.4f}  (per pair avg: ${STARTING_BALANCE + comb_pnl/2:.4f})")
        print(sep)

    # ── Save JSON results ─────────────────────────────────────────────────────
    out = {
        "run_at":           datetime.now(timezone.utc).isoformat(),
        "days":             DAYS,
        "starting_balance": STARTING_BALANCE,
        "strategy": {
            "session":    "overlap (12-16 UTC)",
            "primary_tf": "30m",
            "confirm_tf": "1h",
            "threshold":  1.2,
            "min_adx":    15,
            "min_conf":   55,
            "sl_mult":    1.5,
            "tp1_mult":   0.75,
            "cooldown":   "2 × 30m",
        },
        "results":  {k: {kk: vv for kk, vv in v.items() if kk not in ("trades", "equity_curve")}
                     for k, v in all_results.items()},
        "equity_curves": {k: v["equity_curve"] for k, v in all_results.items()},
        "trades":   {k: v["trades"]       for k, v in all_results.items()},
    }
    out_path = "backtest_360_results.json"
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\nFull results saved → {out_path}")


if __name__ == "__main__":
    main()
