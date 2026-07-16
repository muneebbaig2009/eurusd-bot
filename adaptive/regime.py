"""Market regime classification.

Classifies each trade's market context into one of five regimes using ADX
and optionally the relative ATR level.  Separate parameter sets can be
maintained per regime so the bot uses tighter thresholds when the market
is ranging and looser ones when it is strongly trending.

Regimes
-------
trending        ADX ≥ 28
transitional    20 ≤ ADX < 28
ranging         ADX < 20  (normal volatility)
volatile        ATR significantly above recent average (any ADX level)
quiet           ATR significantly below recent average (any ADX level)
"""
from __future__ import annotations

from statistics import mean, stdev
from typing import Optional


class RegimeDetector:
    """Classify market conditions from indicator readings.

    Parameters
    ----------
    trend_threshold : float
        ADX level above which the market is considered trending.
    range_threshold : float
        ADX level below which the market is considered ranging.
    volatility_z : float
        Number of standard deviations above/below the mean ATR that
        qualifies as 'volatile' or 'quiet'.
    """

    def __init__(
        self,
        trend_threshold: float = 28.0,
        range_threshold: float = 20.0,
        volatility_z: float = 1.0,
    ) -> None:
        self.trend_threshold = trend_threshold
        self.range_threshold = range_threshold
        self.volatility_z = volatility_z

    def detect(
        self,
        adx: Optional[float],
        atr: Optional[float] = None,
        atr_history: Optional[list[float]] = None,
    ) -> str:
        """Return a regime string for the given indicator values.

        Parameters
        ----------
        adx : float | None
            Current ADX reading.
        atr : float | None
            Current ATR value.
        atr_history : list[float] | None
            Recent ATR values used to compute a z-score.  If provided and
            long enough (≥ 10 points), volatility regime overrides the ADX
            classification.
        """
        # ── Volatility override (ATR z-score) ────────────────────────────────
        if atr is not None and atr_history and len(atr_history) >= 10:
            try:
                mu = mean(atr_history)
                sigma = stdev(atr_history)
                if sigma > 0:
                    z = (atr - mu) / sigma
                    if z > self.volatility_z:
                        return "volatile"
                    if z < -self.volatility_z:
                        return "quiet"
            except Exception:
                pass

        # ── ADX-based classification ──────────────────────────────────────────
        if adx is None:
            return "unknown"
        if adx >= self.trend_threshold:
            return "trending"
        if adx >= self.range_threshold:
            return "transitional"
        return "ranging"

    def detect_from_signal(self, sig: dict) -> str:
        """Convenience wrapper: extract adx from a signal dict."""
        adx = sig.get("trend_strength") or sig.get("adx")
        return self.detect(adx)


# Module-level singleton for lightweight usage
_detector = RegimeDetector()


def detect(adx: Optional[float], atr: Optional[float] = None,
           atr_history: Optional[list[float]] = None) -> str:
    return _detector.detect(adx, atr, atr_history)


def detect_from_signal(sig: dict) -> str:
    return _detector.detect_from_signal(sig)
