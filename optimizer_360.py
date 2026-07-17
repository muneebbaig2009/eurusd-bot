"""
360-Day Strategy Optimizer
==========================
Pre-computes per-bar signals once per pair, then grid-searches
SL × TP × Threshold combinations to find the most robust settings.

Rules:
  - TP > SL always (R:R > 1.0)
  - Both pairs run in parallel via ThreadPoolExecutor
  - Ranked by Return/MaxDD ratio (want >= 2.0)

Usage:
  python optimizer_360.py
"""
import sys, os, json, time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import storage
from signal_engine import score_timeframe, compute_atr, trend_strength, confidence_pct

# ── Parameter grid ─────────────────────────────────────────────────────────
SL_MULTS   = [1.0, 1.5, 2.0]
TP_MULTS   = [1.5, 2.0, 2.5, 3.0, 4.0]
THRESHOLDS = [1.2, 1.5, 2.0]

# Fixed filters (not varied here)
MIN_ADX    = 15
MIN_CONF   = 55
COOLDOWN   = 2          # bars after trade close before next entry
MAX_BARS   = 32         # max 30m bars to hold a trade (16 hours)

PAIRS      = ["EUR/USD", "GBP/USD"]
MT5_SYMS   = {"EUR/USD": "EURUSD", "GBP/USD": "GBPUSD"}
WARMUP     = 300
DAYS       = 360
START_BAL  = 100.0
RISK_PCT   = 0.02


# ── MT5 data fetch ─────────────────────────────────────────────────────────

def init_mt5():
    try:
        import MetaTrader5 as mt5
    except ImportError:
        print("MetaTrader5 not installed."); sys.exit(1)
    if not mt5.initialize():
        print(f"MT5 init failed: {mt5.last_error()}"); sys.exit(1)
    info = mt5.account_info()
    print(f"MT5: {info.login} @ {info.server}")
    return mt5


