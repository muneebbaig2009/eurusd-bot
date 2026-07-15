"""
MT5 Walk-Forward Backtest with Parameter Grid Search

Fetches historical OHLC from the running MT5 terminal, walks forward through
1-hour bars collecting signal candidates (bars where 1h + 1day agree in
direction), pre-computes trade outcomes for every SL/TP combination, then
grid-searches to find the parameter set with the best expectancy.

Usage  (MT5 terminal must be running):
    python mt5_backtest.py              # EUR/USD, 180-day period
    python mt5_backtest.py --days 90
    python mt5_backtest.py --pair GBP/USD
    python mt5_backtest.py --apply      # auto-write best params to config.py
"""
import os, sys, re, argparse, tempfile, io
os.environ.setdefault("EXECUTION_MODE", "mt5")
# Force UTF-8 on Windows consoles (avoids cp1252 UnicodeEncodeError)
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from datetime import datetime, timedelta, timezone
import MetaTrader5 as mt5
import pandas as pd

import config, storage
from signal_engine import score_timeframe, compute_atr, trend_strength, confidence_pct

# ── Parameter grid ───────────────────────────────────────────────────────────
SL_GRID     = [1.0, 1.25, 1.5, 1.75, 2.0]
TP_GRID     = [1.5, 2.0, 2.25, 2.5, 3.0]
THRESH_GRID = [1.0, 1.5, 2.0, 2.5]
CONF_GRID   = [50,  55,  60,  65]
MIN_ADX_FIX = 12       # kept constant (not grid-searched to limit scope)
COOLDOWN_H  = config.SIGNAL_COOLDOWN_BARS

# ── Account constants (mirror config.py — intentionally not imported from it
#    so backtest results are stable even if you patch config during the run) ──
_PIP      = 0.0001
_PV       = 10.0        # USD per pip per standard lot
_INIT_BAL = 100.0
_RISK_PCT = 0.02
_MIN_LOT  = 0.01
_LOT_STEP = 0.01


# ── Helpers ──────────────────────────────────────────────────────────────────

def _to_df(rates) -> pd.DataFrame:
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df.set_index("time")[["open", "high", "low", "close"]]


def _lot(balance: float, entry: float, sl: float) -> float:
    sl_pips = abs(entry - sl) / _PIP
    if sl_pips < 1:
        return _MIN_LOT
    raw = (balance * _RISK_PCT) / (sl_pips * _PV)
    stepped = round(raw / _LOT_STEP) * _LOT_STEP
    return max(_MIN_LOT, min(stepped, 100.0))


def _scan(future: pd.DataFrame, direction: str, sl: float, tp: float):
    """Scan 5min candles forward; return (status, price, time) or None."""
    for ts, row in future.iterrows():
        hi, lo = float(row["high"]), float(row["low"])
        if direction == "BUY":
            if lo <= sl:  return "LOSS", sl, ts
            if hi >= tp:  return "WIN",  tp, ts
        else:
            if hi >= sl:  return "LOSS", sl, ts
            if lo <= tp:  return "WIN",  tp, ts
    return None


# ── Phase 1: walk-forward candidate collection ───────────────────────────────

def collect_candidates(df_1h, df_1day, tmp_db: str) -> list:
    """Slide a 200-bar window through df_1h.  For each bar where 1h and
    1day agree in direction, record raw score/conf/adx/atr — no threshold
    filters yet so the grid search can apply them post-hoc."""
    out = []
    n   = len(df_1h)
    for i in range(200, n):
        t      = df_1h.index[i]
        win_1h = df_1h.iloc[max(0, i - 200): i + 1]
        win_1d = df_1day[df_1day.index <= t].tail(200)
        if len(win_1d) < 30:
            continue
        try:
            s1h, _ = score_timeframe(tmp_db, win_1h)
            s1d, _ = score_timeframe(tmp_db, win_1d)
        except Exception:
            continue
        if s1h == 0 or s1d == 0 or (s1h > 0) != (s1d > 0):
            continue
        out.append({
            "time":      t,
            "direction": "BUY" if s1h > 0 else "SELL",
            "entry":     float(win_1h["close"].iloc[-1]),
            "atr":       compute_atr(win_1h),
            "score":     s1h,
            "conf":      confidence_pct(tmp_db, s1h),
            "adx":       trend_strength(win_1h),
        })
    return out


