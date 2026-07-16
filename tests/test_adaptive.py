"""Unit tests for the Adaptive Strategy Optimization Engine.

Run with:
    cd e:/eurusd-bot
    python -m pytest tests/test_adaptive.py -v

All tests use an in-memory or temp-file SQLite database — no MT5 connection
required.  Each test is self-contained and cleans up after itself.
"""
import json
import os
import sys
import tempfile
import math

import pytest

# Make sure the project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_db(tmp_path):
    """Temporary SQLite file for one test."""
    return str(tmp_path / "test_signals.db")


@pytest.fixture
def store(tmp_db):
    """AdaptiveParamStore backed by a temporary DB."""
    import storage
    storage.init_db(tmp_db)               # creates weights / signals tables
    from adaptive.param_store import AdaptiveParamStore
    return AdaptiveParamStore(tmp_db)


@pytest.fixture
def populated_db(tmp_db):
    """DB with the full adaptive schema initialised."""
    import storage
    storage.init_db(tmp_db)
    from adaptive.param_store import AdaptiveParamStore
    AdaptiveParamStore(tmp_db)            # triggers _ensure_schema
    return tmp_db


# ═══════════════════════════════════════════════════════════════════════════════
# AdaptiveParamStore
# ═══════════════════════════════════════════════════════════════════════════════

class TestParamStore:
    def test_defaults_returned_before_any_set(self, store):
        val = store.get("SIGNAL_THRESHOLD")
        assert val == pytest.approx(1.2)

    def test_set_and_get(self, store):
        store.set("SIGNAL_THRESHOLD", 1.5, reason="test")
        assert store.get("SIGNAL_THRESHOLD") == pytest.approx(1.5)

    def test_value_clamped_to_bounds(self, store):
        # SIGNAL_THRESHOLD max = 3.0
        store.set("SIGNAL_THRESHOLD", 99.0)
        assert store.get("SIGNAL_THRESHOLD") == pytest.approx(3.0)

        store.set("SIGNAL_THRESHOLD", -5.0)
        assert store.get("SIGNAL_THRESHOLD") == pytest.approx(0.5)

    def test_no_change_returns_false(self, store):
        store.set("SIGNAL_THRESHOLD", 1.2)   # same as default
        result = store.set("SIGNAL_THRESHOLD", 1.2)
        assert result is False

    def test_rollback_restores_previous(self, store):
        store.set("MIN_ADX", 20.0, reason="initial")
        store.set("MIN_ADX", 25.0, reason="update")
        store.rollback("MIN_ADX")
        assert store.get("MIN_ADX") == pytest.approx(20.0)

    def test_rollback_returns_false_on_first_set(self, store):
        result = store.rollback("MIN_ADX")   # never set → nothing to roll back
        assert result is False

    def test_get_history(self, store):
        store.set("MIN_ADX", 18.0, reason="step1")
        store.set("MIN_ADX", 22.0, reason="step2")
        history = store.get_history("MIN_ADX")
        assert len(history) >= 2
        assert history[0]["value"] == pytest.approx(22.0)   # newest first
        assert history[1]["value"] == pytest.approx(18.0)

    def test_get_all_returns_all_defaults(self, store):
        params = store.get_all()
        assert "SIGNAL_THRESHOLD" in params
        assert "MIN_ADX" in params
        assert "RSI_OS" in params
        assert len(params) == len(store.DEFAULTS)

    def test_snapshot_shows_changed_flag(self, store):
        store.set("MIN_ADX", 20.0)
        snap = store.snapshot()
        assert snap["MIN_ADX"]["changed"] is True
        assert snap["SIGNAL_THRESHOLD"]["changed"] is False

    def test_regime_specific_value(self, store):
        store.set("MIN_ADX", 30.0, regime="trending")
        assert store.get("MIN_ADX", regime="trending") == pytest.approx(30.0)
        assert store.get("MIN_ADX", regime="ranging") == pytest.approx(15.0)  # default

    def test_log_optimization(self, store):
        store.log_optimization(
            pair="EURUSD", regime="all", n_trades=50,
            changes={"MIN_ADX": {"old": 15.0, "new": 18.0}},
            perf_before={"win_rate": 0.55},
            accepted=True, reason="test",
        )
        log = store.get_optimization_log()
        assert len(log) >= 1
        assert log[0]["accepted"] is True


