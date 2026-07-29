"""Ensemble strategy (S5).

Combines signals from multiple strategies; disagreement reduces confidence.
"""

from typing import Any

from app.strategies.base import Signal, Strategy


class EnsembleStrategy(Strategy):
    """Combines independently validated signals into a single decision."""

    name = "ensemble"

    def generate_signal(
        self, snapshot: dict[str, Any], features: dict[str, Any], context: dict[str, Any]
    ) -> Signal | None:
        return None