# ── Phase 2: pre-compute outcomes ────────────────────────────────────────────

def precompute_outcomes(candidates: list, df_5min: pd.DataFrame) -> dict:
    """Outcome for every (candidate_idx, sl_grid_idx, tp_grid_idx) triplet.
    Stored once, reused by every grid-search simulation pass."""
    outcomes = {}
    total = len(candidates) * len(SL_GRID) * len(TP_GRID)
    done  = 0
    for ci, c in enumerate(candidates):
        future = df_5min[df_5min.index > c["time"]].head(2000)
        for si, sl_m in enumerate(SL_GRID):
            for ti, tp_m in enumerate(TP_GRID):
                e, a = c["entry"], c["atr"]
                if c["direction"] == "BUY":
                    sl, tp = e - sl_m * a, e + tp_m * a
                else:
                    sl, tp = e + sl_m * a, e - tp_m * a
                outcomes[(ci, si, ti)] = _scan(future, c["direction"], sl, tp)
                done += 1
                if done % 200 == 0:
                    print(f"  outcomes: {done}/{total} ({done*100//total}%)", end="\r")
    print(f"  outcomes: {total}/{total} (100%)          ")
    return outcomes


# ── Phase 3: simulate one parameter combo ────────────────────────────────────

def _sim(candidates, outcomes, si, ti, sl_m, tp_m, threshold, min_conf):
    balance   = _INIT_BAL
    last_ct   = None
    trades    = []
    for ci, c in enumerate(candidates):
        if abs(c["score"]) < threshold: continue
        if c["conf"]        < min_conf: continue
        if c["adx"]         < MIN_ADX_FIX: continue
        if last_ct is not None:
            if (c["time"] - last_ct).total_seconds() / 3600 < COOLDOWN_H:
                continue
        res = outcomes.get((ci, si, ti))
        if res is None: continue
        status, close_price, close_time = res
        e   = c["entry"]; a = c["atr"]
        sl  = (e - sl_m * a) if c["direction"] == "BUY" else (e + sl_m * a)
        lot = _lot(balance, e, sl)
        pip = abs(close_price - e) / _PIP
        pnl = lot * pip * _PV * (1 if status == "WIN" else -1)
        balance  = round(balance + pnl, 2)
        last_ct  = close_time
        trades.append({"s": status, "pnl": round(pnl, 2), "bal": balance})
    return trades


def _metrics(trades):
    n = len(trades)
    if n == 0: return None
    wins  = sum(1 for t in trades if t["s"] == "WIN")
    pnls  = [t["pnl"] for t in trades]
    peak  = _INIT_BAL; max_dd = 0.0
    for t in trades:
        if t["bal"] > peak: peak = t["bal"]
        dd = (peak - t["bal"]) / peak * 100
        if dd > max_dd: max_dd = dd
    gw = sum(p for p in pnls if p > 0)
    gl = sum(-p for p in pnls if p < 0)
    return {
        "n":    n,
        "wr":   round(wins / n * 100, 1),
        "pnl":  round(trades[-1]["bal"] - _INIT_BAL, 2),
        "exp":  round(sum(pnls) / n, 2),
        "dd":   round(max_dd, 1),
        "pf":   round(gw / gl, 2) if gl > 0 else 99.0,
    }


# ── Phase 4: grid search ─────────────────────────────────────────────────────

