"""Rolling realised-volatility features computed on log returns."""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
from math import log, sqrt
from typing import Any

from app.storage.models import MarketSnapshot


class VolatilityFeatures:
    """Computes rolling realised volatility from sampled midpoint prices.

    Uses log returns over a configurable rolling window.  Volatility is
    the sample standard deviation of log returns.

    Usage::

        vf = VolatilityFeatures(window=20)
        vf.update(0.45)
        vol = vf.realised_volatility()
        features = vf.compute(snapshot)
    """

    def __init__(self, window: int = 20) -> None:
        if window < 2:
            raise ValueError(f"window must be >= 2, got {window}")
        self.window = window
        self._prices: deque[float] = deque(maxlen=window + 1)

    # ── Public API ─────────────────────────────────────────────────

    def update(self, midpoint: float) -> None:
        """Record a new midpoint price observation."""
        self._prices.append(midpoint)

    def realised_volatility(self) -> float | None:
        """Sample standard deviation of log returns over the rolling window.

        Returns ``None`` when fewer than 2 observations are available.
        """
        if len(self._prices) < 2:
            return None

        log_returns: list[float] = []
        for i in range(1, len(self._prices)):
            prev = self._prices[i - 1]
            curr = self._prices[i]
            if prev > 0 and curr > 0:
                log_returns.append(log(curr / prev))

        if len(log_returns) < 1:
            return None

        mean = sum(log_returns) / len(log_returns)
        if len(log_returns) == 1:
            variance = 0.0
        else:
            variance = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
        return sqrt(variance)

    # ── Batch compute ──────────────────────────────────────────────

    def compute(
        self, snapshot: MarketSnapshot | dict[str, Any]
    ) -> dict[str, Any]:
        """Update internal buffer and return volatility features.

        Returns a dict with keys ``realised_volatility``,
        ``volatility_window``, ``volatility_observations``, and
        ``timestamp``.
        """
        s = _unwrap(snapshot)

        if s.midpoint is not None:
            self.update(s.midpoint)

        return {
            "realised_volatility": self.realised_volatility(),
            "volatility_window": self.window,
            "volatility_observations": len(self._prices),
            "timestamp": s.timestamp or datetime.now(UTC).isoformat(),
        }


def _unwrap(
    snapshot: MarketSnapshot | dict[str, Any],
) -> MarketSnapshot:
    if isinstance(snapshot, MarketSnapshot):
        return snapshot
    return MarketSnapshot.from_row(snapshot)
