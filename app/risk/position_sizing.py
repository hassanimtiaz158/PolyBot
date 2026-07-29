"""Position sizing: fixed-risk and fractional-Kelly methods."""

import logging

from app.config.settings import settings

logger = logging.getLogger(__name__)


class PositionSizer:
    """Determines position sizes based on risk budget and hard limits."""

    def fixed_risk_size(self, equity: float, confidence: float) -> float:
        """Calculate position size using fixed-risk percentage of equity."""
        return equity * settings.max_position_pct * confidence

    def fractional_kelly(
        self, edge: float, odds: float, fraction: float = 0.25
    ) -> float:
        """Calculate position size using fractional Kelly criterion."""
        if odds <= 0:
            return 0.0
        kelly = edge / odds
        return max(0.0, kelly * fraction)

    def determine_size(
        self,
        equity: float,
        confidence: float,
        net_edge: float,
        liquidity: float,
        risk_limit: float,
    ) -> float:
        """Determine final position size as the minimum of all constraints."""
        fixed = self.fixed_risk_size(equity, confidence)
        return min(fixed, liquidity, risk_limit)
