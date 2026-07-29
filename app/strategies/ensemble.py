"""Ensemble strategy (S5).

Placeholder — returns ``NO_SIGNAL`` until aggregation logic is
implemented.
"""

from __future__ import annotations

from typing import Any

from app.strategies.base import Signal, Strategy


class EnsembleStrategy(Strategy):
    """Combines independently validated signals into a single decision."""

    name = "ensemble"

    def generate_signal(
        self,
        features: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> Signal:
        market_id: str = features.get("market_id", "unknown")
        return self._reject(
            market_id, "Ensemble aggregation not yet implemented"
        )