# ═══════════════════════════════════════════════════════════════════════════════
# RollingStats
# ═══════════════════════════════════════════════════════════════════════════════

class TestRollingStats:
    def test_empty_returns_zero(self):
        from adaptive.stats import RollingStats
        rs = RollingStats()
        assert rs.win_rate() == 0.0
        assert rs.profit_factor() == 0.0
        assert rs.avg_r() == 0.0

    def test_all_wins(self):
        from adaptive.stats import RollingStats
        rs = RollingStats()
        for _ in range(10):
            rs.add(r_multiple=1.0, won=True)
        assert rs.win_rate() == pytest.approx(1.0)
        assert rs.profit_factor() == float("inf")
        assert rs.avg_r() > 0

    def test_all_losses(self):
        from adaptive.stats import RollingStats
        rs = RollingStats()
        for _ in range(10):
            rs.add(r_multiple=-1.0, won=False)
        assert rs.win_rate() == pytest.approx(0.0)
        assert rs.profit_factor() == 0.0
        assert rs.avg_r() < 0

    def test_win_rate_50_50(self):
        from adaptive.stats import RollingStats
        rs = RollingStats(alpha=1.0)   # equal weights for deterministic result
        for _ in range(5):
            rs.add(1.0, True)
            rs.add(-1.0, False)
        wr = rs.win_rate()
        assert 0.45 < wr < 0.55

    def test_profit_factor(self):
        from adaptive.stats import RollingStats
        rs = RollingStats(alpha=1.0)
        for _ in range(6):
            rs.add(1.0, True)
        for _ in range(4):
            rs.add(-1.0, False)
        pf = rs.profit_factor()
        assert pf == pytest.approx(6.0 / 4.0, rel=0.05)

    def test_is_sufficient(self):
        from adaptive.stats import RollingStats
        rs = RollingStats()
        for i in range(29):
            rs.add(1.0 if i % 2 == 0 else -1.0, i % 2 == 0)
        assert not rs.is_sufficient(30)
        rs.add(1.0, True)
        assert rs.is_sufficient(30)

    def test_max_drawdown_zero_on_all_wins(self):
        from adaptive.stats import RollingStats
        rs = RollingStats()
        for _ in range(10):
            rs.add(1.0, True)
        assert rs.max_drawdown() == pytest.approx(0.0)

    def test_max_drawdown_positive_on_loss(self):
        from adaptive.stats import RollingStats
        rs = RollingStats()
        rs.add(2.0, True)
        rs.add(-1.0, False)
        rs.add(-1.0, False)
        # equity: 0 → 2 → 1 → 0.  peak=2, trough=0 → dd=2
        assert rs.max_drawdown() == pytest.approx(2.0)

    def test_regime_stats_grouping(self):
        from adaptive.stats import RollingStats
        rs = RollingStats()
        for _ in range(4):
            rs.add(1.0, True, regime="trending")
        for _ in range(6):
            rs.add(-1.0, False, regime="ranging")
        s = rs.regime_stats()
        assert "trending" in s
        assert "ranging" in s
        assert s["trending"]["win_rate"] == pytest.approx(1.0)
        assert s["ranging"]["win_rate"] == pytest.approx(0.0)

    def test_ewma_weights_recent_bias(self):
        from adaptive.stats import RollingStats
        rs = RollingStats(window=4, alpha=0.5)
        # Alternate: old losses, then recent wins
        rs.add(-1.0, False)
        rs.add(-1.0, False)
        rs.add(1.0, True)
        rs.add(1.0, True)
        # With alpha=0.5 recent trades dominate → win_rate should be > 0.5
        assert rs.win_rate() > 0.5

    def test_to_dict_keys(self):
        from adaptive.stats import RollingStats
        rs = RollingStats()
        rs.add(1.0, True)
        d = rs.to_dict()
        for k in ("n", "win_rate", "profit_factor", "avg_r", "expectancy",
                  "max_drawdown", "sharpe"):
            assert k in d


# ═══════════════════════════════════════════════════════════════════════════════
# RegimeDetector
# ═══════════════════════════════════════════════════════════════════════════════

