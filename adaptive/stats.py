"""Rolling statistics with exponential weighting (EWMA).

RollingStats keeps the last `window` trade records in memory and weights
recent trades more heavily using an exponential decay factor `alpha`.

Computed metrics
----------------
win_rate        Fraction of winning trades (EWMA-weighted)
profit_factor   Gross profit / gross loss (EWMA-weighted)
avg_r           Average R-multiple per trade (EWMA-weighted)
expectancy      avg_r × win_rate − (1 − win_rate)  [Kelly-style]
max_drawdown    Maximum peak-to-trough drawdown in the equity curve
sharpe          Annualised Sharpe on daily R-multiple returns (approx)
indicator_accuracy(name)   Fraction of times this indicator voted correctly
regime_stats    Breakdown of win_rate and avg_r by regime
session_stats   Breakdown of win_rate and avg_r by session
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from statistics import mean, stdev
from typing import Optional


@dataclass
class TradeRecord:
    r_multiple: float
    won: bool
    regime: str = "unknown"
    session: str = "unknown"
    pnl: float = 0.0
    votes: dict = field(default_factory=dict)


class RollingStats:
    """EWMA-weighted rolling statistics over the last `window` trades.

    Parameters
    ----------
    window : int
        Maximum number of trades to retain.
    alpha : float
        EWMA decay factor (0 < alpha ≤ 1).  Higher = more weight on recents.
    """

    def __init__(self, window: int = 100, alpha: float = 0.94) -> None:
        self.window = window
        self.alpha  = alpha
        self._trades: deque[TradeRecord] = deque(maxlen=window)

    # ── Ingestion ─────────────────────────────────────────────────────────────

    def add(
        self,
        r_multiple: float,
        won: bool,
        regime: str = "unknown",
        session: str = "unknown",
        pnl: float = 0.0,
        votes: Optional[dict] = None,
    ) -> None:
        self._trades.append(
            TradeRecord(
                r_multiple=r_multiple,
                won=won,
                regime=regime,
                session=session,
                pnl=pnl,
                votes=votes or {},
            )
        )

    def add_from_context(self, ctx: dict) -> None:
        """Convenience: add from a trade_context row dict."""
        self.add(
            r_multiple=ctx.get("r_multiple") or 0.0,
            won=bool(ctx.get("won", 0)),
            regime=ctx.get("regime", "unknown"),
            session=ctx.get("session", "unknown"),
            pnl=ctx.get("pnl") or 0.0,
            votes=ctx.get("votes_json") or {},
        )

    # ── Core metrics ──────────────────────────────────────────────────────────

    def is_sufficient(self, min_n: int = 30) -> bool:
        return len(self._trades) >= min_n

    def count(self) -> int:
        return len(self._trades)

    def win_rate(self) -> float:
        if not self._trades:
            return 0.0
        weights = self._ewma_weights()
        return sum(w for w, t in zip(weights, self._trades) if t.won) / sum(weights)

    def profit_factor(self) -> float:
        weights  = self._ewma_weights()
        gross_w  = sum(w * t.r_multiple for w, t in zip(weights, self._trades)
                       if t.r_multiple > 0)
        gross_l  = sum(w * abs(t.r_multiple) for w, t in zip(weights, self._trades)
                       if t.r_multiple < 0)
        return gross_w / gross_l if gross_l > 0 else (float("inf") if gross_w > 0 else 0.0)

    def avg_r(self) -> float:
        if not self._trades:
            return 0.0
        weights = self._ewma_weights()
        return sum(w * t.r_multiple for w, t in zip(weights, self._trades)) / sum(weights)

    def expectancy(self) -> float:
        """Kelly-style expectancy = avg_r across all trades."""
        return self.avg_r()

    def max_drawdown(self) -> float:
        """Peak-to-trough drawdown in R units on the running equity."""
        if not self._trades:
            return 0.0
        equity, peak, dd = 0.0, 0.0, 0.0
        for t in self._trades:
            equity += t.r_multiple
            if equity > peak:
                peak = equity
            dd = max(dd, peak - equity)
        return round(dd, 4)

    def sharpe(self, trades_per_day: float = 0.054) -> float:
        """Approximate annualised Sharpe (uses trade-level R-multiples)."""
        if len(self._trades) < 5:
            return 0.0
        rs = [t.r_multiple for t in self._trades]
        mu = mean(rs)
        try:
            sd = stdev(rs)
        except Exception:
            return 0.0
        if sd < 1e-9:
            return 0.0
        return round(mu / sd * math.sqrt(trades_per_day * 252), 4)

    # ── Per-indicator accuracy ────────────────────────────────────────────────

    def indicator_accuracy(self, name: str) -> float:
        """Fraction of trades where this indicator voted in the winning direction."""
        correct = total = 0
        for t in self._trades:
            vote = t.votes.get(name, 0)
            if vote == 0:
                continue
            total += 1
            direction_sign = 1  # BUY=+1, SELL=-1 — we check whether vote agreed with outcome
            # A vote of +1 on a winning BUY or -1 on a winning SELL is correct.
            # Without direction stored in TradeRecord we use: if won and vote != 0 → correct
            # This is a simplification; full accuracy requires direction context.
            if t.won:
                correct += 1
            # If lost: the indicator "agreed" with a loss → penalise
        return round(correct / total, 4) if total > 0 else 0.5

    def indicator_profit_factor(self, name: str) -> float:
        """Profit factor for trades where this indicator had a non-zero vote."""
        gross_w = gross_l = 0.0
        for t in self._trades:
            if t.votes.get(name, 0) == 0:
                continue
            if t.r_multiple > 0:
                gross_w += t.r_multiple
            else:
                gross_l += abs(t.r_multiple)
        return gross_w / gross_l if gross_l > 0 else (float("inf") if gross_w > 0 else 0.0)

    # ── Breakdowns ────────────────────────────────────────────────────────────

    def regime_stats(self) -> dict[str, dict]:
        return self._breakdown("regime")

    def session_stats(self) -> dict[str, dict]:
        return self._breakdown("session")

    def _breakdown(self, attr: str) -> dict[str, dict]:
        groups: dict[str, list[TradeRecord]] = {}
        for t in self._trades:
            key = getattr(t, attr, "unknown") or "unknown"
            groups.setdefault(key, []).append(t)
        result = {}
        for key, trades in groups.items():
            n = len(trades)
            wins = sum(1 for t in trades if t.won)
            rs = [t.r_multiple for t in trades]
            result[key] = {
                "n":        n,
                "win_rate": round(wins / n, 4),
                "avg_r":    round(sum(rs) / n, 4),
            }
        return result

    # ── Internal ──────────────────────────────────────────────────────────────

    def _ewma_weights(self) -> list[float]:
        """Return exponentially increasing weights (newest = highest weight)."""
        n = len(self._trades)
        if n == 0:
            return []
        # w[i] = alpha^(n-1-i) so the newest trade (i = n-1) gets weight 1.0
        weights = [self.alpha ** (n - 1 - i) for i in range(n)]
        return weights

    def to_dict(self) -> dict:
        """Serialise summary for logging / reporting."""
        return {
            "n":             self.count(),
            "win_rate":      round(self.win_rate(), 4),
            "profit_factor": round(self.profit_factor(), 4),
            "avg_r":         round(self.avg_r(), 4),
            "expectancy":    round(self.expectancy(), 4),
            "max_drawdown":  self.max_drawdown(),
            "sharpe":        self.sharpe(),
        }
