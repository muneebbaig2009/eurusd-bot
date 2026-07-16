"""Adaptive parameter optimizer — perturbation search on rolling context data.

For each optimizable parameter the optimizer:
  1. Loads the recent trade_context rows that have closed outcomes.
  2. Splits them 70 / 30 into a train set and a hold-out validation set.
  3. Tries candidate values (current ± step, current ± 2×step).
  4. Evaluates each candidate on the train set by simulating the filter / rule.
  5. Validates the best candidate on the hold-out set.
  6. Accepts the change only when:
       • validation profit-factor improvement > MIN_IMPROVEMENT
       • sample size on both sets exceeds minimum
       • change magnitude is within the overfitting guard's limit
  7. Logs the decision (accepted or rejected) to optimization_log.

Parameters that can be optimized from context data alone
---------------------------------------------------------
Threshold parameters (we know the metric value at entry):
  SIGNAL_THRESHOLD   — filter by score ≥ threshold
  MIN_ADX            — filter by adx  ≥ threshold
  MIN_CONFIDENCE     — filter by conf ≥ threshold

R-multiple / risk-reward parameters (we know r_multiple at close):
  SL_ATR_MULT        — inferred from win/loss R distribution
  TP1_ATR_MULT       — inferred from win R distribution

Indicator oscillator thresholds (we know rsi, stoch at entry):
  RSI_OS, RSI_OB     — filter trades where RSI voted
  STOCH_OS, STOCH_OB — filter trades where stoch voted

Timing parameters (we know session at entry):
  SIGNAL_COOLDOWN_BARS — compare performance when fewer vs more are filtered
"""
from __future__ import annotations

import math
from typing import Optional

from adaptive.param_store import AdaptiveParamStore
from adaptive.stats import RollingStats


# Minimum closed-trade count before any optimization runs
MIN_SAMPLE = 30
# Minimum improvement on the validation set (5 %)
MIN_IMPROVEMENT = 0.05
# Train / validation split fraction
TRAIN_FRAC = 0.70


