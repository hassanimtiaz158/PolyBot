"""Structured audit events — the single, typed event stream for the bot.

Every meaningful system occurrence — data ingestion, signals, risk
decisions, order lifecycle, portfolio changes, circuit breaker trips and
process lifecycle — is emitted as a structured :class:`AuditEvent`
carrying:

* ``timestamp`` — ISO-8601 UTC time of the event.
* ``event_type`` — one of the canonical ``EVENT_TYPES``.
* ``market_id`` / ``strategy`` / ``decision`` / ``reason`` — context
  fields that apply to the event (``None`` otherwise).
* additional ``values`` — relevant numeric/enumerated detail.

Events are: (1) recorded as structured JSON log lines, (2) persisted to
the ``risk_events`` audit table when a repository is available, and
(3) forwarded to the optional alert dispatcher when the event matches an
alert rule.  Persistence is best-effort: the audit log and alerts work
even when the database is unavailable.

The bus never records credentials, API keys, or private keys; callers
must only pass operational values.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.audit.logger import audit_logger
from app.monitoring.alerts import AlertDispatcher
from app.storage.models import RiskEvent
from app.storage.repositories import RiskEventRepository

logger = logging.getLogger(__name__)

# ── Canonical event catalogue ────────────────────────────────────────

EVENT_TYPES = frozenset({
    "DATA_RECEIVED",
    "DATA_STALE",
    "SIGNAL_CREATED",
    "SIGNAL_REJECTED",
    "RISK_APPROVED",
    "RISK_REJECTED",
    "ORDER_SUBMITTED",
    "ORDER_REJECTED",
    "ORDER_FILLED",
    "ORDER_CANCELLED",
    "POSITION_UPDATED",
    "CIRCUIT_BREAKER",
    "SYSTEM_START",
    "SYSTEM_STOP",
})

SEVERITIES: dict[str, str] = {
    "DATA_RECEIVED": "INFO",
    "DATA_STALE": "WARNING",
    "SIGNAL_CREATED": "INFO",
    "SIGNAL_REJECTED": "INFO",
    "RISK_APPROVED": "INFO",
    "RISK_REJECTED": "WARNING",
    "ORDER_SUBMITTED": "INFO",
    "ORDER_REJECTED": "WARNING",
    "ORDER_FILLED": "INFO",
    "ORDER_CANCELLED": "INFO",
    "POSITION_UPDATED": "INFO",
    "CIRCUIT_BREAKER": "CRITICAL",
    "SYSTEM_START": "INFO",
    "SYSTEM_STOP": "INFO",
}

# Event types that raise an operator alert when the dispatcher is enabled.
ALERTS: dict[str, tuple[str, str, str]] = {
    # event type → (alert key, subject, message)
    "CIRCUIT_BREAKER": (
        "circuit_breaker",
        "Circuit breaker",
        "Circuit breaker tripped: {reason}",
    ),
    "RISK_REJECTED": (
        "risk_rejected",
        "Risk rejection",
        "Trade rejected by risk engine: {reason}",
    ),
    "ORDER_REJECTED": (
        "order_rejected",
        "Order rejected",
        "Order {order_id} rejected: {reason}",
    ),
    "DATA_STALE": (
        "stale_data",
        "Stale market data",
        "Market data is stale ({reason})",
    ),
}

# A risk rejection whose reason indicates a loss or concentration breach
# that the operator must be told about immediately.
_HARD_REJECTIONS = frozenset(
    {"DAILY_LOSS_LIMIT_REACHED", "CONSECUTIVE_LOSS_LIMIT_REACHED", "SYSTEM_HALTED"}
)


@dataclass
class AuditEvent:
    """A single structured audit event."""

    event_type: str
    timestamp: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    market_id: str | None = None
    strategy: str | None = None
    decision: str | None = None
    reason: str | None = None
    severity: str = "INFO"
    values: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def to_dict(self) -> dict[str, Any]:
        """Flat dictionary projection (JSON-serialisable)."""
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "severity": self.severity,
            "market_id": self.market_id,
            "strategy": self.strategy,
            "decision": self.decision,
            "reason": self.reason,
            **self.values,
        }

    def as_risk_event(self) -> RiskEvent:
        """Project onto the persisted ``RiskEvent`` storage model."""
        return RiskEvent(
            event_id=self.event_id,
            event_type=self.event_type,
            severity=self.severity,
            details=json.dumps(self.values, default=str) if self.values else None,
            timestamp=self.timestamp,
            market_id=self.market_id,
            strategy=self.strategy,
            decision=self.decision,
            reason=self.reason,
        )


class EventBus:
    """Records and routes structured audit events.

    Parameters
    ----------
    repo : RiskEventRepository | None
        Persistence target.  When ``None`` the persisted ``risk_event_repo``
        is used if available; persistence failures are logged and swallowed.
    notifier : AlertDispatcher | None
        Optional alert dispatcher.  When ``None`` a dispatcher is built
        from settings (which are disabled by default).
    persist : bool
        When False, never touches the database (used in unit tests).
    """

    def __init__(
        self,
        repo: RiskEventRepository | None = None,
        notifier: AlertDispatcher | None = None,
        persist: bool = True,
    ) -> None:
        self._repo = repo
        self._persist_enabled = persist and repo is not None
        self._notifier = notifier or AlertDispatcher.from_settings()

    async def emit(
        self,
        event_type: str,
        *,
        market_id: str | None = None,
        strategy: str | None = None,
        decision: str | None = None,
        reason: str | None = None,
        severity: str | None = None,
        **values: Any,
    ) -> AuditEvent:
        """Build, record, persist, and optionally alert on an event."""
        if event_type not in EVENT_TYPES:
            logger.warning("Unknown audit event type: %s", event_type)
            severity = severity or "INFO"
        else:
            severity = severity or SEVERITIES[event_type]

        event = AuditEvent(
            event_type=event_type,
            market_id=market_id,
            strategy=strategy,
            decision=decision,
            reason=reason,
            severity=severity,
            values=dict(values),
        )

        # 1. Structured log line (always).
        audit_logger.log(
            event.event_type,
            event_id=event.event_id,
            severity=event.severity,
            market_id=event.market_id,
            strategy=event.strategy,
            decision=event.decision,
            reason=event.reason,
            **event.values,
        )

        # 2. Best-effort persistence.
        if self._persist_enabled:
            try:
                await self._repo.insert(event.as_risk_event())  # type: ignore[union-attr]
            except Exception:
                logger.exception(
                    "Failed to persist audit event %s", event.event_id
                )

        # 3. Optional alerts.
        await self._alert(event)

        return event

    # ── Alert routing ─────────────────────────────────────────────

    async def _alert(self, event: AuditEvent) -> None:
        spec = ALERTS.get(event.event_type)
        if spec is None:
            return
        key, subject, template = spec
        message = template.format(
            market=event.market_id, reason=event.reason or "unknown", **event.values
        )
        await self._notifier.send(
            title=subject,
            message=message,
            level=event.severity,
            key=key,
            context=event.to_dict(),
        )

        if (
            event.event_type == "RISK_REJECTED"
            and (event.reason or "").upper() in _HARD_REJECTIONS
        ):
            await self._notifier.send(
                title="Daily loss limit breach",
                message=(
                    f"Risk engine halted trading: "
                    f"{event.reason} for {event.market_id or 'unknown market'}"
                ),
                level="CRITICAL",
                key="daily_loss_limit",
                context=event.to_dict(),
            )

    # ── Convenience emit helpers ─────────────────────────────────────

    async def data_received(
        self,
        market_id: str | None = None,
        fresh_seconds: float | None = None,
    ) -> AuditEvent:
        kwargs: dict[str, Any] = {}
        if fresh_seconds is not None:
            kwargs["fresh_seconds"] = fresh_seconds
        return await self.emit(
            "DATA_RECEIVED",
            market_id=market_id,
            **kwargs,
        )

    async def data_stale(
        self, age_seconds: float, market_id: str | None = None
    ) -> AuditEvent:
        return await self.emit(
            "DATA_STALE",
            market_id=market_id,
            reason=f"data age {age_seconds:.1f}s",
            age_seconds=age_seconds,
        )

    async def signal_created(
        self,
        market_id: str,
        strategy: str,
        *,
        side: str,
        edge: float | None = None,
        confidence: float | None = None,
    ) -> AuditEvent:
        kwargs: dict[str, Any] = {"side": side}
        if edge is not None:
            kwargs["edge"] = edge
        if confidence is not None:
            kwargs["confidence"] = confidence
        return await self.emit(
            "SIGNAL_CREATED",
            market_id=market_id,
            strategy=strategy,
            decision="CANDIDATE",
            **kwargs,
        )

    async def signal_rejected(
        self, market_id: str, strategy: str, reason: str
    ) -> AuditEvent:
        return await self.emit(
            "SIGNAL_REJECTED",
            market_id=market_id,
            strategy=strategy,
            decision="NO_SIGNAL",
            reason=reason,
        )

    async def risk_decision(self, decision: Any, approved: bool) -> AuditEvent:
        """Emit a ``RISK_APPROVED`` / ``RISK_REJECTED`` event from a decision."""
        if approved:
            return await self.emit(
                "RISK_APPROVED",
                market_id=getattr(decision, "market_id", None),
                decision="APPROVED",
                size=getattr(decision, "size", None),
            )
        return await self.emit(
            "RISK_REJECTED",
            market_id=getattr(decision, "market_id", None),
            decision="REJECTED",
            reason=getattr(decision, "reason", "REJECTED"),
        )

    async def order_submitted(
        self, order_id: str, market_id: str, *, side: str, size: float
    ) -> AuditEvent:
        return await self.emit(
            "ORDER_SUBMITTED",
            market_id=market_id,
            order_id=order_id,
            side=side,
            size=size,
        )

    async def order_rejected(
        self, order_id: str, market_id: str, reason: str, *, side: str = ""
    ) -> AuditEvent:
        return await self.emit(
            "ORDER_REJECTED",
            market_id=market_id,
            order_id=order_id,
            side=side,
            reason=reason,
        )

    async def order_filled(self, order_id: str, market_id: str, **values: Any) -> AuditEvent:
        return await self.emit(
            "ORDER_FILLED", market_id=market_id, order_id=order_id, **values
        )

    async def order_cancelled(self, order_id: str, market_id: str) -> AuditEvent:
        return await self.emit(
            "ORDER_CANCELLED", market_id=market_id, order_id=order_id
        )

    async def position_updated(
        self,
        market_id: str,
        *,
        side: str = "",
        size: float = 0.0,
        average_entry: float = 0.0,
        unrealised_pnl: float = 0.0,
    ) -> AuditEvent:
        return await self.emit(
            "POSITION_UPDATED",
            market_id=market_id,
            side=side,
            size=size,
            average_entry=average_entry,
            unrealised_pnl=unrealised_pnl,
        )


def event_from_dict(data: dict[str, Any]) -> AuditEvent:
    """Reconstruct an :class:`AuditEvent` from a stored row."""
    return AuditEvent(
        event_type=data["event_type"],
        timestamp=data.get("timestamp") or datetime.now(UTC).isoformat(),
        market_id=data.get("market_id"),
        strategy=data.get("strategy"),
        decision=data.get("decision"),
        reason=data.get("reason"),
        severity=data.get("severity", "INFO"),
        event_id=data.get("event_id", ""),
        values=json.loads(data["details"]) if data.get("details") else {},
    )


default_bus = EventBus(persist=False)
"""Process-wide default bus.  The application wires it with a repository
and alert dispatcher at startup; tests replace it with fakes."""
