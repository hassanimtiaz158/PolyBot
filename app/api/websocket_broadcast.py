"""Real-time dashboard event broadcaster.

Watches the persisted database (read-only) and fan-outs WebSocket events
to subscribed dashboard clients at ``/ws/dashboard``.

Design notes
------------
* **Prefer push over polling.** Clients connect once and receive typed
  events only when a section's data actually changes.  The server runs a
  lightweight change-detector that probes the database every
  ``settings.dashboard_poll_interval_seconds`` and publishes a section's
  payload only when its change token differs from the previous one.
* **Never affects trading.** The detector only *reads* the database and
  health state.  It cannot modify signals, orders, positions, risk state,
  or the circuit breaker.
* **No secrets.** Payloads are the same read-only projections served by
  the REST dashboard endpoints; nothing secret is serialised.

Event types pushed to clients:
``MARKET_UPDATE``, ``SIGNAL_UPDATE``, ``POSITION_UPDATE``,
``ORDER_UPDATE``, ``P&L_UPDATE``, ``RISK_UPDATE``, ``HEALTH_UPDATE``,
``CIRCUIT_BREAKER``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

from app.api import dashboard_service
from app.api.models import (
    MarketResponse,
    OrderResponse,
    SignalResponse,
)
from app.config.settings import settings
from app.storage.db import Database
from app.storage.repositories import (
    MarketRepository,
    OrderRepository,
    PositionRepository,
    SignalRepository,
)

logger = logging.getLogger(__name__)

# Event type names (wire format).
MARKET_UPDATE = "MARKET_UPDATE"
SIGNAL_UPDATE = "SIGNAL_UPDATE"
POSITION_UPDATE = "POSITION_UPDATE"
ORDER_UPDATE = "ORDER_UPDATE"
PNL_UPDATE = "P&L_UPDATE"
RISK_UPDATE = "RISK_UPDATE"
HEALTH_UPDATE = "HEALTH_UPDATE"
CIRCUIT_BREAKER = "CIRCUIT_BREAKER"


def _now() -> str:
    return datetime.now(UTC).isoformat()


class DashboardBroadcaster:
    """Change detector + pub/sub fan-out for dashboard WebSocket events."""

    def __init__(
        self,
        interval: float | None = None,
        database: Database | None = None,
    ) -> None:
        self.database = database
        self.interval = (
            interval if interval is not None else float(settings.dashboard_poll_interval_seconds)
        )
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._tokens: dict[str, Any] = {}
        self._task: asyncio.Task[None] | None = None
        self._running = False

    # ── pub / sub ───────────────────────────────────────────────────

    async def subscribe(self) -> asyncio.Queue[str]:
        """Register a subscriber and return its event queue."""
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=200)
        self._subscribers.add(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        """Remove a subscriber queue from the broadcast set."""
        self._subscribers.discard(queue)

    async def publish(self, event_type: str, data: dict[str, Any] | None) -> None:
        """Broadcast a JSON-encoded event to every subscriber."""
        message = json.dumps(
            {
                "type": event_type,
                "timestamp": _now(),
                "data": data,
            },
            default=str,
        )
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # Feed is moving faster than the consumer — drop the
                # oldest event rather than block or grow unboundedly.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait(message)
                except asyncio.QueueFull:
                    pass

    # ── change-detection loop ───────────────────────────────────────

    def start(self) -> None:
        """Start the background change-detection task (idempotent)."""
        if self._task is None and self.database is not None:
            self._running = True
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop and await the change-detection task."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.emit_changes(self.database)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("dashboard event poll failed")
            await asyncio.sleep(self.interval)

    async def emit_changes(self, db: Database | None) -> None:
        """Publish an event for every section whose data changed."""
        if db is None:
            return
        collectors: tuple[tuple[str, Any], ...] = (
            ("market", self._collect_market),
            ("signal", self._collect_signals),
            ("position", self._collect_positions),
            ("order", self._collect_orders),
            ("pnl", self._collect_pnl),
            ("risk", self._collect_risk),
            ("health", self._collect_health),
            ("circuit_breaker", self._collect_circuit_breaker),
        )
        for key, collect in collectors:
            try:
                token, event_type, payload = await collect(db)
            except Exception:
                logger.exception("dashboard event collector %s failed", key)
                continue
            if self._tokens.get(key) == token:
                continue
            self._tokens[key] = token
            await self.publish(event_type, payload)

    # ── collectors (each returns (token, event_type, payload)) ─────

    async def _collect_market(self, db: Database) -> tuple[Any, str, dict[str, Any]]:
        repo = MarketRepository(db)
        rows = await repo.list_all()
        updated = max((m.updated_at or "" for m in rows), default="")
        token = (len(rows), updated)
        payload: dict[str, Any] = {
            "items": [MarketResponse.model_validate(m).model_dump() for m in rows],
            "total": len(rows),
        }
        return token, MARKET_UPDATE, payload

    async def _collect_signals(self, db: Database) -> tuple[Any, str, dict[str, Any]]:
        repo = SignalRepository(db)
        rows, total = await repo.list_paginated(limit=50, offset=0)
        latest = max((s.timestamp or "" for s in rows), default="")
        token = (total, latest)
        payload: dict[str, Any] = {
            "items": [SignalResponse.model_validate(s).model_dump() for s in rows],
            "total": total,
        }
        return token, SIGNAL_UPDATE, payload

    async def _collect_positions(self, db: Database) -> tuple[Any, str, dict[str, Any]]:
        repo = PositionRepository(db)
        rows = await repo.list_open()
        pnl = await repo.pnl_summary()
        items, total = await dashboard_service.build_positions(
            db, limit=100, offset=0, open_only=True
        )
        token = (
            len(rows),
            round(float(pnl["total_realised_pnl"]), 6),
            round(float(pnl["total_unrealised_pnl"]), 6),
        )
        payload: dict[str, Any] = {
            "items": [r.model_dump() for r in items],
            "total": total,
        }
        return token, POSITION_UPDATE, payload

    async def _collect_orders(self, db: Database) -> tuple[Any, str, dict[str, Any]]:
        repo = OrderRepository(db)
        rows, total = await repo.list_paginated(limit=50, offset=0)
        latest = max((o.submitted_at or "" for o in rows), default="")
        filled = await repo.count_filled()
        token = (total, latest, filled)
        payload: dict[str, Any] = {
            "items": [OrderResponse.model_validate(o).model_dump() for o in rows],
            "total": total,
        }
        return token, ORDER_UPDATE, payload

    async def _collect_pnl(self, db: Database) -> tuple[Any, str, dict[str, Any]]:
        overview = await dashboard_service.build_overview(db)
        breaker_state = overview.circuit_breaker.state if overview.circuit_breaker else None
        token = (
            round(overview.realized_pnl, 6),
            round(overview.unrealized_pnl, 6),
            round(overview.today_pnl, 6),
            round(overview.total_exposure, 6),
            overview.open_positions,
            overview.active_signals,
            overview.bot_mode,
            breaker_state,
        )
        return token, PNL_UPDATE, overview.model_dump()

    async def _collect_risk(self, db: Database) -> tuple[Any, str, dict[str, Any]]:
        risk = await dashboard_service.build_risk(db)
        breaker = risk.circuit_breaker
        token = (
            round(risk.account_balance, 6),
            round(risk.available_balance, 6),
            round(risk.today_pnl, 6),
            round(risk.daily_loss, 6),
            round(risk.exposure, 6),
            round(risk.exposure_pct, 6),
            round(risk.exposure_limit, 6),
            risk.consecutive_losses,
            risk.open_positions,
            round(risk.largest_position, 6),
            round(risk.largest_market_exposure, 6),
            round(risk.average_spread or 0.0, 6),
            round(risk.minimum_liquidity or 0.0, 6),
            risk.spread_status,
            risk.liquidity_status,
            risk.data_freshness,
            breaker.state if breaker else None,
            tuple(breaker.reasons) if breaker else (),
        )
        return token, RISK_UPDATE, risk.model_dump()

    async def _collect_health(self, db: Database) -> tuple[Any, str, dict[str, Any]]:
        health = await dashboard_service.build_health(db)
        token = (
            health.healthy,
            tuple(sorted((name, ch.healthy) for name, ch in health.checks.items())),
        )
        return token, HEALTH_UPDATE, health.model_dump()

    async def _collect_circuit_breaker(self, db: Database) -> tuple[Any, str, dict[str, Any]]:
        breaker = await dashboard_service.circuit_breaker(db)
        if breaker is None:
            token: tuple[Any, ...] = ("none",)
            payload: dict[str, Any] = {
                "state": None,
                "reasons": [],
                "triggered_at": None,
            }
        else:
            token = (
                breaker.state,
                tuple(breaker.reasons),
                breaker.triggered_at,
            )
            payload = {
                "state": breaker.state,
                "reasons": list(breaker.reasons),
                "triggered_at": breaker.triggered_at,
            }
        return token, CIRCUIT_BREAKER, payload
