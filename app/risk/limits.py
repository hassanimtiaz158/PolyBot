"""Hard limit enforcement for positions, exposure, and daily loss."""

import logging

from app.config.settings import settings

logger = logging.getLogger(__name__)


class RiskLimits:
    """Evaluates hard risk limits against the current portfolio state."""

    def check_position_size(self, proposed_size: float, equity: float) -> bool:
        """Verify proposed position size does not exceed max position percentage."""
        return proposed_size <= equity * settings.max_position_pct

    def check_market_exposure(self, market_exposure: float, equity: float) -> bool:
        """Verify combined exposure to a single market is within limits."""
        return market_exposure <= equity * settings.max_market_exposure_pct

    def check_total_exposure(self, total_exposure: float, equity: float) -> bool:
        """Verify total portfolio exposure does not exceed the limit."""
        return total_exposure <= equity * settings.max_total_exposure_pct

    def check_daily_loss(self, daily_pnl: float, equity: float) -> bool:
        """Verify daily loss does not exceed the configured threshold."""
        return daily_pnl >= -(equity * settings.max_daily_loss_pct)

    def check_consecutive_losses(self, consecutive_losses: int) -> bool:
        """Verify consecutive losses are within the acceptable count."""
        return consecutive_losses < settings.max_consecutive_losses

    def check_open_positions(self, open_count: int) -> bool:
        """Verify the number of open positions is within limits."""
        return open_count < settings.max_open_positions

    def all_checks(
        self,
        proposed_size: float,
        equity: float,
        market_exposure: float,
        total_exposure: float,
        daily_pnl: float,
        consecutive_losses: int,
        open_positions: int,
    ) -> tuple[bool, list[str]]:
        """Run all limit checks and return (approved, reasons)."""
        reasons: list[str] = []
        if not self.check_position_size(proposed_size, equity):
            reasons.append("Position size exceeds max")
        if not self.check_market_exposure(market_exposure, equity):
            reasons.append("Market exposure exceeds max")
        if not self.check_total_exposure(total_exposure, equity):
            reasons.append("Total exposure exceeds max")
        if not self.check_daily_loss(daily_pnl, equity):
            reasons.append("Daily loss limit reached")
        if not self.check_consecutive_losses(consecutive_losses):
            reasons.append("Consecutive loss limit reached")
        if not self.check_open_positions(open_positions):
            reasons.append("Max open positions reached")
        return (len(reasons) == 0, reasons)