def grid_search(candidates, outcomes) -> list:
    results = []
    combos  = len(SL_GRID) * len(TP_GRID) * len(THRESH_GRID) * len(CONF_GRID)
    done    = 0
    for si, sl_m in enumerate(SL_GRID):
        for ti, tp_m in enumerate(TP_GRID):
            for thresh in THRESH_GRID:
                for min_c in CONF_GRID:
                    trades = _sim(candidates, outcomes, si, ti, sl_m, tp_m, thresh, min_c)
                    done  += 1
                    if done % 60 == 0:
                        print(f"  grid: {done}/{combos}", end="\r")
                    if len(trades) < 5: continue
                    m = _metrics(trades)
                    if m is None: continue
                    # Composite: expectancy × win-rate, penalise drawdown
                    score = m["exp"] * (m["wr"] / 100) / max(1.0, m["dd"] / 10)
                    results.append({"sl": sl_m, "tp": tp_m, "thr": thresh,
                                    "mc": min_c, "_s": score, **m})
    print(f"  grid: {combos}/{combos} done          ")
    results.sort(key=lambda r: r["_s"], reverse=True)
    return results


# ── Output ───────────────────────────────────────────────────────────────────

def _print_table(results, n=10):
    H = f"{'#':>3}  {'SL':>5}  {'TP':>5}  {'Thr':>4}  {'Conf':>4}  " \
        f"{'N':>4}  {'WR%':>6}  {'P&L':>8}  {'DD%':>5}  {'E$/t':>6}  {'PF':>5}"
    bar = "─" * len(H)
    print(f"\n{bar}\n{H}\n{bar}")
    for rank, r in enumerate(results[:n], 1):
        pnl = f"+${r['pnl']:.2f}" if r["pnl"] >= 0 else f"-${abs(r['pnl']):.2f}"
        exp = f"+{r['exp']:.2f}"  if r["exp"] >= 0 else f"{r['exp']:.2f}"
        print(f"{rank:>3}  {r['sl']:>5}  {r['tp']:>5}  {r['thr']:>4}  {r['mc']:>4}  "
              f"{r['n']:>4}  {r['wr']:>6}  {pnl:>8}  {r['dd']:>5}  {exp:>6}  {r['pf']:>5}")
    print(bar)