class ParamLearner:
    """Run one optimization pass across all learnable parameters.

    Usage
    -----
    learner = ParamLearner()
    changes = learner.optimize(db, param_store, contexts, guard, pair)
    """

    # Parameters we can optimize, grouped by the method used.
    # "threshold": filter trades whose metric >= candidate; compute PF on remainder
    _THRESHOLD_PARAMS = {
        "SIGNAL_THRESHOLD": "score",
        "MIN_ADX":          "adx",
        "MIN_CONFIDENCE":   "confidence",
    }

    # Oscillator-level params — filter trades where that indicator was active
    _OSCILLATOR_PARAMS = {
        "RSI_OS":   ("rsi",     "low"),   # lower = catches more oversold signals
        "RSI_OB":   ("rsi",     "high"),  # higher = more conservative overbought
        "STOCH_OS": ("stoch_k", "low"),
        "STOCH_OB": ("stoch_k", "high"),
    }

    def optimize(
        self,
        db: str,
        param_store: AdaptiveParamStore,
        contexts: list[dict],
        guard,   # OverfittingGuard instance
        pair: str,
        regime: str = "all",
        rolling_stats: Optional[RollingStats] = None,
    ) -> dict[str, dict]:
        """Run one full optimization pass.  Returns accepted changes."""
        closed = [c for c in contexts if c.get("won") is not None]
        if len(closed) < MIN_SAMPLE:
            return {}

        # Sort chronologically (oldest first) to get a realistic train/val split
        closed_sorted = sorted(closed, key=lambda c: c.get("created_at", ""))
        split = int(len(closed_sorted) * TRAIN_FRAC)
        train = closed_sorted[:split]
        val   = closed_sorted[split:]

        if len(train) < 15 or len(val) < 8:
            return {}

        all_changes: dict[str, dict] = {}
        perf_before = _metrics(closed_sorted)

        # ── 1. Threshold parameters ───────────────────────────────────────────
        for param_name, ctx_field in self._THRESHOLD_PARAMS.items():
            change = self._optimize_threshold(
                param_name, ctx_field, train, val, param_store, guard, regime
            )
            if change:
                all_changes[param_name] = change

        # ── 2. Oscillator thresholds ──────────────────────────────────────────
        for param_name, (ctx_field, direction) in self._OSCILLATOR_PARAMS.items():
            change = self._optimize_oscillator(
                param_name, ctx_field, direction, train, val, param_store, guard, regime
            )
            if change:
                all_changes[param_name] = change

        # ── 3. ATR multipliers (from R-multiple distribution) ─────────────────
        for param_name in ("SL_ATR_MULT", "TP1_ATR_MULT"):
            change = self._optimize_atr_mult(
                param_name, closed_sorted, param_store, guard, regime
            )
            if change:
                all_changes[param_name] = change

        accepted = bool(all_changes)
        param_store.log_optimization(
            pair=pair,
            regime=regime,
            n_trades=len(closed),
            changes=all_changes,
            perf_before=perf_before,
            accepted=accepted,
            reason="batch_param_optimization" if accepted else "no_improvement_found",
        )
        return all_changes

    # ── Threshold optimization ────────────────────────────────────────────────

    def _optimize_threshold(
        self,
        param_name: str,
        ctx_field: str,
        train: list[dict],
        val: list[dict],
        store: AdaptiveParamStore,
        guard,
        regime: str,
    ) -> Optional[dict]:
        current = store.get(param_name, regime)
        if current is None:
            return None
        defn = store.DEFAULTS.get(param_name, {})
        step = defn.get("step", 0.1)

        best_val_pf = _pf_threshold(val, ctx_field, current)
        best_candidate = current
        best_train_pf  = _pf_threshold(train, ctx_field, current)

        for mult in (-2, -1, 1, 2):
            candidate = current + mult * step
            candidate = max(defn.get("min", -1e9), min(defn.get("max", 1e9), candidate))
            if abs(candidate - current) < 1e-9:
                continue
            t_pf = _pf_threshold(train, ctx_field, candidate)
            if t_pf <= best_train_pf:
                continue
            v_pf = _pf_threshold(val, ctx_field, candidate)
            improvement = (v_pf - best_val_pf) / (best_val_pf + 1e-9)
            if improvement > MIN_IMPROVEMENT:
                if guard.check_change_safe(param_name, current, candidate, store):
                    best_candidate = candidate
                    best_val_pf    = v_pf

        if abs(best_candidate - current) < 1e-9:
            return None

        store.set(
            param_name, best_candidate,
            reason=f"threshold_opt: val_pf {best_val_pf:.3f} > baseline",
            perf_before={"val_pf": round(_pf_threshold(val, ctx_field, current), 4)},
            n_trades=len(train) + len(val),
            regime=regime,
            confirmed=0,  # shadow period required
        )
        return {"old": current, "new": best_candidate, "param": param_name}

    # ── Oscillator threshold optimization ────────────────────────────────────

    def _optimize_oscillator(
        self,
        param_name: str,
        ctx_field: str,
        direction: str,
        train: list[dict],
        val: list[dict],
        store: AdaptiveParamStore,
        guard,
        regime: str,
    ) -> Optional[dict]:
        current = store.get(param_name, regime)
        if current is None:
            return None
        defn = store.DEFAULTS.get(param_name, {})
        step = defn.get("step", 1.0)

        def _pf_osc(dataset, candidate):
            return _pf_oscillator(dataset, ctx_field, direction, candidate)

        best_v_pf = _pf_osc(val, current)
        best_t_pf = _pf_osc(train, current)
        best_candidate = current

        for mult in (-2, -1, 1, 2):
            candidate = current + mult * step
            candidate = max(defn.get("min", -1e9), min(defn.get("max", 1e9), candidate))
            if abs(candidate - current) < 1e-9:
                continue
            t_pf = _pf_osc(train, candidate)
            if t_pf <= best_t_pf:
                continue
            v_pf = _pf_osc(val, candidate)
            improvement = (v_pf - best_v_pf) / (best_v_pf + 1e-9)
            if improvement > MIN_IMPROVEMENT:
                if guard.check_change_safe(param_name, current, candidate, store):
                    best_candidate = candidate
                    best_v_pf      = v_pf

        if abs(best_candidate - current) < 1e-9:
            return None

        store.set(
            param_name, best_candidate,
            reason=f"oscillator_opt({ctx_field}): val_pf {best_v_pf:.3f}",
            perf_before={"val_pf": round(_pf_osc(val, current), 4)},
            n_trades=len(train) + len(val),
            regime=regime,
            confirmed=0,
        )
        return {"old": current, "new": best_candidate, "param": param_name}

    # ── ATR multiplier optimization ───────────────────────────────────────────

    def _optimize_atr_mult(
        self,
        param_name: str,
        closed: list[dict],
        store: AdaptiveParamStore,
        guard,
        regime: str,
    ) -> Optional[dict]:
        """Heuristic ATR multiplier tuning from R-multiple distributions.

        If the average winning R-multiple is consistently lower than the
        TP/SL ratio implies (e.g. TP1 = 0.5 × SL so expected R = +0.5 on wins),
        the TP may be set too tight.  The opposite suggests SL is too loose.
        """
        wins  = [c["r_multiple"] for c in closed if c.get("won") and c.get("r_multiple")]
        losses = [abs(c["r_multiple"]) for c in closed
                  if not c.get("won") and c.get("r_multiple")]
        if len(wins) < 10 or len(losses) < 10:
            return None

        avg_win  = sum(wins)  / len(wins)
        avg_loss = sum(losses) / len(losses)

        current = store.get(param_name, regime)
        if current is None:
            return None
        defn = store.DEFAULTS.get(param_name, {})
        step = defn.get("step", 0.1)

        candidate = current
        if param_name == "TP1_ATR_MULT":
            # If avg win < expected TP, price rarely reaches TP → pull TP closer
            # If avg win >> expected, TP is being hit comfortably → can push farther
            sl_mult = store.get("SL_ATR_MULT", regime) or 1.5
            expected_r = current / sl_mult
            if avg_win < expected_r * 0.6 and current > defn["min"]:
                candidate = current - step
            elif avg_win > expected_r * 1.4 and current < defn["max"]:
                candidate = current + step

        elif param_name == "SL_ATR_MULT":
            # If avg loss > SL should trigger → SL too tight (getting hit by noise)
            # If avg loss << SL → might be over-risking
            if avg_loss > current * 1.3 and current < defn["max"]:
                candidate = current + step
            elif avg_loss < current * 0.7 and current > defn["min"]:
                candidate = current - step

        if abs(candidate - current) < 1e-9:
            return None
        if not guard.check_change_safe(param_name, current, candidate, store):
            return None

        store.set(
            param_name, candidate,
            reason=f"atr_mult_heuristic: avg_win={avg_win:.2f} avg_loss={avg_loss:.2f}",
            n_trades=len(closed),
            regime=regime,
            confirmed=0,
        )
        return {"old": current, "new": candidate, "param": param_name}


