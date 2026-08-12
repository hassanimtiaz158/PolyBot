"""Backend-controlled emergency kill switch.

Two states:

* ``ACTIVE`` — normal operation; new orders may be submitted.
* ``KILLED`` — an operator has engaged the switch; **no new orders**
  are permitted.  Existing positions remain visible and the bot keeps
  running (monitoring, reconciliation, health checks) — only new order
  submission is blocked.

State is persisted to SQLite (the ``circuit_breaker_state`` key-value
table) so it survives a process restart.  Unlike the circuit breaker,
the kill switch is never re-armed automatically: after a restart the
persisted ``KILLED`` state is loaded and trading stays stopped until an
operator explicitly resumes with confirmation.

Every state change is emitted as a structured audit event via the
:class:`EventBus` (``KILL_SWITCH`` / ``KILL_SWITCH_RESUMED``) with the
operator identity recorded.  Only the backend control endpoints
(``/api/control/*``) may change this state — the frontend is read-only.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from app.audit.events import EventBus
from app.storage.db import Database
from app.storage.db import db as _db

logger = logging.getLogger(__name__)

#: Key under which the kill switch state is stored in the KV table.
_STATE_KEY = "kill_switch"

#: Canonical reason recorded on every KILLED transition.
KILL_SWITCH_REASON = "MANUAL EMERGENCY STOP"


class KillSwitchState(StrEnum):
    """Two-state emergency kill switch."""

    ACTIVE = "ACTIVE"
    KILLED = "KILLED"


class KillSwitchResumeError(Exception):
    """Raised when a resume is attempted without explicit confirmation."""


class KillSwitch:
    """Persistent, backend-controlled emergency kill switch.

    Parameters
    ----------
    db : Database | None
        Database for persistence.  Defaults to the global ``db``.
    event_bus : EventBus | None
        Structured audit bus; ``None`` disables audit emission.
    persist : bool
        Whether to persist state to the database (default True).  Set to
        ``False`` in unit tests to avoid database dependencies.
    """

    def __init__(
        self,
        db: Database | None = None,
        event_bus: EventBus | None = None,
        persist: bool = True,
    ) -> None:
        self._db = db or _db
        self._persist_enabled = persist
        self._event_bus = event_bus
        self._state: KillSwitchState = KillSwitchState.ACTIVE
        self._killed_at: str | None = None
        self._killed_by: str | None = None
        self._reason: str | None = None

    # ── Properties ─────────────────────────────────────────────────────

    @property
    def state(self) -> KillSwitchState:
        """Current kill switch state (in-memory snapshot)."""
        return self._state

    @property
    def killed_at(self) -> str | None:
        """ISO-8601 timestamp of the last KILL transition."""
        return self._killed_at

    @property
    def killed_by(self) -> str | None:
        """Operator who engaged the switch (from the control request)."""
        return self._killed_by

    @property
    def reason(self) -> str | None:
        """Human-readable reason for the stop (KILLED only)."""
        return self._reason

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable projection of the current state."""
        return {
            "state": self._state.value,
            "reason": self._reason if self._state == KillSwitchState.KILLED else None,
            "killed_at": self._killed_at,
            "killed_by": self._killed_by,
        }

    # ── Public API ─────────────────────────────────────────────────────

    async def is_killed(self) -> bool:
        """Whether new orders are currently blocked.

        Re-reads the persisted state on every call so that a kill
        switch engaged by a different process (e.g. the API backend
        writing to the shared SQLite database) is honoured immediately.
        """
        await self.load_state()
        return self._state == KillSwitchState.KILLED

    async def kill(self, operator: str | None = None) -> KillSwitchState:
        """Engage the kill switch.  Idempotent; emits a ``KILL_SWITCH`` event."""
        if self._state == KillSwitchState.KILLED:
            return self._state
        previous = self._state
        self._state = KillSwitchState.KILLED
        self._killed_at = datetime.now(UTC).isoformat()
        self._killed_by = operator
        self._reason = KILL_SWITCH_REASON
        logger.warning("Kill switch engaged (by=%s)", operator)
        await self._emit(
            "KILL_SWITCH", previous, severity="CRITICAL", operator=operator
        )
        await self._persist()
        return self._state

    async def resume(
        self, operator: str | None = None, confirm: bool = False
    ) -> KillSwitchState:
        """Resume trading from a stop.

        Requires explicit operator confirmation (``confirm=True``);
        otherwise raises :class:`KillSwitchResumeError` and leaves the
        switch in the KILLED state.  A switch that is already ACTIVE is
        returned unchanged (idempotent).
        """
        if not confirm:
            raise KillSwitchResumeError(
                "Resume requires explicit confirmation (confirm=true)"
            )
        if self._state == KillSwitchState.ACTIVE:
            return self._state
        previous = self._state
        previous_reason = self._reason
        self._state = KillSwitchState.ACTIVE
        self._killed_at = None
        self._killed_by = None
        self._reason = None
        logger.info("Kill switch resumed (by=%s)", operator)
        await self._emit(
            "KILL_SWITCH_RESUMED",
            previous,
            severity="INFO",
            operator=operator,
            reason=previous_reason,
        )
        await self._persist()
        return self._state

    async def load_state(self) -> None:
        """Load persisted state from the database (best-effort).

        When the database is unavailable (or persistence is disabled)
        the current in-memory state is kept, so a fresh process starts
        ACTIVE and a running process never regresses to trading during
        a DB outage — the fail-closed default is a DB failure, which the
        risk engine already handles.
        """
        if not self._persist_enabled:
            return
        try:
            cursor = await self._db.conn.execute(
                "SELECT value FROM circuit_breaker_state WHERE key = ?",
                (_STATE_KEY,),
            )
            row = await cursor.fetchone()
        except Exception:
            logger.debug("Kill switch state unavailable — keeping in-memory state")
            return
        if row is None:
            return
        try:
            data = json.loads(row["value"])
            self._state = KillSwitchState(data.get("state", "ACTIVE"))
            self._killed_at = data.get("killed_at")
            self._killed_by = data.get("killed_by")
            self._reason = data.get("reason")
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.warning("Corrupt kill switch state — defaulting to ACTIVE")
            self._state = KillSwitchState.ACTIVE
            self._killed_at = None
            self._killed_by = None
            self._reason = None
        if self._state == KillSwitchState.KILLED:
            logger.warning(
                "Loaded kill switch state: KILLED (%s) — trading remains stopped",
                self._reason or KILL_SWITCH_REASON,
            )

    # ── Persistence ─────────────────────────────────────────────────────

    async def _persist(self) -> None:
        if not self._persist_enabled:
            return
        try:
            state = json.dumps(self.to_dict())
            now = datetime.now(UTC).isoformat()
            await self._db.conn.execute(
                "INSERT OR REPLACE INTO circuit_breaker_state "
                "(key, value, updated_at) VALUES (?, ?, ?)",
                (_STATE_KEY, state, now),
            )
            await self._db.conn.commit()
        except Exception:
            logger.exception("Failed to persist kill switch state")

    # ── Audit emission ─────────────────────────────────────────────────

    async def _emit(
        self,
        event_type: str,
        previous: KillSwitchState,
        severity: str,
        operator: str | None,
        reason: str | None = None,
    ) -> None:
        """Emit a kill-switch audit event through the configured bus."""
        if self._event_bus is None:
            return
        emit_fn = getattr(self._event_bus, "emit", None)
        if emit_fn is None:
            return
        try:
            await emit_fn(
                event_type,
                decision=self._state.value,
                previous_state=previous.value,
                reason=reason if reason is not None else self._reason,
                operator=operator,
                severity=severity,
            )
        except Exception:
            logger.debug("Failed to emit kill switch event", exc_info=True)
