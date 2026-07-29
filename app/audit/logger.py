"""Structured audit event logging.

Every signal, decision, order, and risk event is recorded as a
structured JSON log for full audit trail traceability.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

REQUIRED_EVENTS = frozenset({
    "DATA_RECEIVED",
    "DATA_STALE",
    "SIGNAL_CREATED",
    "SIGNAL_REJECTED",
    "RISK_APPROVED",
    "ORDER_SUBMITTED",
    "ORDER_REJECTED",
    "ORDER_FILLED",
    "ORDER_CANCELLED",
    "POSITION_UPDATED",
    "CIRCUIT_BREAKER",
    "SYSTEM_START",
    "SYSTEM_STOP",
})


class AuditLogger:
    """Centralised structured audit event logger."""

    def log(self, event: str, **details: Any) -> None:
        """Record a structured audit event."""
        if event not in REQUIRED_EVENTS:
            logger.warning("Unknown audit event type: %s", event)
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": event,
            **details,
        }
        logger.info("AUDIT: %s", json.dumps(record, default=str))

    def data_received(self, market_id: str, **details: Any) -> None:
        self.log("DATA_RECEIVED", market_id=market_id, **details)

    def data_stale(self, market_id: str, age_seconds: float) -> None:
        self.log("DATA_STALE", market_id=market_id, age_seconds=age_seconds)

    def signal_created(self, market_id: str, strategy: str, **details: Any) -> None:
        self.log("SIGNAL_CREATED", market_id=market_id, strategy=strategy, **details)

    def signal_rejected(self, market_id: str, reason: str, **details: Any) -> None:
        self.log("SIGNAL_REJECTED", market_id=market_id, reason=reason, **details)

    def risk_approved(self, market_id: str, **details: Any) -> None:
        self.log("RISK_APPROVED", market_id=market_id, **details)

    def order_submitted(self, order_id: str, market_id: str, **details: Any) -> None:
        self.log("ORDER_SUBMITTED", order_id=order_id, market_id=market_id, **details)

    def order_filled(self, order_id: str, **details: Any) -> None:
        self.log("ORDER_FILLED", order_id=order_id, **details)

    def order_cancelled(self, order_id: str, **details: Any) -> None:
        self.log("ORDER_CANCELLED", order_id=order_id, **details)

    def circuit_breaker(self, reason: str, **details: Any) -> None:
        self.log("CIRCUIT_BREAKER", reason=reason, **details)

    def system_start(self) -> None:
        self.log("SYSTEM_START")

    def system_stop(self) -> None:
        self.log("SYSTEM_STOP")


audit_logger = AuditLogger()