def _apply(best):
    with open("config.py") as f:
        txt = f.read()
    patches = [
        (r"SL_ATR_MULT\s*=\s*[\d.]+",     f"SL_ATR_MULT = {best['sl']}"),
        (r"TP1_ATR_MULT\s*=\s*[\d.]+",    f"TP1_ATR_MULT = {best['tp']}"),
        (r"TP_ATR_MULT\s*=\s*TP1_ATR_MULT", "TP_ATR_MULT = TP1_ATR_MULT"),  # keep alias
        (r"SIGNAL_THRESHOLD\s*=\s*[\d.]+", f"SIGNAL_THRESHOLD = {best['thr']}"),
        (r"MIN_CONFIDENCE\s*=\s*\d+",      f"MIN_CONFIDENCE = {int(best['mc'])}"),
    ]
    for pat, rep in patches:
        txt = re.sub(pat, rep, txt)
    with open("config.py", "w") as f:
        f.write(txt)
    print(f"\n  config.py updated:")
    print(f"    SL_ATR_MULT      = {best['sl']}")
    print(f"    TP1_ATR_MULT     = {best['tp']}")
    print(f"    SIGNAL_THRESHOLD = {best['thr']}")
    print(f"    MIN_CONFIDENCE   = {int(best['mc'])}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="MT5 Walk-Forward Backtest")
    ap.add_argument("--days",  type=int, default=180)
    ap.add_argument("--pair",  default="EUR/USD")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    sym     = args.pair.replace("/", "")
    end_dt  = datetime.now(timezone.utc)
    beg_dt  = end_dt - timedelta(days=args.days)
    day_beg = end_dt - timedelta(days=args.days + 730)

    print(f"\n{'='*62}")
    print(f"  MT5 Walk-Forward Backtest  |  {args.pair}")
    print(f"  {beg_dt:%Y-%m-%d} -> {end_dt:%Y-%m-%d}  ({args.days} days)")
    grid_n = len(SL_GRID) * len(TP_GRID) * len(THRESH_GRID) * len(CONF_GRID)
    print(f"  Grid: {grid_n} combos  "
          f"({len(SL_GRID)} SL × {len(TP_GRID)} TP × {len(THRESH_GRID)} Thresh × {len(CONF_GRID)} Conf)")
    print(f"{'='*62}\n")

    import mt5_executor
    mt5_executor.connect()
    tmp_db = tempfile.mktemp(suffix=".db")

    try:
        # ── Fetch ─────────────────────────────────────────────────────────
        print("Fetching historical data from MT5...")
        r1h   = mt5.copy_rates_range(sym, mt5.TIMEFRAME_H1, beg_dt,  end_dt)
        r1d   = mt5.copy_rates_range(sym, mt5.TIMEFRAME_D1, day_beg, end_dt)
        r5m   = mt5.copy_rates_range(sym, mt5.TIMEFRAME_M5, beg_dt,  end_dt)

        if r1h is None or len(r1h) == 0:
            sys.exit(f"No 1h data for {sym}. Is MT5 running and {sym} available?")

        df_1h   = _to_df(r1h)
        df_1day = _to_df(r1d)
        df_5min = _to_df(r5m)
        print(f"  1h: {len(df_1h)}  1day: {len(df_1day)}  5min: {len(df_5min)}")

        if len(df_1h) < 210:
            sys.exit(f"Only {len(df_1h)} 1h bars. Use --days 30 at minimum.")

        # ── Candidates ────────────────────────────────────────────────────
        storage.init_db(tmp_db)   # default weights → isolates param effect
        print(f"\nWalking forward through {len(df_1h)} 1h bars...")
        cands = collect_candidates(df_1h, df_1day, tmp_db)
        print(f"  Signal candidates: {len(cands)}")
        if len(cands) < 10:
            sys.exit("Too few candidates. Try --days 90 or --days 180.")

        # ── Pre-compute ───────────────────────────────────────────────────
        print(f"\nPre-computing outcomes ({len(cands)} × {len(SL_GRID)*len(TP_GRID)} SL/TP)...")
        outcomes = precompute_outcomes(cands, df_5min)

        # ── Grid search ───────────────────────────────────────────────────
        print(f"\nGrid searching {grid_n} parameter combos...")
        results = grid_search(cands, outcomes)

        if not results:
            sys.exit("No combos yielded ≥ 5 trades. Use a longer --days period.")

        # ── Print results ─────────────────────────────────────────────────
        print(f"\nTop 10 configurations  (sorted by composite score):")
        _print_table(results, n=10)

        best = results[0]
        print(f"\n  CURRENT config → SL={config.SL_ATR_MULT}  "
              f"TP={config.TP1_ATR_MULT}  Thresh={config.SIGNAL_THRESHOLD}  "
              f"Conf={config.MIN_CONFIDENCE}")
        print(f"  BEST found     → SL={best['sl']}  TP={best['tp']}  "
              f"Thresh={best['thr']}  Conf={best['mc']}")
        print(f"  Best: {best['n']} trades  WR {best['wr']}%  "
              f"P&L ${best['pnl']:+.2f}  DD {best['dd']}%  "
              f"Expectancy ${best['exp']:+.2f}/trade")

        # Baseline simulation with current config (closest grid point)
        ci = min(range(len(SL_GRID)), key=lambda i: abs(SL_GRID[i] - config.SL_ATR_MULT))
        ti = min(range(len(TP_GRID)), key=lambda i: abs(TP_GRID[i] - config.TP1_ATR_MULT))
        cur_trades = _sim(cands, outcomes, ci, ti,
                          SL_GRID[ci], TP_GRID[ti],
                          config.SIGNAL_THRESHOLD, config.MIN_CONFIDENCE)
        cm = _metrics(cur_trades)
        if cm:
            print(f"  Current: {cm['n']} trades  WR {cm['wr']}%  "
                  f"P&L ${cm['pnl']:+.2f}  DD {cm['dd']}%  "
                  f"Expectancy ${cm['exp']:+.2f}/trade")

        # ── Apply ─────────────────────────────────────────────────────────
        if args.apply:
            _apply(best)
        else:
            try:
                ans = input("\nApply best config to config.py? [y/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = "n"
            _apply(best) if ans == "y" else print("  config.py unchanged.")

    finally:
        try: os.remove(tmp_db)
        except Exception: pass
        mt5_executor.disconnect()


if __name__ == "__main__":
    main()