class TestRegimeDetector:
    def test_trending(self):
        from adaptive.regime import RegimeDetector
        rd = RegimeDetector()
        assert rd.detect(adx=35.0) == "trending"

    def test_ranging(self):
        from adaptive.regime import RegimeDetector
        rd = RegimeDetector()
        assert rd.detect(adx=15.0) == "ranging"

    def test_transitional(self):
        from adaptive.regime import RegimeDetector
        rd = RegimeDetector()
        assert rd.detect(adx=24.0) == "transitional"

    def test_none_adx(self):
        from adaptive.regime import RegimeDetector
        rd = RegimeDetector()
        assert rd.detect(adx=None) == "unknown"

    def test_volatile_override(self):
        from adaptive.regime import RegimeDetector
        rd = RegimeDetector(volatility_z=1.0)
        # History with realistic variance: mean≈0.0010, stdev≈0.0002
        history = [0.0008, 0.0009, 0.0010, 0.0011, 0.0012] * 4
        # current ATR = 0.0020 → z ≈ (0.0020-0.001)/0.0002 = 5 → volatile
        assert rd.detect(adx=15.0, atr=0.0020, atr_history=history) == "volatile"

    def test_quiet_override(self):
        from adaptive.regime import RegimeDetector
        rd = RegimeDetector(volatility_z=1.0)
        # History with realistic variance: mean≈0.0030, stdev≈0.0002
        history = [0.0028, 0.0029, 0.0030, 0.0031, 0.0032] * 4
        # current ATR = 0.0005 → z ≈ (0.0005-0.003)/0.0002 = -12.5 → quiet
        assert rd.detect(adx=35.0, atr=0.0005, atr_history=history) == "quiet"

    def test_module_detect(self):
        from adaptive.regime import detect
        assert detect(adx=30.0) == "trending"


# ═══════════════════════════════════════════════════════════════════════════════
# OverfittingGuard
# ═══════════════════════════════════════════════════════════════════════════════

class TestOverfittingGuard:
    def test_check_safe_blocks_excessive_change(self, store):
        from adaptive.guard import OverfittingGuard
        guard = OverfittingGuard()
        # SIGNAL_THRESHOLD range = 0.5 to 3.0 (span = 2.5).
        # 20 % of 2.5 = 0.5 max change.  Going from 1.2 to 1.8 = 0.6 → blocked.
        assert not guard.check_change_safe("SIGNAL_THRESHOLD", 1.2, 1.8, store)

    def test_check_safe_allows_small_change(self, store):
        from adaptive.guard import OverfittingGuard
        guard = OverfittingGuard()
        # 0.3 change on span 2.5 = 12 % < 20 % → allowed
        assert guard.check_change_safe("SIGNAL_THRESHOLD", 1.2, 1.5, store)

    def test_blocks_while_in_shadow(self, store):
        from adaptive.guard import OverfittingGuard
        guard = OverfittingGuard()
        guard.start_shadow("MIN_ADX", 15.0, 18.0, baseline_avg_r=0.5)
        assert not guard.check_change_safe("MIN_ADX", 18.0, 20.0, store)

    def test_shadow_rollback_on_degradation(self, store):
        from adaptive.guard import OverfittingGuard, SHADOW_TRADES
        guard = OverfittingGuard()
        store.set("MIN_ADX", 20.0, reason="update")
        guard.start_shadow("MIN_ADX", 15.0, 20.0, baseline_avg_r=1.0)
        # Feed shadow trades with terrible performance
        for _ in range(SHADOW_TRADES):
            guard.record_trade(r_multiple=-2.0, won=False)
        rolled = guard.check_rollbacks(store)
        assert "MIN_ADX" in rolled

    def test_shadow_confirmed_on_good_performance(self, store):
        from adaptive.guard import OverfittingGuard, SHADOW_TRADES
        guard = OverfittingGuard()
        store.set("MIN_ADX", 20.0, reason="update")
        guard.start_shadow("MIN_ADX", 15.0, 20.0, baseline_avg_r=0.5)
        for _ in range(SHADOW_TRADES):
            guard.record_trade(r_multiple=1.0, won=True)
        rolled = guard.check_rollbacks(store)
        assert "MIN_ADX" not in rolled

    def test_shadow_status(self):
        from adaptive.guard import OverfittingGuard
        guard = OverfittingGuard()
        guard.start_shadow("SIGNAL_THRESHOLD", 1.2, 1.4, baseline_avg_r=0.3)
        s = guard.shadow_status()
        assert "SIGNAL_THRESHOLD" in s
        assert s["SIGNAL_THRESHOLD"]["remaining"] > 0

    def test_has_active_shadows(self):
        from adaptive.guard import OverfittingGuard
        guard = OverfittingGuard()
        assert not guard.has_active_shadows()
        guard.start_shadow("MIN_ADX", 15.0, 18.0, 0.5)
        assert guard.has_active_shadows()


