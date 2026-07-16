"""Overfitting guard — shadow testing and automatic rollback.

When the param_learner accepts a parameter change it is stored with
confirmed=0 (shadow mode).  This guard then monitors the next
SHADOW_TRADES live trades.  If performance degrades by more than
ROLLBACK_THRESHOLD versus the pre-change baseline, the parameter is
automatically reverted to its previous value.

Checks performed before accepting a change
------------------------------------------
1. Magnitude check  — change must not exceed MAX_CHANGE_PCT of the param range
2. Shadow validation — next SHADOW_TRADES must not degrade avg_r by > threshold
3. Only one unconfirmed change per parameter at a time

All state is in-memory per process.  The confirmed flag in the DB is
the persistent record: confirmed=0 means the change is under probation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from adaptive.param_store import AdaptiveParamStore

# ── Constants ─────────────────────────────────────────────────────────────────

# Maximum fraction of the parameter range allowed per single update cycle.
# e.g. SIGNAL_THRESHOLD range = 0.5 → 3.0 (span=2.5).  MAX_CHANGE=20% → ≤ 0.5 per cycle.
MAX_CHANGE_PCT = 0.20

# Number of live trades to monitor after a parameter change before confirming
SHADOW_TRADES = 25

# Rollback if shadow avg_r < pre-change avg_r × (1 + ROLLBACK_THRESHOLD)
# At -0.15 this means: roll back if performance drops more than 15 %
ROLLBACK_THRESHOLD = -0.15


# ── Shadow state ──────────────────────────────────────────────────────────────

@dataclass
class _ShadowEntry:
    param_name:  str
    old_value:   float
    new_value:   float
    baseline_r:  float          # avg_r over the pre-change window
    trades_seen: int = 0
    r_sum:       float = 0.0
    r_count:     int  = 0

    @property
    def current_avg_r(self) -> Optional[float]:
        return self.r_sum / self.r_count if self.r_count > 0 else None


class OverfittingGuard:
    """Track shadow periods and issue rollback decisions."""

    def __init__(self) -> None:
        # param_name → _ShadowEntry
        self._shadows: dict[str, _ShadowEntry] = {}

    # ── Pre-change safety check ───────────────────────────────────────────────

    def check_change_safe(
        self,
        param_name: str,
        old_value: float,
        new_value: float,
        store: AdaptiveParamStore,
    ) -> bool:
        """Return True if the proposed change is within the allowed magnitude.

        Rejects changes that are already in shadow mode (prevents oscillation)
        or that exceed MAX_CHANGE_PCT of the parameter range.
        """
        # Block if already in shadow for this parameter
        if param_name in self._shadows:
            return False

        defn = store.DEFAULTS.get(param_name, {})
        lo, hi = defn.get("min", 0.0), defn.get("max", 1.0)
        param_range = hi - lo
        if param_range < 1e-9:
            return False

        change_pct = abs(new_value - old_value) / param_range
        return change_pct <= MAX_CHANGE_PCT

    # ── Shadow registration ───────────────────────────────────────────────────

    def start_shadow(
        self,
        param_name: str,
        old_value: float,
        new_value: float,
        baseline_avg_r: float,
    ) -> None:
        """Register a shadow period for a just-changed parameter."""
        self._shadows[param_name] = _ShadowEntry(
            param_name=param_name,
            old_value=old_value,
            new_value=new_value,
            baseline_r=baseline_avg_r,
        )

    # ── Trade recording ───────────────────────────────────────────────────────

    def record_trade(self, r_multiple: float, won: bool) -> None:
        """Update all active shadow periods with one more trade result."""
        for entry in list(self._shadows.values()):
            entry.trades_seen += 1
            entry.r_sum       += r_multiple
            entry.r_count     += 1

    # ── Rollback evaluation ───────────────────────────────────────────────────

    def check_rollbacks(self, store: AdaptiveParamStore) -> list[str]:
        """Check all shadow periods.  Roll back degraded params; confirm good ones.

        Returns the list of parameter names that were rolled back.
        """
        rolled_back: list[str] = []
        to_remove:   list[str] = []

        for name, entry in self._shadows.items():
            if entry.trades_seen < SHADOW_TRADES:
                continue  # shadow period not yet complete

            shadow_r = entry.current_avg_r
            if shadow_r is None:
                to_remove.append(name)
                continue

            # Determine whether shadow performance is acceptable
            baseline = entry.baseline_r
            if baseline == 0.0:
                # No baseline → check absolute avg_r
                degraded = shadow_r < ROLLBACK_THRESHOLD
            else:
                change_ratio = (shadow_r - baseline) / (abs(baseline) + 1e-9)
                degraded = change_ratio < ROLLBACK_THRESHOLD

            if degraded:
                rolled_back.append(name)
                store.rollback(name)
                print(
                    f"[guard] ROLLBACK {name}: shadow avg_r={shadow_r:.4f} "
                    f"baseline={baseline:.4f} → reverted"
                )
            else:
                # Confirm the change in the DB (set confirmed=1)
                current = store.get(name)
                if current is not None:
                    store.set(
                        name, current,
                        reason="shadow_validated — performance maintained",
                        confirmed=1,
                    )
                print(
                    f"[guard] CONFIRMED {name}={current:.4f} "
                    f"(shadow avg_r={shadow_r:.4f} vs baseline={baseline:.4f})"
                )

            to_remove.append(name)

        for name in to_remove:
            self._shadows.pop(name, None)

        return rolled_back

    # ── Status ────────────────────────────────────────────────────────────────

    def shadow_status(self) -> dict:
        """Return a summary of all active shadow periods."""
        return {
            name: {
                "old":         entry.old_value,
                "new":         entry.new_value,
                "baseline_r":  entry.baseline_r,
                "trades_seen": entry.trades_seen,
                "remaining":   max(0, SHADOW_TRADES - entry.trades_seen),
                "current_avg_r": entry.current_avg_r,
            }
            for name, entry in self._shadows.items()
        }

    def has_active_shadows(self) -> bool:
        return bool(self._shadows)


# ── Module-level registry (one guard per symbol) ──────────────────────────────

_GUARDS: dict[str, OverfittingGuard] = {}


def get_guard(symbol: str) -> OverfittingGuard:
    if symbol not in _GUARDS:
        _GUARDS[symbol] = OverfittingGuard()
    return _GUARDS[symbol]
