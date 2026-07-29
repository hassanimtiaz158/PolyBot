"""Fair probability model strategy (S3).

Uses calibrated probability models to estimate P(outcome | market features).
"""

from typing import Any

from app.strategies.base import Signal, Strategy


class ProbabilityStrategy(Strategy):
    """Generates signals from fair probability model estimates."""

    name = "probability"

    def generate_signal(
        self, snapshot: dict[str, Any], features: dict[str, Any], context: dict[str, Any]
    ) -> Signal | None:
        return None