# ═══════════════════════════════════════════════════════════════════════════════
# WeightLearner
# ═══════════════════════════════════════════════════════════════════════════════

class TestWeightLearner:
    def test_per_trade_win_increases_correct_weight(self, populated_db):
        import storage
        from adaptive.weight_learner import update_per_trade
        storage.set_weight(populated_db, "rsi", 1.0)
        # BUY signal, RSI voted +1, trade won → weight should increase
        update_per_trade(populated_db, {"rsi": 1, "macd": 0}, "BUY", won=True)
        w = storage.get_weight(populated_db, "rsi")
        assert w > 1.0

    def test_per_trade_loss_decreases_correct_weight(self, populated_db):
        import storage
        from adaptive.weight_learner import update_per_trade
        storage.set_weight(populated_db, "rsi", 1.0)
        # BUY signal, RSI voted +1, trade lost → weight should decrease
        update_per_trade(populated_db, {"rsi": 1, "macd": 0}, "BUY", won=False)
        w = storage.get_weight(populated_db, "rsi")
        assert w < 1.0

    def test_zero_vote_not_changed(self, populated_db):
        import storage
        from adaptive.weight_learner import update_per_trade
        storage.set_weight(populated_db, "macd", 1.0)
        update_per_trade(populated_db, {"rsi": 1, "macd": 0}, "BUY", won=True)
        assert storage.get_weight(populated_db, "macd") == pytest.approx(1.0)

    def test_batch_rebalance_dry_run(self, populated_db):
        from adaptive.stats import RollingStats
        from adaptive.weight_learner import batch_rebalance
        rs = RollingStats()
        # rsi votes on many winning trades, macd votes on many losing trades
        for _ in range(35):
            rs.add(1.0, True, votes={"rsi": 1, "macd": -1})
        for _ in range(15):
            rs.add(-1.0, False, votes={"rsi": -1, "macd": 1})
        changes = batch_rebalance(populated_db, rs, dry_run=True)
        # Dry run should not write to DB but should return changes
        assert isinstance(changes, dict)

    def test_batch_rebalance_below_min_sample(self, populated_db):
        from adaptive.stats import RollingStats
        from adaptive.weight_learner import batch_rebalance, _BATCH_MIN_TRADES
        rs = RollingStats()
        for i in range(_BATCH_MIN_TRADES - 1):
            rs.add(1.0, True)
        changes = batch_rebalance(populated_db, rs, dry_run=True)
        assert changes == {}


# ═══════════════════════════════════════════════════════════════════════════════
# ParamLearner
# ═══════════════════════════════════════════════════════════════════════════════

class TestParamLearner:
    def _make_contexts(self, n=60, good_threshold=1.2):
        """Generate synthetic trade context dicts."""
        import random
        rng = random.Random(42)
        contexts = []
        for i in range(n):
            score = rng.uniform(0.5, 3.0)
            adx   = rng.uniform(10, 40)
            conf  = rng.randint(40, 95)
            # Trades with high score/adx are more likely to win
            win_prob = 0.3 + 0.4 * (score > good_threshold) + 0.2 * (adx > 20)
            won = rng.random() < win_prob
            r   = rng.uniform(0.3, 1.5) if won else -rng.uniform(0.3, 1.5)
            contexts.append({
                "signal_id":  i,
                "score":      round(score, 3),
                "adx":        round(adx, 1),
                "confidence": conf,
                "rsi":        rng.uniform(20, 80),
                "stoch_k":    rng.uniform(10, 90),
                "r_multiple": round(r, 4),
                "won":        int(won),
                "regime":     "ranging",
                "session":    "overlap",
                "created_at": f"2025-01-{i+1:02d}T12:00:00+00:00",
            })
        return contexts

    def test_optimize_does_not_crash(self, store, tmp_db):
        from adaptive.param_learner import ParamLearner
        from adaptive.guard import OverfittingGuard
        import storage
        storage.init_db(tmp_db)

        learner = ParamLearner()
        guard   = OverfittingGuard()
        contexts = self._make_contexts(60)
        changes = learner.optimize(
            db=tmp_db, param_store=store, contexts=contexts,
            guard=guard, pair="EURUSD",
        )
        assert isinstance(changes, dict)

    def test_optimize_respects_min_sample(self, store, tmp_db):
        from adaptive.param_learner import ParamLearner
        from adaptive.guard import OverfittingGuard
        import storage
        storage.init_db(tmp_db)

        learner  = ParamLearner()
        guard    = OverfittingGuard()
        contexts = self._make_contexts(10)    # too few
        changes  = learner.optimize(
            db=tmp_db, param_store=store, contexts=contexts,
            guard=guard, pair="EURUSD",
        )
        assert changes == {}

    def test_optimize_stays_within_bounds(self, store, tmp_db):
        from adaptive.param_learner import ParamLearner
        from adaptive.guard import OverfittingGuard
        import storage
        storage.init_db(tmp_db)

        learner  = ParamLearner()
        guard    = OverfittingGuard()
        contexts = self._make_contexts(80)
        learner.optimize(
            db=tmp_db, param_store=store, contexts=contexts,
            guard=guard, pair="EURUSD",
        )
        # Every adapted param must stay within its declared bounds
        for name, defn in store.DEFAULTS.items():
            v = store.get(name)
            if v is not None:
                assert v >= defn["min"] - 1e-9, f"{name}={v} below min {defn['min']}"
                assert v <= defn["max"] + 1e-9, f"{name}={v} above max {defn['max']}"


