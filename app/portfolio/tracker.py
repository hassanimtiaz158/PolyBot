"""Portfolio tracking and P&L calculation."""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class PortfolioTracker:
    """Tracks positions, equity, and P&L across all markets."""

    def __init__(self) -> None:
        self._equity: float = 10000.0
        self._positions: dict[str, dict[str, Any]] = {}

    @property
    def equity(self) -> float:
        return self._equity

    def update_position(self, position: dict[str, Any]) -> None:
        """Update or add a position."""
        self._positions[position["market_id"]] = position

    def total_exposure(self) -> float:
        """Calculate the sum of all position sizes."""
        return sum(float(p.get("size", 0)) for p in self._positions.values())

    def unrealised_pnl(self) -> float:
        """Calculate total unrealised P&L across all positions."""
        return sum(float(p.get("unrealised_pnl", 0)) for p in self._positions.values())

    def realised_pnl(self) -> float:
        """Calculate total realised P&L."""
        return sum(float(p.get("realised_pnl", 0)) for p in self._positions.values())

    def summary(self) -> dict[str, Any]:
        """Return a comprehensive portfolio summary."""
        return {
            "equity": self._equity,
            "total_exposure": self.total_exposure(),
            "unrealised_pnl": self.unrealised_pnl(),
            "realised_pnl": self.realised_pnl(),
            "open_positions": len(self._positions),
            "positions": self._positions,
        }
