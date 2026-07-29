"""Market microstructure strategy (S1).

Uses order-book imbalance, spread, and momentum to generate signals.
"""

from typing import Any

from app.strategies.base import Signal, Strategy


class MicrostructureStrategy(Strategy):
    """Generates signals based on order-book microstructure."""

    name = "microstructure"

    def generate_signal(
        self, snapshot: dict[str, Any], features: dict[str, Any], context: dict[str, Any]
    ) -> Signal | None:
        return None
