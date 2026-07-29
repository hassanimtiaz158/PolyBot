"""Order-book feature calculations: OBI, spread, depth, midpoint."""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class OrderBookFeatures:
    """Computes order-book-derived features from snapshot data."""

    def midpoint(self, bid: float, ask: float) -> float:
        """Calculate the midpoint price."""
        return (bid + ask) / 2.0

    def spread(self, bid: float, ask: float) -> float:
        """Calculate the absolute spread."""
        return ask - bid

    def relative_spread(self, bid: float, ask: float) -> float:
        """Calculate spread as a fraction of the midpoint."""
        mid = self.midpoint(bid, ask)
        return (ask - bid) / mid if mid != 0 else 0.0

    def order_book_imbalance(self, bid_depth: float, ask_depth: float) -> float:
        """Calculate the Order-Book Imbalance (OBI) ratio.

        OBI = (bid_depth - ask_depth) / (bid_depth + ask_depth)
        """
        total = bid_depth + ask_depth
        if total == 0:
            return 0.0
        return (bid_depth - ask_depth) / total

    def compute(self, snapshot: dict[str, Any]) -> dict[str, float]:
        """Compute all order-book features from a snapshot."""
        bid = snapshot.get("bid", 0.0)
        ask = snapshot.get("ask", 0.0)
        bid_depth = snapshot.get("bid_depth", 0.0)
        ask_depth = snapshot.get("ask_depth", 0.0)
        return {
            "midpoint": self.midpoint(bid, ask),
            "spread": self.spread(bid, ask),
            "relative_spread": self.relative_spread(bid, ask),
            "obi": self.order_book_imbalance(bid_depth, ask_depth),
        }