# ═══════════════════════════════════════════════════════════════════════════════
# Context recording
# ═══════════════════════════════════════════════════════════════════════════════

class TestContext:
    def test_capture_and_retrieve(self, populated_db):
        from adaptive.context import capture_context, get_recent_contexts
        sig = {
            "direction": "BUY",
            "entry":     1.1000,
            "sl":        1.0950,
            "tp1":       1.1030,
            "score":     1.8,
            "confidence": 70,
            "trend_strength": 28,
            "timeframe":  "30m",
            "contributors": {"rsi": 1, "macd": 1},
            "_ctx": {
                "indicator_values": {"rsi": 42.5, "atr": 0.0012},
                "adaptive_params": {"SIGNAL_THRESHOLD": 1.2},
                "weights": {"rsi": 1.0},
            },
        }
        capture_context(populated_db, signal_id=1, sig=sig,
                        pair="EURUSD", regime="trending")
        rows = get_recent_contexts(populated_db, n=10)
        # No outcomes yet — get_recent_contexts filters to completed trades
        assert isinstance(rows, list)

    def test_update_outcome(self, populated_db):
        from adaptive.context import capture_context, update_outcome, get_all_contexts
        import sqlite3
        sig = {
            "direction": "SELL", "entry": 1.1000, "sl": 1.1050,
            "tp1": 1.0970, "score": -1.5, "confidence": 65,
            "trend_strength": 32, "timeframe": "30m",
            "contributors": {"rsi": -1}, "_ctx": {},
        }
        capture_context(populated_db, signal_id=99, sig=sig, pair="GBPUSD")
        update_outcome(
            populated_db, signal_id=99, status="WIN",
            close_price=1.0970, entry=1.1000, sl=1.1050,
            pnl=3.0, hold_hours=1.5,
        )
        rows = get_all_contexts(populated_db)
        assert any(r["signal_id"] == 99 and r["won"] == 1 for r in rows)

    def test_r_multiple_computed_correctly(self, populated_db):
        from adaptive.context import capture_context, update_outcome, get_all_contexts
        sig = {
            "direction": "BUY", "entry": 1.1000, "sl": 1.0990,
            "tp1": 1.1010, "score": 1.5, "confidence": 60,
            "trend_strength": 25, "timeframe": "30m",
            "contributors": {}, "_ctx": {},
        }
        capture_context(populated_db, signal_id=55, sig=sig, pair="EURUSD")
        # Risk = 1.1000 - 1.0990 = 0.0010
        # WIN at 1.1010 → reward = 0.0010 → R = 1.0
        update_outcome(
            populated_db, signal_id=55, status="WIN",
            close_price=1.1010, entry=1.1000, sl=1.0990,
            pnl=1.0, hold_hours=0.5,
        )
        rows = get_all_contexts(populated_db)
        row = next(r for r in rows if r["signal_id"] == 55)
        assert row["r_multiple"] == pytest.approx(1.0, rel=0.01)


# ═══════════════════════════════════════════════════════════════════════════════
# Techniques (backward-compat + adaptive params)
# ═══════════════════════════════════════════════════════════════════════════════

