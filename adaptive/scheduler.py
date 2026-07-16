"""Adaptive optimization scheduler — orchestrates the learning engine.

Called from main.py at two points in the trade lifecycle:

  on_trade_closed(...)   — after every WIN / LOSS:
      • update trade_context outcome columns
      • add the result to the in-memory RollingStats
      • feed the result to the OverfittingGuard shadow tracker
      • trigger batch weight rebalance every WEIGHT_BATCH_N trades
      • trigger parameter optimization every PARAM_BATCH_N trades

  run_cycle(...)         — once per 5-minute bot cycle:
      • check for shadow-period rollbacks
      • (future: regime re-classification)

Every adaptive action is wrapped in a broad try/except so that a bug in
the adaptive engine can never take down the main trading bot.
"""
from __future__ import annotations

import traceback
from datetime import datetime, timezone

import config
from adaptive import context as ctx_mod
from adaptive import weight_learner
from adaptive.guard import OverfittingGuard, get_guard
from adaptive.param_learner import ParamLearner
from adaptive.param_store import AdaptiveParamStore, get_store
from adaptive.regime import detect_from_signal
from adaptive.stats import RollingStats

# ── Tunable schedule constants ────────────────────────────────────────────────

# Run batch weight rebalancing every N closed trades
WEIGHT_BATCH_N = int(getattr(config, "ADAPTIVE_WEIGHT_BATCH", 50))

# Run full parameter optimization every N closed trades
PARAM_BATCH_N = int(getattr(config, "ADAPTIVE_OPTIMIZE_EVERY", 50))

# Minimum trades before any parameter learning is attempted
MIN_SAMPLE = int(getattr(config, "ADAPTIVE_MIN_SAMPLE", 30))


class AdaptiveScheduler:
    """Per-symbol scheduler that wires together all adaptive components."""

    def __init__(self, symbol: str) -> None:
        self.symbol    = symbol
        self._stats    = RollingStats(
            window=int(getattr(config, "ADAPTIVE_WINDOW", 100)),
            alpha=float(getattr(config, "ADAPTIVE_EWMA_ALPHA", 0.94)),
        )
        self._guard    = get_guard(symbol)
        self._learner  = ParamLearner()
        self._n_closed = 0   # total closed trades since process start

    # ── Primary hooks ─────────────────────────────────────────────────────────

    def on_trade_closed(
        self,
        db: str,
        signal_id: int,
        direction: str,
        won: bool,
        close_price: float,
        entry: float,
        sl: float,
        pnl: float,
        hold_hours: float = 0.0,
    ) -> None:
        """Called by main.py immediately after a trade closes.  Never raises."""
        if not getattr(config, "ADAPTIVE_ENABLED", True):
            return
        try:
            self._on_trade_closed_impl(
                db, signal_id, direction, won,
                close_price, entry, sl, pnl, hold_hours,
            )
        except Exception as exc:
            print(f"[adaptive/{self.symbol}] on_trade_closed error: {exc}")
            traceback.print_exc()

    def run_cycle(self, db: str, pair_cfg: dict) -> None:
        """Called once per bot cycle.  Checks for pending rollbacks.  Never raises."""
        if not getattr(config, "ADAPTIVE_ENABLED", True):
            return
        try:
            store = get_store(db)
            rolled = self._guard.check_rollbacks(store)
            if rolled:
                print(f"[adaptive/{self.symbol}] rolled back: {rolled}")
        except Exception as exc:
            print(f"[adaptive/{self.symbol}] run_cycle error: {exc}")

    # ── Implementation ────────────────────────────────────────────────────────

    def _on_trade_closed_impl(
        self,
        db: str,
        signal_id: int,
        direction: str,
        won: bool,
        close_price: float,
        entry: float,
        sl: float,
        pnl: float,
        hold_hours: float,
    ) -> None:
        status = "WIN" if won else "LOSS"

        # 1. Record outcome in trade_context
        ctx_mod.update_outcome(
            db, signal_id, status, close_price, entry, sl, pnl, hold_hours
        )

        # 2. Compute r_multiple for in-memory stats
        risk = abs(entry - sl) if sl and abs(entry - sl) > 1e-9 else 1.0
        r_mult = (abs(close_price - entry) / risk) * (1 if won else -1)

        # 3. Update rolling stats (regime from recent context row)
        regime = self._last_regime(db, signal_id)
        self._stats.add(
            r_multiple=r_mult,
            won=won,
            regime=regime,
            session=_current_session(),
            pnl=pnl,
        )

        # 4. Update guard shadow periods
        self._guard.record_trade(r_mult, won)

        self._n_closed += 1

        # 5. Batch weight rebalance (every WEIGHT_BATCH_N trades)
        if self._n_closed % WEIGHT_BATCH_N == 0 and self._stats.is_sufficient(MIN_SAMPLE):
            changes = weight_learner.batch_rebalance(db, self._stats)
            if changes:
                print(f"[adaptive/{self.symbol}] weight rebalance: {list(changes.keys())}")

        # 6. Parameter optimization (every PARAM_BATCH_N trades)
        if self._n_closed % PARAM_BATCH_N == 0 and self._stats.is_sufficient(MIN_SAMPLE):
            self._run_param_optimization(db)

    def _run_param_optimization(self, db: str) -> None:
        store = get_store(db)
        contexts = ctx_mod.get_recent_contexts(db, n=200)
        if len(contexts) < MIN_SAMPLE:
            return

        baseline_r = self._stats.avg_r()
        changes = self._learner.optimize(
            db=db,
            param_store=store,
            contexts=contexts,
            guard=self._guard,
            pair=self.symbol,
            rolling_stats=self._stats,
        )

        # Register shadow periods for all accepted changes
        for param_name, info in changes.items():
            self._guard.start_shadow(
                param_name,
                old_value=info["old"],
                new_value=info["new"],
                baseline_avg_r=baseline_r,
            )

        if changes:
            print(
                f"[adaptive/{self.symbol}] optimized {len(changes)} params "
                f"({', '.join(changes)})"
            )
        else:
            print(f"[adaptive/{self.symbol}] optimization pass: no improvements found")

    def _last_regime(self, db: str, signal_id: int) -> str:
        """Fetch the regime recorded at signal entry, or return 'unknown'."""
        try:
            from adaptive.param_store import _db_lock
            import sqlite3
            lock = _db_lock(db)
            with lock:
                con = sqlite3.connect(db)
                try:
                    cur = con.cursor()
                    cur.execute(
                        "SELECT regime FROM trade_context WHERE signal_id=? LIMIT 1",
                        (signal_id,),
                    )
                    row = cur.fetchone()
                    return row[0] if row and row[0] else "unknown"
                finally:
                    con.close()
        except Exception:
            return "unknown"

    def stats_summary(self) -> dict:
        return self._stats.to_dict()


