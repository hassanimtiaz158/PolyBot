"""Execution engine — orchestrates order lifecycle from risk decision to fill.

Flow
----
1. Receive a ``RiskDecision`` (approved signal from the risk engine).
2. Build an ``OrderRequest`` with market, side, size, and reference price.
3. Run through the ``OrderStateMachine``: CREATED → RISK_APPROVED → SUBMITTED.
4. Submit to the configured ``ExecutionAdapter`` (paper or live).
5. Monitor the fill status and persist to the database.
6. Return an ``OrderResult`` with fill details.

The engine is **adapter-agnostic** — it delegates all exchange interaction to
the ``ExecutionAdapter`` interface, so it works identically for paper trading
and live execution.

Execution health is tracked via :class:`ExecutionHealthCheck` so the
monitoring subsystem can detect adapter failures early.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.audit.events import EventBus, default_bus
from app.execution.interface import ExecutionAdapter
from app.execution.state_machine import OrderState, OrderStateMachine
from app.monitoring.health import checks as health_checks
from app.risk.engine import RiskDecision
from app.risk.kill_switch import KILL_SWITCH_REASON, KillSwitch

logger = logging.getLogger(__name__)


@dataclass
class OrderRequest:
    """Parameters for order submission, derived from a ``RiskDecision``.

    Parameters
    ----------
    market_id : str
        Target market ID.
    side : str
        ``"YES"`` or ``"NO"``.
    size : float
        Number of contracts to trade.
    price : float
        Reference price (midpoint / implied probability).
    signal_id : str
        Source signal for audit trailing.
    order_type : str
        ``"MARKET"`` (default) or ``"LIMIT"``.
    extra : dict
        Arbitrary metadata for downstream consumers.
    """

    market_id: str
    side: str
    size: float
    price: float
    signal_id: str = ""
    order_type: str = "MARKET"
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class OrderResult:
    """Outcome of an order execution attempt.

    Parameters
    ----------
    order_id : str
        Unique order identifier.
    market_id : str
        Target market ID.
    side : str
        ``"YES"`` or ``"NO"``.
    status : str
        Final order status (FILLED, PARTIALLY_FILLED, REJECTED, etc.).
    requested_size : float
        Original requested size.
    filled_size : float
        Contracts actually filled.
    average_fill : float | None
        Volume-weighted average fill price.
    state_machine : OrderStateMachine | None
        State machine instance for lifecycle tracking.
    error : str | None
        Error message if the order was rejected or failed.
    extra : dict
        Arbitrary metadata.
    """

    order_id: str
    market_id: str
    side: str
    status: str
    requested_size: float
    filled_size: float = 0.0
    average_fill: float | None = None
    state_machine: OrderStateMachine | None = None
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class ExecutionEngine:
    """Orchestrates the full order lifecycle from risk decision to fill.

    Tracks execution health via the ``execution`` health check so that
    consecutive adapter failures are surfaced to the monitoring subsystem.

    Parameters
    ----------
    adapter : ExecutionAdapter
        The underlying execution adapter (paper or live).
    kill_switch : KillSwitch | None
        Backend-controlled emergency kill switch.  When provided, every
        order is gated on it: a KILLED switch blocks submission with a
        REJECTED result before the adapter is ever called.
    event_bus : EventBus | None
        Structured audit bus.  Uses the module-level ``default_bus`` when
        ``None``.
    """

    def __init__(
        self,
        adapter: ExecutionAdapter,
        kill_switch: KillSwitch | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self._adapter = adapter
        self._kill_switch = kill_switch
        self._exec_health = health_checks.get("execution")
        self._bus = event_bus or default_bus

    async def execute(self, decision: RiskDecision) -> OrderResult:
        """Execute a risk-approved decision through the full order lifecycle.

        Parameters
        ----------
        decision : RiskDecision
            An approved risk decision with a non-zero size.

        Returns
        -------
        OrderResult
            Outcome of the execution attempt.
        """
        if not decision.approved:
            return OrderResult(
                order_id="",
                market_id=decision.market_id,
                side=decision.side,
                status="REJECTED",
                requested_size=0.0,
                error="Cannot execute unapproved risk decision",
            )

        if decision.size <= 0:
            return OrderResult(
                order_id="",
                market_id=decision.market_id,
                side=decision.side,
                status="REJECTED",
                requested_size=0.0,
                error="Decision has zero size",
            )

        # Emergency kill switch gate — blocks NEW orders while KILLED.
        # Checked before the state machine is built or any adapter is
        # touched, so a KILLED switch can never reach an adapter.
        if self._kill_switch is not None and await self._kill_switch.is_killed():
            return OrderResult(
                order_id="",
                market_id=decision.market_id,
                side=decision.side,
                status="REJECTED",
                requested_size=0.0,
                error=f"Kill switch active: {KILL_SWITCH_REASON}",
            )

        # Build the order request
        request = OrderRequest(
            market_id=decision.market_id,
            side=decision.side,
            size=decision.size,
            price=decision.risk_metrics.get("entry_price", 0.50),
            signal_id=decision.signal_id,
        )

        # Initialise state machine
        sm = OrderStateMachine(OrderState.CREATED)

        # CREATED → RISK_APPROVED
        sm.transition(OrderState.RISK_APPROVED)

        # Build raw payload for the adapter
        order_id = uuid.uuid4().hex[:12]
        payload = {
            "order_id": order_id,
            "market_id": request.market_id,
            "side": request.side,
            "size": request.size,
            "price": request.price,
            "signal_id": request.signal_id,
        }

        # RISK_APPROVED → SUBMITTED
        try:
            sm.transition(OrderState.SUBMITTED)
        except ValueError:
            return OrderResult(
                order_id=order_id,
                market_id=request.market_id,
                side=request.side,
                status="REJECTED",
                requested_size=request.size,
                state_machine=sm,
                error="Invalid state transition from RISK_APPROVED",
            )

        # Submit to adapter
        try:
            raw_result = await self._adapter.submit(payload)
            # Record success for execution health check
            if self._exec_health is not None:
                self._exec_health.record_success()
        except Exception as exc:
            logger.exception("Adapter submission failed for %s", order_id)
            # Record failure for execution health check
            if self._exec_health is not None:
                self._exec_health.record_error()
            return OrderResult(
                order_id=order_id,
                market_id=request.market_id,
                side=request.side,
                status="REJECTED",
                requested_size=request.size,
                state_machine=sm,
                error=f"Adapter submission failed: {exc}",
            )

        # Parse the adapter result
        adapter_status = raw_result.get("status", "REJECTED")
        filled_size = float(raw_result.get("filled_size", 0))
        average_fill = raw_result.get("average_fill")

        # Map adapter status to our state machine
        if adapter_status == "FILLED":
            try:
                sm.transition(OrderState.FILLED)
            except ValueError:
                pass
            status = "FILLED"
        elif adapter_status == "PARTIALLY_FILLED":
            try:
                sm.transition(OrderState.PARTIALLY_FILLED)
            except ValueError:
                pass
            status = "PARTIALLY_FILLED"
        else:
            try:
                sm.transition(OrderState.REJECTED)
            except ValueError:
                pass
            status = "REJECTED"

        return OrderResult(
            order_id=order_id,
            market_id=request.market_id,
            side=request.side,
            status=status,
            requested_size=request.size,
            filled_size=filled_size,
            average_fill=average_fill,
            state_machine=sm,
            error=raw_result.get("error") if status == "REJECTED" else None,
            extra={"raw_adapter_result": raw_result},
        )

    async def cancel_order(self, order_id: str, market_id: str = "") -> bool:
        """Cancel an order through the adapter.

        Parameters
        ----------
        order_id : str
            Order ID to cancel.
        market_id : str
            Market ID for audit trail (optional).

        Returns
        -------
        bool
            ``True`` if the cancellation was successful.
        """
        success = await self._adapter.cancel(order_id)
        if success:
            await self._bus.order_cancelled(order_id=order_id, market_id=market_id)
        return success

    async def order_status(self, order_id: str) -> dict[str, Any]:
        """Query the current status of an order.

        Parameters
        ----------
        order_id : str
            Order ID to query.

        Returns
        -------
        dict
            Status response from the adapter.
        """
        return await self._adapter.status(order_id)
