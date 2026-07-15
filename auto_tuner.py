"""
Strategy Auto-Tuner — scheduled backtest / forward-test comparison.

Called every bot cycle from run_local.py. Runs:
  Daily   (every 24 h)  — compare last 7 days of live trades vs baseline
  Weekly  (Sundays 20 UTC) — 30-day rolling backtest; apply if ≥5% gain
  Monthly (1st 20 UTC)    — 180-day deep backtest; apply if ≥5% gain

Results are written to docs/performance_log.json for the dashboard.
Strategy state (last-run timestamps + current baseline) lives in tuner_state.json.
"""
import json, os
from datetime import datetime, timedelta, timezone

STATE_FILE       = "tuner_state.json"
PERF_LOG         = os.path.join("docs", "performance_log.json")
IMPROVE_THRESHOLD = 0.05    # require ≥5% expectancy gain before swapping params
MAX_LOG_ENTRIES  = 90       # keep last 90 comparison snapshots


# ── State helpers ─────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)

def _load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def _hours_since(iso_str) -> float:
    if not iso_str:
        return float("inf")
    try:
        t = datetime.fromisoformat(iso_str)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (_now() - t).total_seconds() / 3600
    except Exception:
        return float("inf")


# ── Live performance from SQLite ─────────────────────────────────────────────

def _get_live_stats(db: str, days: int) -> dict:
    import sqlite3
    cutoff = (_now() - timedelta(days=days)).isoformat()
    con = sqlite3.connect(db)
    cur = con.cursor()
    cur.execute("""
        SELECT s.status, da.pnl
        FROM   signals s
        LEFT JOIN demo_account da ON da.signal_id = s.id
        WHERE  s.status IN ('WIN','LOSS')
          AND  s.closed_at >= ?
    """, (cutoff,))
    rows = cur.fetchall()
    con.close()
    if not rows:
        return {"n_trades": 0, "win_rate": 0.0, "expectancy": 0.0}
    wins = sum(1 for r in rows if r[0] == "WIN")
    pnls = [r[1] for r in rows if r[1] is not None]
    return {
        "n_trades":   len(rows),
        "win_rate":   round(wins / len(rows) * 100, 1),
        "expectancy": round(sum(pnls) / len(pnls), 2) if pnls else 0.0,
    }


# ── Backtest runner (no argparse, no connect/disconnect) ─────────────────────

def _run_backtest(days: int) -> dict | None:
    """Run the walk-forward backtest programmatically.
    MT5 must already be connected by the caller.
    Returns the best result dict, or None on failure."""
    print(f"[tuner] Running {days}-day backtest...")
    try:
        import mt5_backtest, tempfile, MetaTrader5 as mt5
        from datetime import datetime, timedelta, timezone
        import storage

        sym     = "EURUSD"
        end_dt  = datetime.now(timezone.utc)
        beg_dt  = end_dt - timedelta(days=days)
        day_beg = end_dt - timedelta(days=days + 730)

        r1h  = mt5.copy_rates_range(sym, mt5.TIMEFRAME_H1, beg_dt,  end_dt)
        r1d  = mt5.copy_rates_range(sym, mt5.TIMEFRAME_D1, day_beg, end_dt)
        r5m  = mt5.copy_rates_range(sym, mt5.TIMEFRAME_M5, beg_dt,  end_dt)

        if r1h is None or len(r1h) < 210:
            print(f"[tuner] Not enough 1h data ({len(r1h) if r1h else 0} bars).")
            return None

        df_1h   = mt5_backtest._to_df(r1h)
        df_1day = mt5_backtest._to_df(r1d)
        df_5min = mt5_backtest._to_df(r5m)

        tmp_db = tempfile.mktemp(suffix=".db")
        storage.init_db(tmp_db)

        cands = mt5_backtest.collect_candidates(df_1h, df_1day, tmp_db)
        if len(cands) < 10:
            print(f"[tuner] Too few candidates ({len(cands)}).")
            try: os.remove(tmp_db)
            except Exception: pass
            return None

        outcomes = mt5_backtest.precompute_outcomes(cands, df_5min)
        results  = mt5_backtest.grid_search(cands, outcomes)

        try: os.remove(tmp_db)
        except Exception: pass

        return results[0] if results else None

    except Exception as e:
        print(f"[tuner] Backtest error: {e}")
        return None


def _maybe_apply(best: dict, state: dict, label: str) -> bool:
    """Apply new params if they improve on the current baseline by ≥ threshold."""
    import mt5_backtest
    baseline = state.get("baseline", {})
    cur_exp  = baseline.get("expected_expectancy", 0.0)

    if not baseline or best["exp"] > max(0.01, cur_exp * (1 + IMPROVE_THRESHOLD)):
        mt5_backtest._apply(best)
        state["baseline"] = {
            "sl_mult":              best["sl"],
            "tp_mult":              best["tp"],
            "threshold":            best["thr"],
            "min_conf":             best["mc"],
            "expected_wr":          best["wr"],
            "expected_expectancy":  best["exp"],
            "n_trades":             best["n"],
            "source":               label,
            "timestamp":            _now().isoformat(),
        }
        print(f"[tuner] [{label}] Applied new params: "
              f"SL={best['sl']} TP={best['tp']} Conf={best['mc']} "
              f"WR {best['wr']}% E ${best['exp']:.2f}")
        return True
    else:
        print(f"[tuner] [{label}] Current params still best "
              f"(cur E=${cur_exp:.2f} vs new E=${best['exp']:.2f})")
        return False