class TestTechniquesAdaptive:
    """Ensure techniques work identically with and without a params dict."""

    @pytest.fixture
    def df(self):
        import pandas as pd
        import numpy as np
        rng = np.random.default_rng(0)
        n = 300
        close = 1.1000 + np.cumsum(rng.normal(0, 0.0003, n))
        high  = close + rng.uniform(0.0001, 0.0005, n)
        low   = close - rng.uniform(0.0001, 0.0005, n)
        return pd.DataFrame({"open": close, "high": high, "low": low, "close": close,
                             "volume": rng.integers(100, 1000, n).astype(float)})

    def test_get_votes_no_params(self, df):
        from techniques import get_votes
        votes = get_votes(df)
        assert set(votes.keys()) == {"rsi", "ma_cross", "macd", "bbands",
                                     "ema_trend", "stoch", "supertrend"}
        for v in votes.values():
            assert v in (-1, 0, 1)

    def test_get_votes_with_params(self, df):
        from techniques import get_votes
        params = {"RSI_OS": 40, "RSI_OB": 60, "STOCH_OS": 35, "STOCH_OB": 65}
        votes = get_votes(df, params=params)
        for v in votes.values():
            assert v in (-1, 0, 1)

    def test_get_indicator_values_returns_dict(self, df):
        from techniques import get_indicator_values
        vals = get_indicator_values(df)
        assert "rsi" in vals
        assert "atr" in vals
        assert "macd_hist" in vals
        assert "bb_pct" in vals
        assert "ema_above" in vals

    def test_indicator_values_with_params(self, df):
        from techniques import get_indicator_values
        params = {"RSI_PERIOD": 7, "BB_PERIOD": 10, "BB_STD": 2.0}
        vals = get_indicator_values(df, params=params)
        assert vals["rsi"] is not None


# ═══════════════════════════════════════════════════════════════════════════════
# Scheduler (integration smoke test)
# ═══════════════════════════════════════════════════════════════════════════════

class TestScheduler:
    def test_on_trade_closed_no_crash(self, populated_db):
        import storage
        from adaptive.scheduler import AdaptiveScheduler
        # Insert a dummy signal so trade_context capture has somewhere to write
        storage.init_db(populated_db)
        sched = AdaptiveScheduler("EURUSD")
        # Should not raise
        sched.on_trade_closed(
            db=populated_db, signal_id=1,
            direction="BUY", won=True,
            close_price=1.1020, entry=1.1000, sl=1.0980,
            pnl=2.0, hold_hours=1.0,
        )

    def test_run_cycle_no_crash(self, populated_db):
        from adaptive.scheduler import AdaptiveScheduler
        sched = AdaptiveScheduler("EURUSD")
        sched.run_cycle(populated_db, {})

    def test_stats_summary_keys(self, populated_db):
        from adaptive.scheduler import AdaptiveScheduler
        sched = AdaptiveScheduler("EURUSD")
        sched.on_trade_closed(
            db=populated_db, signal_id=2,
            direction="SELL", won=False,
            close_price=1.1060, entry=1.1000, sl=1.1050,
            pnl=-2.0, hold_hours=0.5,
        )
        s = sched.stats_summary()
        assert "n" in s
        assert "win_rate" in s

    def test_module_level_on_trade_closed(self, populated_db):
        import adaptive.scheduler as sched
        sched.on_trade_closed(
            symbol="GBPUSD", db=populated_db, signal_id=3,
            direction="BUY", won=True,
            close_price=1.3020, entry=1.3000, sl=1.2980,
            pnl=2.0, hold_hours=0.8,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Reporter (smoke test — only checks it doesn't crash)
# ═══════════════════════════════════════════════════════════════════════════════

class TestReporter:
    def test_generate_returns_dict(self, populated_db):
        import storage
        storage.init_db(populated_db)
        from adaptive.reporter import generate
        report = generate(populated_db, "EURUSD")
        assert isinstance(report, dict)
        assert "overall_stats" in report
        assert "indicator_stats" in report
        assert "param_snapshot" in report

    def test_export_json(self, populated_db, tmp_path):
        from adaptive.reporter import export_json
        path = str(tmp_path / "report.json")
        out = export_json(populated_db, "EURUSD", path=path)
        assert os.path.exists(out)
        with open(out) as f:
            data = json.load(f)
        assert "symbol" in data
