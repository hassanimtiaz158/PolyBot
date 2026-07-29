"""Execution cost estimation: spread, slippage, fees, and uncertainty penalty."""

import logging

logger = logging.getLogger(__name__)


class CostEstimator:
    """Estimates total execution cost for a potential trade."""

    def spread_cost(self, spread: float, price: float) -> float:
        """Calculate the cost of crossing the spread as a fraction."""
        return spread / price if price != 0 else 0.0

    def slippage_cost(self, size: float, depth: float, base_cost: float = 0.001) -> float:
        """Estimate slippage based on trade size relative to depth."""
        if depth == 0:
            return base_cost
        return min(base_cost * (size / depth), 0.05)

    def fee_cost(self, fee_rate: float = 0.0) -> float:
        """Return the fee cost as a fraction (maker/taker)."""
        return fee_rate

    def uncertainty_penalty(self, confidence: float, base_penalty: float = 0.01) -> float:
        """Penalty proportional to model uncertainty (1 - confidence)."""
        return base_penalty * (1.0 - confidence)

    def total_cost(
        self,
        spread: float,
        price: float,
        size: float,
        depth: float,
        confidence: float,
        fee_rate: float = 0.0,
    ) -> float:
        """Compute the total estimated execution cost."""
        return (
            self.spread_cost(spread, price)
            + self.slippage_cost(size, depth)
            + self.fee_cost(fee_rate)
            + self.uncertainty_penalty(confidence)
        )