# ── Module-level registry ─────────────────────────────────────────────────────

_SCHEDULERS: dict[str, AdaptiveScheduler] = {}


def get_scheduler(symbol: str) -> AdaptiveScheduler:
    if symbol not in _SCHEDULERS:
        _SCHEDULERS[symbol] = AdaptiveScheduler(symbol)
    return _SCHEDULERS[symbol]


# ── Convenience module-level functions (for main.py) ─────────────────────────

def on_trade_closed(
    symbol: str,
    db: str,
    signal_id: int,
    direction: str,
    won: bool,
    close_price: float,
    entry: float,
    sl: float = 0.0,
    pnl: float = 0.0,
    hold_hours: float = 0.0,
) -> None:
    get_scheduler(symbol).on_trade_closed(
        db, signal_id, direction, won, close_price, entry, sl, pnl, hold_hours
    )


def run_cycle(symbol: str, db: str, pair_cfg: dict) -> None:
    get_scheduler(symbol).run_cycle(db, pair_cfg)


def record_signal_context(
    db: str,
    signal_id: int,
    sig: dict,
    symbol: str,
) -> None:
    """Called from main.py after storage.log_signal() returns the signal ID."""
    if not getattr(config, "ADAPTIVE_ENABLED", True):
        return
    try:
        regime = detect_from_signal(sig)
        ctx_mod.capture_context(db, signal_id, sig, pair=symbol, regime=regime)
    except Exception as exc:
        print(f"[adaptive/{symbol}] record_signal_context error: {exc}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _current_session() -> str:
    h = datetime.now(timezone.utc).hour
    if 12 <= h <= 16:
        return "overlap"
    if 7 <= h < 12:
        return "london"
    if 17 <= h <= 20:
        return "ny"
    return "asian"
