"""Adaptive engine reporting — human-readable console output and JSON export.

generate(db, symbol) → dict  — full structured report
print_report(db, symbol)     — formatted console print
export_json(db, symbol, path) — write report to JSON file
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from adaptive.context import get_all_contexts
from adaptive.param_store import AdaptiveParamStore, get_store
from adaptive.stats import RollingStats


def generate(db: str, symbol: str) -> dict:
    """Build and return the full adaptive engine report as a dict."""
    store = get_store(db)

    # ── Load all closed contexts ──────────────────────────────────────────────
    contexts = get_all_contexts(db)
    closed   = [c for c in contexts if c.get("won") is not None]

    # ── Rebuild rolling stats from DB ─────────────────────────────────────────
    stats = RollingStats(window=len(closed) + 1, alpha=0.94)
    for c in closed:
        stats.add_from_context(c)

    # ── Weights from storage ──────────────────────────────────────────────────
    import storage
    weights = storage.all_weights(db)

    # ── Parameter snapshot ────────────────────────────────────────────────────
    param_snap = store.snapshot()

    # ── Changed params (vs defaults) ─────────────────────────────────────────
    changed_params = {k: v for k, v in param_snap.items() if v["changed"]}

    # ── Optimization history ──────────────────────────────────────────────────
    opt_log = store.get_optimization_log(limit=20)

    # ── Per-indicator stats ───────────────────────────────────────────────────
    from techniques import TECHNIQUES
    indicator_stats = {}
    for name in TECHNIQUES:
        indicator_stats[name] = {
            "weight":   round(weights.get(name, 1.0), 4),
            "accuracy": stats.indicator_accuracy(name),
            "pf":       round(stats.indicator_profit_factor(name), 4),
        }
    # Sort by PF descending
    indicator_stats = dict(
        sorted(indicator_stats.items(), key=lambda x: x[1]["pf"], reverse=True)
    )

    # ── Guard shadow status ───────────────────────────────────────────────────
    from adaptive.guard import get_guard
    shadow = get_guard(symbol).shadow_status()

    return {
        "generated_at":   datetime.now(timezone.utc).isoformat(),
        "symbol":         symbol,
        "db":             db,
        "total_contexts": len(closed),
        "overall_stats":  stats.to_dict(),
        "regime_stats":   stats.regime_stats(),
        "session_stats":  stats.session_stats(),
        "indicator_stats": indicator_stats,
        "weights":        weights,
        "param_snapshot": param_snap,
        "changed_params": changed_params,
        "shadow_periods": shadow,
        "optimization_log": opt_log,
    }


def print_report(db: str, symbol: str) -> None:
    """Print a formatted adaptive engine report to stdout."""
    r = generate(db, symbol)
    W = 62
    bar = "─" * W

    print(f"\n  {'='*W}")
    print(f"  ADAPTIVE ENGINE REPORT  {symbol}  —  {r['generated_at'][:19]} UTC")
    print(f"  {'='*W}")
    print(f"  Context records: {r['total_contexts']}")

    st = r["overall_stats"]
    print(f"\n  OVERALL PERFORMANCE (rolling {st['n']} trades)")
    print(f"  {bar}")
    print(f"  Win rate:      {st['win_rate']*100:.1f}%")
    print(f"  Profit factor: {st['profit_factor']:.4f}")
    print(f"  Avg R:         {st['avg_r']:+.4f}")
    print(f"  Expectancy:    {st['expectancy']:+.4f}")
    print(f"  Max drawdown:  {st['max_drawdown']:.4f} R")
    print(f"  Sharpe (ann):  {st['sharpe']:+.4f}")

    # ── Regime breakdown ──────────────────────────────────────────────────────
    if r["regime_stats"]:
        print(f"\n  REGIME BREAKDOWN")
        print(f"  {bar}")
        print(f"  {'Regime':<16} {'N':>5}  {'WR':>7}  {'Avg R':>8}")
        for regime, s in sorted(r["regime_stats"].items()):
            wr_s = f"{s['win_rate']*100:.1f}%"
            print(f"  {regime:<16} {s['n']:>5}  {wr_s:>7}  {s['avg_r']:>+8.4f}")

    # ── Session breakdown ─────────────────────────────────────────────────────
    if r["session_stats"]:
        print(f"\n  SESSION BREAKDOWN")
        print(f"  {bar}")
        print(f"  {'Session':<12} {'N':>5}  {'WR':>7}  {'Avg R':>8}")
        for sess, s in sorted(r["session_stats"].items()):
            wr_s = f"{s['win_rate']*100:.1f}%"
            print(f"  {sess:<12} {s['n']:>5}  {wr_s:>7}  {s['avg_r']:>+8.4f}")

    # ── Indicator weights + accuracy ──────────────────────────────────────────
    print(f"\n  INDICATOR WEIGHTS  (sorted by profit-factor contribution)")
    print(f"  {bar}")
    print(f"  {'Indicator':<14} {'Weight':>8}  {'Accuracy':>10}  {'PF':>8}")
    for name, stats_dict in r["indicator_stats"].items():
        acc_s = f"{stats_dict['accuracy']*100:.1f}%"
        print(
            f"  {name:<14} {stats_dict['weight']:>8.4f}  "
            f"{acc_s:>10}  {stats_dict['pf']:>8.4f}"
        )

    # ── Parameters changed from defaults ─────────────────────────────────────
    if r["changed_params"]:
        print(f"\n  ADAPTED PARAMETERS  ({len(r['changed_params'])} changed from defaults)")
        print(f"  {bar}")
        print(f"  {'Parameter':<25} {'Default':>9}  {'Current':>9}  {'Change':>8}")
        for name, info in r["changed_params"].items():
            delta = info["current"] - info["default"]
            sign = "+" if delta >= 0 else ""
            print(
                f"  {name:<25} {info['default']:>9.4f}  "
                f"{info['current']:>9.4f}  {sign}{delta:>7.4f}"
            )
    else:
        print(f"\n  All parameters at defaults — no adaptations yet.")

    # ── Shadow periods ────────────────────────────────────────────────────────
    if r["shadow_periods"]:
        print(f"\n  ACTIVE SHADOW PERIODS")
        print(f"  {bar}")
        for name, s in r["shadow_periods"].items():
            remaining = s["remaining"]
            cur_r = s.get("current_avg_r")
            cur_r_s = f"{cur_r:+.4f}" if cur_r is not None else "  n/a "
            print(
                f"  {name}: {s['old']:.4f} → {s['new']:.4f}  "
                f"({remaining} trades left)  avg_r={cur_r_s}  "
                f"baseline={s['baseline_r']:+.4f}"
            )

    # ── Optimization log ──────────────────────────────────────────────────────
    if r["optimization_log"]:
        print(f"\n  RECENT OPTIMIZATION RUNS (last {len(r['optimization_log'])})")
        print(f"  {bar}")
        for entry in r["optimization_log"][:5]:
            result = "ACCEPTED" if entry["accepted"] else "rejected"
            changes = ", ".join(
                f"{k}: {v['old']:.3f}→{v['new']:.3f}"
                for k, v in entry["changes"].items()
            ) if entry["changes"] else "none"
            print(f"  {entry['at'][:16]}  n={entry['n_trades']:>4}  "
                  f"{result:<9}  {changes}")

    print(f"  {'='*W}\n")


def export_json(db: str, symbol: str, path: Optional[str] = None) -> str:
    """Export full report as JSON.  Returns the path written."""
    r = generate(db, symbol)
    if path is None:
        path = os.path.join("docs", f"adaptive_{symbol.lower().replace('/', '')}.json")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(r, f, indent=2, default=str)
    return path