def fetch_rates(mt5, sym: str, tf_str: str) -> pd.DataFrame:
    tf = {"30m": mt5.TIMEFRAME_M30, "1h": mt5.TIMEFRAME_H1}[tf_str]
    bpd = 48 if tf_str == "30m" else 24
    n   = DAYS * bpd + WARMUP + 300
    rates = mt5.copy_rates_from_pos(sym, tf, 0, n)
    if rates is None or len(rates) == 0:
        raise RuntimeError(f"No data for {sym} {tf_str}")
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.rename(columns={"tick_volume": "volume"})
    df = df[["time","open","high","low","close","volume"]].set_index("time")
    cutoff = df.index[-1] - pd.Timedelta(days=DAYS + WARMUP // bpd + 10)
    return df[df.index >= cutoff]


# ── Signal pre-computation ─────────────────────────────────────────────────

@dataclass
class BarCandidate:
    """A bar that passed session + data-sufficiency filters — not yet threshold-filtered."""
    bar_idx:   int
    ts:        object
    score_30m: float
    score_1h:  float
    adx:       int
    conf_at_1_2: int       # confidence at abs(score)=1.2 threshold baseline
    entry:     float
    atr:       float
    f_highs:   np.ndarray  # future MAX_BARS high values
    f_lows:    np.ndarray  # future MAX_BARS low values
    f_closes:  np.ndarray  # future MAX_BARS close values (for timeout)


SESSION_HOURS = config.SESSION_HOURS["overlap"]


def precompute(symbol: str, df30m: pd.DataFrame, df1h: pd.DataFrame) -> list:
    """Walk all bars and pre-compute scores/indicators. No threshold applied yet."""
    db   = config.db_path(symbol)
    cfg  = config.get_pair_config(symbol)
    cands = []
    n30  = len(df30m)
    h30  = df30m["high"].values
    l30  = df30m["low"].values
    c30  = df30m["close"].values

    print(f"  [{symbol}] Pre-computing {n30 - WARMUP:,} bars …")
    t0 = time.time()

    for i in range(WARMUP, n30):
        ts = df30m.index[i]
        if ts.hour not in SESSION_HOURS:
            continue

        win30 = df30m.iloc[i - WARMUP : i + 1]
        win1h = df1h[df1h.index <= ts].tail(WARMUP)
        if len(win1h) < 50:
            continue

        try:
            s30, v30 = score_timeframe(db, win30)
            s1h, _   = score_timeframe(db, win1h)
        except Exception:
            continue

        # Both TFs must agree in direction
        if s30 == 0 or s1h == 0:
            continue
        if (s30 > 0) != (s1h > 0):
            continue

        try:
            adx  = trend_strength(win30)
            atr  = compute_atr(win30)
            conf = confidence_pct(db, s30, cfg, active_votes=v30)
            entry = float(win30["close"].iloc[-1])
        except Exception:
            continue

        # Collect future bars for TP/SL scanning
        end = min(i + 1 + MAX_BARS, n30)
        f_h = h30[i + 1 : end]
        f_l = l30[i + 1 : end]
        f_c = c30[i + 1 : end]
        if len(f_h) == 0:
            continue

        cands.append(BarCandidate(
            bar_idx      = i,
            ts           = ts,
            score_30m    = s30,
            score_1h     = s1h,
            adx          = adx,
            conf_at_1_2  = conf,
            entry        = entry,
            atr          = atr,
            f_highs      = f_h,
            f_lows       = f_l,
            f_closes     = f_c,
        ))

    elapsed = time.time() - t0
    print(f"  [{symbol}] {len(cands):,} candidates found  ({elapsed:.1f}s)")
    return cands


# ── Grid simulator ─────────────────────────────────────────────────────────

def lot_from_risk(balance: float, sl_dist: float) -> float:
    sl_pips = sl_dist / config.PIP_SIZE
    if sl_pips <= 0:
        return config.MIN_LOT
    lot = (balance * RISK_PCT) / (sl_pips * config.PIP_VALUE_PER_LOT)
    lot = max(config.MIN_LOT, round(lot / config.LOT_STEP) * config.LOT_STEP)
    return min(config.MAX_LOT, lot)


def scan_outcome(c: BarCandidate, direction: str, tp: float, sl: float):
    """Scan pre-stored future bars for TP/SL hit. Returns (result, close_price, bars)."""
    for k in range(len(c.f_highs)):
        h, l = c.f_highs[k], c.f_lows[k]
        if direction == "BUY":
            if l <= sl:  return "LOSS",    sl, k + 1
            if h >= tp:  return "WIN",     tp, k + 1
        else:
            if h >= sl:  return "LOSS",    sl, k + 1
            if l <= tp:  return "WIN",     tp, k + 1
    close = float(c.f_closes[-1]) if len(c.f_closes) > 0 else c.entry
    return "TIMEOUT", close, len(c.f_highs)


def simulate_combo(cands: list, sl_mult: float, tp_mult: float,
                   threshold: float) -> dict:
    """Simulate one (SL, TP, threshold) combo on pre-computed candidates."""
    balance     = START_BAL
    max_bal     = balance
    max_dd      = 0.0
    equity      = [balance]
    wins = losses = timeouts = 0
    gp = gl = 0.0
    next_allowed_idx = 0      # bar_idx after which we can enter

    for c in cands:
        # Cooldown gate (bar_idx based)
        if c.bar_idx < next_allowed_idx:
            continue
        # Threshold gate
        if abs(c.score_30m) < threshold:
            continue
        # ADX gate
        if c.adx < MIN_ADX:
            continue
        # Confidence gate (pre-computed at current score — close enough)
        if c.conf_at_1_2 < MIN_CONF:
            continue

        direction = "BUY" if c.score_30m > 0 else "SELL"
        sl_dist   = sl_mult * c.atr
        tp_dist   = tp_mult * c.atr

        if direction == "BUY":
            tp = c.entry + tp_dist
            sl = c.entry - sl_dist
        else:
            tp = c.entry - tp_dist
            sl = c.entry + sl_dist

        lot = lot_from_risk(balance, sl_dist)
        result, close_p, n_bars = scan_outcome(c, direction, tp, sl)

        pnl_pips = (close_p - c.entry) / config.PIP_SIZE if direction == "BUY" \
                   else (c.entry - close_p) / config.PIP_SIZE
        pnl_usd  = pnl_pips * lot * config.PIP_VALUE_PER_LOT

        if result == "WIN":
            wins    += 1
            gp      += pnl_usd
        elif result == "LOSS":
            losses  += 1
            gl      += abs(pnl_usd)
        else:
            timeouts += 1
            if pnl_usd >= 0: gp += pnl_usd
            else:             gl += abs(pnl_usd)

        balance += pnl_usd
        equity.append(round(balance, 4))
        max_bal = max(max_bal, balance)
        dd = (max_bal - balance) / max_bal * 100 if max_bal > 0 else 0
        max_dd = max(max_dd, dd)

        # Advance past trade + cooldown
        next_allowed_idx = c.bar_idx + n_bars + COOLDOWN + 1

    n_trades = wins + losses + timeouts
    pf       = round(gp / gl, 3)      if gl > 0      else 999.0
    wr       = round(wins / n_trades * 100, 1) if n_trades else 0.0
    net_pnl  = round(balance - START_BAL, 4)
    roi      = round(net_pnl / START_BAL * 100, 2)
    rr_ratio = round(tp_mult / sl_mult, 2)
    ret_dd   = round(roi / max_dd, 3)  if max_dd > 0 else 999.0

    return {
        "sl":        sl_mult,
        "tp":        tp_mult,
        "rr":        rr_ratio,
        "thr":       threshold,
        "n":         n_trades,
        "wins":      wins,
        "losses":    losses,
        "timeouts":  timeouts,
        "wr":        wr,
        "pf":        pf,
        "net_pnl":   net_pnl,
        "roi":       roi,
        "max_dd":    round(max_dd, 2),
        "ret_dd":    ret_dd,
        "final_bal": round(balance, 4),
        "equity":    equity,
    }


# ── Per-pair runner ────────────────────────────────────────────────────────

def run_pair(symbol: str, df30m: pd.DataFrame, df1h: pd.DataFrame) -> list:
    """Pre-compute then grid-search all combos. Returns list of result dicts."""
    cands = precompute(symbol, df30m, df1h)

    # Build valid combos: TP > SL (R:R > 1)
    combos = [
        (sl, tp, thr)
        for sl  in SL_MULTS
        for tp  in TP_MULTS
        for thr in THRESHOLDS
        if tp > sl
    ]
    print(f"  [{symbol}] Running {len(combos)} combos …")

    results = []
    for sl, tp, thr in combos:
        r = simulate_combo(cands, sl, tp, thr)
        r["symbol"] = symbol
        results.append(r)

    results.sort(key=lambda x: x["ret_dd"], reverse=True)
    return results


# ── Main ───────────────────────────────────────────────────────────────────

def fmt_row(r):
    rr_str  = f"SL{r['sl']:.1f}/TP{r['tp']:.1f}  R:R {r['rr']:.2f}  thr={r['thr']:.1f}"
    stat    = (f"n={r['n']:>3}  WR={r['wr']:>5.1f}%  PF={r['pf']:.3f}  "
               f"ROI={r['roi']:>+6.2f}%  DD={r['max_dd']:>5.2f}%  "
               f"Ret/DD={r['ret_dd']:>5.3f}  bal=${r['final_bal']:.2f}")
    return f"  {rr_str:<35}  {stat}"


def main():
    sep = "=" * 80
    print(sep)
    print(f"360-Day Optimizer  |  {DAYS} days  |  ${START_BAL:.0f} start  |  {RISK_PCT*100:.0f}% risk/trade")
    print(f"SL: {SL_MULTS}  TP: {TP_MULTS}  Threshold: {THRESHOLDS}  (TP > SL only)")
    print(f"Fixed: ADX>={MIN_ADX}  Conf>={MIN_CONF}  Cooldown={COOLDOWN}x30m  MaxHold={MAX_BARS}x30m")
    print(sep)

    mt5 = init_mt5()

    # Fetch data for both pairs
    pair_data = {}
    for symbol in PAIRS:
        sym = MT5_SYMS[symbol]
        print(f"\nFetching {symbol} …")
        df30 = fetch_rates(mt5, sym, "30m")
        df1h = fetch_rates(mt5, sym, "1h")
        print(f"  30m {len(df30):,} bars  |  1h {len(df1h):,} bars  "
              f"|  {df30.index[0].date()} → {df30.index[-1].date()}")
        pair_data[symbol] = (df30, df1h)

    mt5.shutdown()

    # Run both pairs in parallel (pre-compute + grid search)
    all_results = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = {
            pool.submit(run_pair, sym, *pair_data[sym]): sym
            for sym in PAIRS
        }
        for fut in as_completed(futs):
            sym = futs[fut]
            all_results[sym] = fut.result()

    # ── Per-pair top results ───────────────────────────────────────────────
    for symbol in PAIRS:
        results = all_results[symbol]
        print(f"\n{'─'*80}")
        print(f"{symbol}  — top 10 combos by Return/MaxDD")
        print('─'*80)
        print(f"  {'SL/TP  R:R  Threshold':<35}  {'Trades  WR%  PF  ROI  MaxDD  Ret/DD  FinalBal'}")
        print('─'*80)
        for r in results[:10]:
            flag = "  <<" if r["ret_dd"] >= 1.5 and r["pf"] >= 1.3 else ""
            print(fmt_row(r) + flag)

    # ── Combined ranking (sum ROI for both pairs, same combo) ─────────────
    print(f"\n{'='*80}")
    print("COMBINED RANKING  (EUR + GBP, same combo applied to both pairs)")
    print("Sorted by: avg Return/MaxDD × avg PF  (higher = better)")
    print('='*80)
    print(f"  {'SL/TP  R:R  Thr':<32}  "
          f"{'EUR ROI':>8}  {'GBP ROI':>8}  {'AvgROI':>7}  "
          f"{'AvgDD':>6}  {'AvgPF':>6}  {'Ret/DD':>6}  {'Score':>7}")
    print('─'*80)

    eur_map = {(r["sl"], r["tp"], r["thr"]): r for r in all_results["EUR/USD"]}
    gbp_map = {(r["sl"], r["tp"], r["thr"]): r for r in all_results["GBP/USD"]}
    combos  = list(eur_map.keys())

    combined = []
    for key in combos:
        e = eur_map.get(key)
        g = gbp_map.get(key)
        if not e or not g:
            continue
        avg_roi = (e["roi"] + g["roi"]) / 2
        avg_dd  = (e["max_dd"] + g["max_dd"]) / 2
        avg_pf  = (e["pf"]  + g["pf"])  / 2
        ret_dd  = round(avg_roi / avg_dd, 3) if avg_dd > 0 else 0
        score   = round(ret_dd * avg_pf, 4)
        combined.append({
            "key":     key,
            "sl":      key[0], "tp": key[1], "thr": key[2],
            "eur_roi": e["roi"], "gbp_roi": g["roi"],
            "eur_pf":  e["pf"],  "gbp_pf":  g["pf"],
            "avg_roi": round(avg_roi, 2),
            "avg_dd":  round(avg_dd,  2),
            "avg_pf":  round(avg_pf,  3),
            "ret_dd":  ret_dd,
            "score":   score,
            "eur_n":   e["n"],  "gbp_n":  g["n"],
            "eur_wr":  e["wr"], "gbp_wr": g["wr"],
        })

    combined.sort(key=lambda x: x["score"], reverse=True)

    for i, c in enumerate(combined[:20]):
        sl, tp, thr = c["sl"], c["tp"], c["thr"]
        rr   = round(tp / sl, 2)
        flag = " **BEST**" if i == 0 else (" <<" if c["score"] >= 1.0 and c["avg_pf"] >= 1.2 else "")
        print(f"  SL{sl:.1f}/TP{tp:.1f}  R:R{rr:.2f}  thr={thr:.1f}   "
              f"{c['eur_roi']:>+7.2f}%  {c['gbp_roi']:>+7.2f}%  "
              f"{c['avg_roi']:>+6.2f}%  "
              f"{c['avg_dd']:>5.2f}%  {c['avg_pf']:>6.3f}  "
              f"{c['ret_dd']:>6.3f}  {c['score']:>7.4f}"
              + flag)

    # Winner
    best = combined[0]
    print(f"\n{'='*80}")
    print(f"RECOMMENDED SETTINGS:")
    print(f"  SL_ATR_MULT   = {best['sl']}")
    print(f"  TP1_ATR_MULT  = {best['tp']}")
    print(f"  R:R           = {round(best['tp']/best['sl'],2)} : 1")
    print(f"  SIGNAL_THRESHOLD = {best['thr']}")
    print(f"  EUR/USD: ROI {best['eur_roi']:+.2f}%  PF {best['eur_pf']:.3f}  WR {all_results['EUR/USD'][[r['thr']==best['thr'] and r['sl']==best['sl'] and r['tp']==best['tp'] for r in all_results['EUR/USD']].index(True)]['wr']:.1f}%")
    print(f"  GBP/USD: ROI {best['gbp_roi']:+.2f}%  PF {best['gbp_pf']:.3f}  WR {all_results['GBP/USD'][[r['thr']==best['thr'] and r['sl']==best['sl'] and r['tp']==best['tp'] for r in all_results['GBP/USD']].index(True)]['wr']:.1f}%")
    print(f"  Avg ROI  {best['avg_roi']:+.2f}%  Avg PF {best['avg_pf']:.3f}  Ret/DD {best['ret_dd']:.3f}")
    print(f"{'='*80}")

    # Save JSON
    save_data = {
        "run_at":     datetime.now(timezone.utc).isoformat(),
        "days":       DAYS,
        "start_bal":  START_BAL,
        "grid": {
            "sl_mults":   SL_MULTS,
            "tp_mults":   TP_MULTS,
            "thresholds": THRESHOLDS,
        },
        "combined_ranking": [{k: v for k, v in c.items() if k != "key"} for c in combined],
        "per_pair": {
            sym: [{k: v for k, v in r.items() if k != "equity"} for r in rs]
            for sym, rs in all_results.items()
        },
        "best": {k: v for k, v in best.items() if k != "key"},
        "equity_curves": {
            sym: {
                f"SL{r['sl']:.1f}_TP{r['tp']:.1f}_thr{r['thr']:.1f}": r["equity"]
                for r in rs if r["ret_dd"] == max(x["ret_dd"] for x in rs[:10])
            }
            for sym, rs in all_results.items()
        },
    }
    with open("optimizer_1000d_results.json", "w") as f:
        json.dump(save_data, f, indent=2, default=str)
    print(f"\nFull results saved → optimizer_1000d_results.json")


if __name__ == "__main__":
    main()