# ── Performance log ───────────────────────────────────────────────────────────

def _append_log(entry: dict):
    log = []
    if os.path.exists(PERF_LOG):
        try:
            with open(PERF_LOG) as f:
                log = json.load(f).get("history", [])
        except Exception:
            pass
    log.insert(0, entry)
    log = log[:MAX_LOG_ENTRIES]
    os.makedirs("docs", exist_ok=True)
    with open(PERF_LOG, "w") as f:
        json.dump({
            "updated_at": _now().isoformat(),
            "history":    log,
        }, f, indent=2)


# ── Public entry point ────────────────────────────────────────────────────────

def init_baseline():
    """Seed the baseline from current config.py values on first run."""
    state = _load_state()
    if not state.get("baseline"):
        import config
        state["baseline"] = {
            "sl_mult":             config.SL_ATR_MULT,
            "tp_mult":             config.TP1_ATR_MULT,
            "threshold":           config.SIGNAL_THRESHOLD,
            "min_conf":            config.MIN_CONFIDENCE,
            "expected_wr":         62.2,
            "expected_expectancy": 0.41,
            "source":              "initial_backtest",
            "timestamp":           _now().isoformat(),
        }
        _save_state(state)
        print("[tuner] Baseline seeded from current config.py")


def check_and_run(db_pair: str):
    """Check schedule and run any due tasks. Called every bot cycle."""
    state   = _load_state()
    now     = _now()
    changed = False

    # ── Daily forward-test comparison (every 24 h) ─────────────────────────
    if _hours_since(state.get("last_daily")) >= 24:
        baseline = state.get("baseline", {})
        live     = _get_live_stats(db_pair, days=7)

        if live["n_trades"] >= 3 and baseline:
            exp_wr  = baseline.get("expected_wr",          50.0)
            exp_e   = baseline.get("expected_expectancy",  0.01)
            wr_dev  = abs(exp_wr - live["win_rate"]) / max(1, exp_wr) * 100
            e_dev   = (exp_e - live["expectancy"])    / max(0.01, abs(exp_e)) * 100

            action = "none"
            if e_dev > 30 or wr_dev > 20:
                action = "early_reopt"
                print(f"[tuner] Daily: performance degraded >threshold — "
                      f"WR {live['win_rate']}% vs {exp_wr}%  "
                      f"E ${live['expectancy']:.2f} vs ${exp_e:.2f}. "
                      f"Triggering 30-day reopt.")
                best = _run_backtest(days=30)
                if best:
                    _maybe_apply(best, state, "daily_reopt_30d")

            print(f"[tuner] Daily: {live['n_trades']} trades | "
                  f"WR {live['win_rate']}% (expected {exp_wr}%) | "
                  f"E ${live['expectancy']:.2f} (expected ${exp_e:.2f})")

            _append_log({
                "date":                 now.strftime("%Y-%m-%d %H:%M"),
                "type":                 "daily_check",
                "n_trades":             live["n_trades"],
                "actual_wr":            live["win_rate"],
                "actual_expectancy":    live["expectancy"],
                "expected_wr":          exp_wr,
                "expected_expectancy":  exp_e,
                "action":               action,
            })
        else:
            print(f"[tuner] Daily: only {live['n_trades']} trades in last 7 days — skipping compare.")

        state["last_daily"] = now.isoformat()
        changed = True

    # ── Weekly 30-day backtest (Sundays ≥ 20:00 UTC, at least 6 days apart) ─
    weekly_due = (now.weekday() == 6 and now.hour >= 20
                  and _hours_since(state.get("last_weekly")) >= 144)
    if weekly_due:
        best = _run_backtest(days=30)
        if best:
            applied = _maybe_apply(best, state, "weekly_30d")
            _append_log({
                "date":   now.strftime("%Y-%m-%d"),
                "type":   "weekly_30d",
                "result_wr":          best["wr"],
                "result_expectancy":  best["exp"],
                "applied":            applied,
                "params": {"sl": best["sl"], "tp": best["tp"],
                           "thr": best["thr"], "mc": best["mc"]},
            })
        state["last_weekly"] = now.isoformat()
        changed = True

    # ── Monthly 180-day backtest (1st of month ≥ 20:00 UTC, ≥ 28 days apart) ─
    monthly_due = (now.day == 1 and now.hour >= 20
                   and _hours_since(state.get("last_monthly")) >= 672)
    if monthly_due:
        best = _run_backtest(days=180)
        if best:
            applied = _maybe_apply(best, state, "monthly_180d")
            _append_log({
                "date":   now.strftime("%Y-%m-%d"),
                "type":   "monthly_180d",
                "result_wr":          best["wr"],
                "result_expectancy":  best["exp"],
                "applied":            applied,
                "params": {"sl": best["sl"], "tp": best["tp"],
                           "thr": best["thr"], "mc": best["mc"]},
            })
        state["last_monthly"] = now.isoformat()
        changed = True

    if changed:
        _save_state(state)
