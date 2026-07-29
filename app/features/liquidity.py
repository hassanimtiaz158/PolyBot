"""Liquidity estimation from order-book depth and volume."""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class LiquidityFeatures:
    """Estimates market liquidity from depth, volume, and spread."""

    def depth_liquidity(self, bid_depth: float, ask_depth: float) -> float:
        """Simple depth-based liquidity estimate."""
        return bid_depth + ask_depth

    def compute(self, snapshot: dict[str, Any]) -> dict[str, float]:
        """Compute liquidity metrics from a snapshot."""
        bid_depth = snapshot.get("bid_depth", 0.0)
        ask_depth = snapshot.get("ask_depth", 0.0)
        volume = snapshot.get("volume", 0.0)
        return {
            "total_depth": self.depth_liquidity(bid_depth, ask_depth),
            "volume": volume,
        }
