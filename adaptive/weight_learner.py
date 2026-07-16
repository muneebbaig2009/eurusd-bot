"""Improved indicator weight learning — two-layer update strategy.

Layer 1 — Per-trade nudge (backward-compatible with learner.py)
  After every WIN or LOSS, nudge each contributing indicator's weight by ±LEARN_RATE.
  This preserves the existing behaviour.

Layer 2 — Batch rebalancing (every N closed trades)
  Analyse the rolling window of recent trade contexts.  For each indicator:
    • compute its accuracy (fraction of voted trades that were winners)
    • compute its profit-factor contribution
  Then rebalance weights so high-performing indicators gain influence relative
  to low-performing ones, subject to the existing MIN_WEIGHT / MAX_WEIGHT bounds.

  The batch update is conservative: adjustments are small, require a minimum
  sample size, and are proportional to how significantly an indicator outperforms
  or underperforms the median.
"""
from __future__ import annotations

import math
from statistics import median
from typing import Optional

import config
import storage
from adaptive.stats import RollingStats


# Minimum sample before batch rebalancing runs
_BATCH_MIN_TRADES = 30

# Maximum fractional weight change per batch cycle (prevents big jumps)
_MAX_BATCH_DELTA = 0.15


def update_per_trade(db: str, contributors: dict, direction: str, won: bool) -> None:
    """Per-trade weight nudge — identical logic to the original learner.py.

    Called after every trade closes.  Kept here so learner.py can delegate
    to this module while maintaining backward compatibility.

    contributors : {technique: vote}
    direction    : "BUY" or "SELL"
    won          : True if TP hit, False if SL hit
    """
    dir_sign = 1 if direction == "BUY" else -1
    for tech, vote in contributors.items():
        if vote == 0:
            continue
        agreed = (vote == dir_sign)
        w = storage.get_weight(db, tech)
        if won and agreed:
            w += config.LEARN_RATE
        elif won and not agreed:
            w -= config.LEARN_RATE
        elif (not won) and agreed:
            w -= config.LEARN_RATE
        else:  # not won, not agreed
            w += config.LEARN_RATE
        storage.set_weight(db, tech, w)


def batch_rebalance(
    db: str,
    rolling_stats: RollingStats,
    techniques: Optional[list[str]] = None,
    dry_run: bool = False,
) -> dict[str, dict]:
    """Rebalance indicator weights based on rolling performance statistics.

    Returns a dict describing the changes made (or that would be made in dry_run):
    {technique: {"old": float, "new": float, "delta": float}}
    """
    if not rolling_stats.is_sufficient(_BATCH_MIN_TRADES):
        return {}

    from techniques import TECHNIQUES
    techs = techniques or list(TECHNIQUES.keys())

    # ── Step 1: compute per-indicator profit-factor ───────────────────────────
    pf_scores: dict[str, float] = {}
    for name in techs:
        pf = rolling_stats.indicator_profit_factor(name)
        # Cap extreme values to avoid wild swings
        pf_scores[name] = min(pf, 3.0) if not math.isinf(pf) else 3.0

    if not pf_scores:
        return {}

    pf_values = list(pf_scores.values())
    med_pf = median(pf_values)
    if med_pf <= 0:
        return {}

    # ── Step 2: compute target adjustments ───────────────────────────────────
    # adjustment_ratio = (indicator_pf - median_pf) / median_pf
    # weight_delta     = current_weight * adjustment_ratio * damping
    # Positive ratio → increase weight; negative → decrease.
    damping = 0.20  # 20% of the ratio translates into weight change

    changes: dict[str, dict] = {}
    for name in techs:
        old_w = storage.get_weight(db, name)
        ratio = (pf_scores[name] - med_pf) / (med_pf + 1e-9)
        delta = old_w * ratio * damping
        # Clamp per-cycle change
        delta = max(-_MAX_BATCH_DELTA, min(_MAX_BATCH_DELTA, delta))
        new_w = old_w + delta
        # Respect global weight bounds
        new_w = max(config.MIN_WEIGHT, min(config.MAX_WEIGHT, new_w))

        if abs(new_w - old_w) < 0.005:
            continue  # skip negligible change

        changes[name] = {"old": round(old_w, 4), "new": round(new_w, 4),
                         "delta": round(delta, 4), "pf": round(pf_scores[name], 4)}
        if not dry_run:
            storage.set_weight(db, name, new_w)

    if changes and not dry_run:
        _log_batch(changes, rolling_stats.count())

    return changes


def _log_batch(changes: dict, n_trades: int) -> None:
    techs = ", ".join(f"{k}→{v['new']:.3f}" for k, v in changes.items())
    print(f"[weight_learner] batch({n_trades} trades): {techs}")
