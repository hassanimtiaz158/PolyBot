"""Composite market quality score computation."""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class MarketQuality:
    """Computes a composite quality score for a market."""

    def score(self, snapshot: dict[str, Any]) -> float:
        """Calculate a 0–1 quality score from snapshot features."""
        spread = float(snapshot.get("relative_spread", 1.0))
        depth = float(snapshot.get("total_depth", 0.0))
        volume = float(snapshot.get("volume", 0.0))

        spread_score = max(0.0, 1.0 - spread * 10)
        depth_score = min(1.0, depth / 10000.0)
        volume_score = min(1.0, volume / 100000.0)

        return round(0.4 * spread_score + 0.3 * depth_score + 0.3 * volume_score, 4)
