"""Fair probability model strategy (S3).

Placeholder — returns ``NO_SIGNAL`` until a probability model is
integrated.
"""

from __future__ import annotations

from typing import Any

from app.strategies.base import Signal, Strategy


class ProbabilityStrategy(Strategy):
    """Generates signals from fair probability model estimates."""

    name = "probability"

    def generate_signal(
        self,
        features: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> Signal:
        market_id: str = features.get("market_id", "unknown")
        return self._reject(market_id, "Probability model not yet implemented")
