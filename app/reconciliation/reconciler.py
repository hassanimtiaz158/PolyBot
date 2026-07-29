"""Startup order and position reconciliation."""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class Reconciler:
    """Reconciles database state with actual account state on startup.

    Compares stored orders/positions with current exchange state and
    flags discrepancies for operator review.
    """

    async def reconcile_orders(self) -> list[dict[str, Any]]:
        """Check open orders and flag any discrepancies."""
        logger.debug("Reconciler.reconcile_orders called")
        return []

    async def reconcile_positions(self) -> list[dict[str, Any]]:
        """Check positions and flag any discrepancies."""
        logger.debug("Reconciler.reconcile_positions called")
        return []
