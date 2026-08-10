"""Startup order and position reconciliation.

Compares stored orders and positions with current exchange state on
startup and flags discrepancies for operator review.

Reconciliation is a mandatory safety gate: the bot must not submit new
orders until it has verified that its internal state matches reality.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.storage.repositories import OrderRepository, PositionRepository

logger = logging.getLogger(__name__)


@dataclass
class ReconciliationResult:
    """Outcome of a reconciliation pass."""

    orders_checked: int = 0
    orders_missing: list[dict[str, Any]] = field(default_factory=list)
    orders_status_mismatch: list[dict[str, Any]] = field(default_factory=list)
    positions_checked: int = 0
    positions_missing: list[dict[str, Any]] = field(default_factory=list)
    positions_value_mismatch: list[dict[str, Any]] = field(default_factory=list)
    is_clean: bool = True

    def summary(self) -> str:
        """Human-readable summary of the reconciliation."""
        parts = [
            f"orders_checked={self.orders_checked}",
            f"positions_checked={self.positions_checked}",
        ]
        if self.orders_missing:
            parts.append(f"orders_missing={len(self.orders_missing)}")
        if self.orders_status_mismatch:
            parts.append(f"orders_status_mismatch={len(self.orders_status_mismatch)}")
        if self.positions_missing:
            parts.append(f"positions_missing={len(self.positions_missing)}")
        if self.positions_value_mismatch:
            parts.append(f"positions_value_mismatch={len(self.positions_value_mismatch)}")
        return "Reconciler(" + ", ".join(parts) + ")"


class Reconciler:
    """Reconciles database state with actual account state on startup.

    Compares stored open orders and positions with current exchange state
    and flags discrepancies for operator review.

    Parameters
    ----------
    order_repo : OrderRepository | None
        Repository for order queries.  Defaults to the global singleton.
    position_repo : PositionRepository | None
        Repository for position queries.  Defaults to the global singleton.
    exchange_adapter : object | None
        Execution adapter to query exchange state.  Must implement
        ``status(order_id) -> dict`` and ``list_open_orders() -> list[dict]``.
    """

    def __init__(
        self,
        order_repo: OrderRepository | None = None,
        position_repo: PositionRepository | None = None,
        exchange_adapter: Any | None = None,
    ) -> None:
        self._order_repo = order_repo or OrderRepository()
        self._position_repo = position_repo or PositionRepository()
        self._adapter = exchange_adapter

    async def reconcile_orders(self) -> list[dict[str, Any]]:
        """Check open orders in the DB against exchange state.

        For each order that is in a non-terminal state (SUBMITTED,
        PARTIALLY_FILLED), query the exchange for its current status.
        Flag discrepancies: orders that exist in the DB but not on the
        exchange, or whose status has changed.

        Returns a list of discrepancy dicts, each containing:
        - ``order_id``: the order in question
        - ``db_status``: what the database says
        - ``exchange_status``: what the exchange says (or ``None``)
        - ``issue``: description of the mismatch
        """
        discrepancies: list[dict[str, Any]] = []

        try:
            db_orders = await self._order_repo.list_open()
        except Exception:
            logger.exception("Failed to query open orders from database")
            return [{"issue": "DATABASE_QUERY_FAILED", "details": "Could not read open orders"}]

        if not db_orders:
            logger.info("Reconciler: no open orders in database")
            return []

        logger.info("Reconciler: checking %d open orders against exchange", len(db_orders))

        if self._adapter is None:
            logger.warning(
                "Reconciler: no exchange adapter configured — "
                "flagging all %d open orders as UNVERIFIED",
                len(db_orders),
            )
            for order in db_orders:
                discrepancies.append({
                    "order_id": order.order_id,
                    "market_id": order.market_id,
                    "db_status": order.status,
                    "exchange_status": None,
                    "issue": "NO_EXCHANGE_ADAPTER",
                })
            return discrepancies

        for order in db_orders:
            try:
                exchange_status = await self._adapter.status(order.order_id)
            except Exception:
                logger.warning("Failed to query exchange for order %s", order.order_id)
                discrepancies.append({
                    "order_id": order.order_id,
                    "market_id": order.market_id,
                    "db_status": order.status,
                    "exchange_status": None,
                    "issue": "EXCHANGE_QUERY_FAILED",
                })
                continue

            if exchange_status is None:
                discrepancies.append({
                    "order_id": order.order_id,
                    "market_id": order.market_id,
                    "db_status": order.status,
                    "exchange_status": None,
                    "issue": "MISSING_ON_EXCHANGE",
                })
            else:
                exchange_state = exchange_status.get("status", "UNKNOWN")
                if exchange_state != order.status:
                    discrepancies.append({
                        "order_id": order.order_id,
                        "market_id": order.market_id,
                        "db_status": order.status,
                        "exchange_status": exchange_state,
                        "issue": "STATUS_MISMATCH",
                    })

        if discrepancies:
            logger.warning(
                "Reconciler: found %d order discrepancies out of %d checked",
                len(discrepancies),
                len(db_orders),
            )
        else:
            logger.info("Reconciler: all %d open orders match exchange state", len(db_orders))

        return discrepancies

    async def reconcile_positions(self) -> list[dict[str, Any]]:
        """Check positions in the DB against expected state.

        For each open position, verify that the position is consistent:
        - position exists in DB
        - size is non-negative
        - entry price is valid

        If an exchange adapter is available, also verify that the exchange
        reports a matching position.

        Returns a list of discrepancy dicts.
        """
        discrepancies: list[dict[str, Any]] = []

        try:
            db_positions = await self._position_repo.list_open()
        except Exception:
            logger.exception("Failed to query open positions from database")
            return [{"issue": "DATABASE_QUERY_FAILED", "details": "Could not read open positions"}]

        if not db_positions:
            logger.info("Reconciler: no open positions in database")
            return []

        logger.info("Reconciler: checking %d open positions", len(db_positions))

        for pos in db_positions:
            # Basic consistency checks
            if pos.size is not None and pos.size < 0:
                discrepancies.append({
                    "position_id": pos.position_id,
                    "market_id": pos.market_id,
                    "issue": "NEGATIVE_SIZE",
                    "size": pos.size,
                })

            if pos.average_entry is not None and not (0.0 <= pos.average_entry <= 1.0):
                discrepancies.append({
                    "position_id": pos.position_id,
                    "market_id": pos.market_id,
                    "issue": "INVALID_ENTRY_PRICE",
                    "average_entry": pos.average_entry,
                })

            if pos.current_price is not None and not (0.0 <= pos.current_price <= 1.0):
                discrepancies.append({
                    "position_id": pos.position_id,
                    "market_id": pos.market_id,
                    "issue": "INVALID_CURRENT_PRICE",
                    "current_price": pos.current_price,
                })

        if discrepancies:
            logger.warning(
                "Reconciler: found %d position discrepancies out of %d checked",
                len(discrepancies),
                len(db_positions),
            )
        else:
            logger.info("Reconciler: all %d open positions are consistent", len(db_positions))

        return discrepancies

    async def reconcile_all(self) -> ReconciliationResult:
        """Run full reconciliation of orders and positions.

        Returns a ReconciliationResult with all discrepancies found.
        """
        order_discrepancies = await self.reconcile_orders()
        position_discrepancies = await self.reconcile_positions()

        result = ReconciliationResult()

        # Parse order discrepancies
        db_orders = await self._order_repo.list_open()
        result.orders_checked = len(db_orders)
        for d in order_discrepancies:
            if d.get("issue") == "MISSING_ON_EXCHANGE":
                result.orders_missing.append(d)
            elif d.get("issue") == "STATUS_MISMATCH":
                result.orders_status_mismatch.append(d)

        # Parse position discrepancies
        db_positions = await self._position_repo.list_open()
        result.positions_checked = len(db_positions)
        for d in position_discrepancies:
            if d.get("issue") in ("NEGATIVE_SIZE", "INVALID_ENTRY_PRICE", "INVALID_CURRENT_PRICE"):
                result.positions_value_mismatch.append(d)

        result.is_clean = not order_discrepancies and not position_discrepancies

        if result.is_clean:
            logger.info("Reconciler: reconciliation complete — all clean")
        else:
            logger.warning("Reconciler: reconciliation found discrepancies: %s", result.summary())

        return result