# ── Metric helpers ─────────────────────────────────────────────────────────────

def _pf_threshold(dataset: list[dict], field: str, threshold: float) -> float:
    """Profit-factor on trades where dataset[field] >= threshold."""
    filtered = [c for c in dataset if (c.get(field) or 0) >= threshold
                and c.get("r_multiple") is not None]
    return _pf_from_list(filtered)


def _pf_oscillator(
    dataset: list[dict],
    field: str,
    direction: str,
    candidate: float,
) -> float:
    """Profit-factor on trades selected by oscillator threshold.

    direction='low'  → include trades where field < candidate (oversold filter)
    direction='high' → include trades where field > candidate (overbought filter)
    """
    if direction == "low":
        filtered = [c for c in dataset if (c.get(field) or 999) < candidate
                    and c.get("r_multiple") is not None]
    else:
        filtered = [c for c in dataset if (c.get(field) or 0) > candidate
                    and c.get("r_multiple") is not None]
    return _pf_from_list(filtered)


def _pf_from_list(trades: list[dict]) -> float:
    if not trades:
        return 0.0
    gross_w = sum(c["r_multiple"] for c in trades if c["r_multiple"] > 0)
    gross_l = sum(abs(c["r_multiple"]) for c in trades if c["r_multiple"] < 0)
    return gross_w / gross_l if gross_l > 0 else (1.0 if gross_w > 0 else 0.0)


def _metrics(trades: list[dict]) -> dict:
    if not trades:
        return {}
    n   = len(trades)
    wins = sum(1 for t in trades if t.get("won"))
    rs = [t["r_multiple"] for t in trades if t.get("r_multiple") is not None]
    gross_w = sum(r for r in rs if r > 0)
    gross_l = sum(abs(r) for r in rs if r < 0)
    return {
        "n":             n,
        "win_rate":      round(wins / n, 4),
        "profit_factor": round(gross_w / gross_l, 4) if gross_l else 0.0,
        "avg_r":         round(sum(rs) / len(rs), 4) if rs else 0.0,
    }
