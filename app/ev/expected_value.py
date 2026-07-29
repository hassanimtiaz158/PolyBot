"""Expected value calculation: gross edge to net edge."""

import logging

from app.config.settings import settings
from app.ev.costs import CostEstimator

logger = logging.getLogger(__name__)


class ExpectedValueEngine:
    """Calculates gross and net expected value for a potential trade."""

    def __init__(self) -> None:
        self._cost_estimator = CostEstimator()

    def gross_edge(self, model_probability: float, implied_probability: float) -> float:
        """Calculate gross edge as the difference between model and market probabilities."""
        return model_probability - implied_probability

    def net_edge(
        self,
        model_probability: float,
        implied_probability: float,
        spread: float,
        price: float,
        size: float,
        depth: float,
        confidence: float,
        fee_rate: float = 0.0,
    ) -> float:
        """Calculate net edge after all execution costs and uncertainty penalty."""
        gross = self.gross_edge(model_probability, implied_probability)
        costs = self._cost_estimator.total_cost(spread, price, size, depth, confidence, fee_rate)
        return gross - costs

    def is_acceptable(self, net_edge: float) -> bool:
        """Check if net edge exceeds the minimum threshold."""
        return net_edge >= settings.min_net_edge
