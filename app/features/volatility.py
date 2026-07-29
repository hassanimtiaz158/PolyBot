"""Rolling realised volatility features."""

import logging
from collections import deque
from math import sqrt
from typing import Any

logger = logging.getLogger(__name__)


class VolatilityFeatures:
    """Computes rolling realised volatility on sampled prices."""

    def __init__(self, window: int = 20) -> None:
        self.window = window
        self._prices: deque[float] = deque(maxlen=window + 1)

    def update(self, price: float) -> None:
        """Record a new price observation."""
        self._prices.append(price)

    def realised_volatility(self) -> float | None:
        """Calculate realised volatility over the rolling window."""
        if len(self._prices) < 2:
            return None
        returns = [
            (self._prices[i] - self._prices[i - 1]) / self._prices[i - 1]
            for i in range(1, len(self._prices))
        ]
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / len(returns)
        return sqrt(variance)

    def compute(self, snapshot: dict[str, Any]) -> dict[str, float | None]:
        """Compute volatility from a snapshot's midpoint."""
        price = snapshot.get("midpoint")
        if price is not None:
            self.update(price)
        return {"realised_volatility": self.realised_volatility()}
