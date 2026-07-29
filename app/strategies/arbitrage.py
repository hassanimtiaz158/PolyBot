"""Cross-market / logical inconsistency detection strategy (S2).

Detects explicit relationships such as implication, mutual exclusivity,
complement, and sum constraints.
"""

from typing import Any

from app.strategies.base import Signal, Strategy


class ArbitrageStrategy(Strategy):
    """Generates signals from cross-market pricing inconsistencies."""

    name = "arbitrage"

    def generate_signal(
        self, snapshot: dict[str, Any], features: dict[str, Any], context: dict[str, Any]
    ) -> Signal | None:
        return None
