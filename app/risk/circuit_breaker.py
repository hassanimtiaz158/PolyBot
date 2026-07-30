"""Circuit breaker with three-state machine and SQLite persistence.

States
------
NORMAL  →  normal operation, trading permitted
WARNING →  soft limit breached (data stale, minor loss); trading permitted
HALTED  →  hard limit breached (major loss, API down); trading blocked

Transitions
-----------
NORMAL  → WARNING  on soft triggers (e.g. single data-age breach)
WARNING → HALTED   on escalation (e.g. consecutive losses above threshold)
NORMAL  → HALTED   on hard triggers (e.g. API health failure)
HALTED  → NORMAL   **only** via explicit operator clear (never auto-reset)

State is persisted to SQLite so it survives a restart.  On startup the
system loads the persisted state and stays HALTED until the operator
explicitly clears breakers.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from enum import StrEnum

from app.config.settings import settings

logger = logging.getLogger(__name__)


class BreakerState(StrEnum):
    """Three-state circuit breaker."""

    NORMAL = "NORMAL"
    WARNING = "WARNING"
    HALTED = "HALTED"


class CircuitBreaker:
    """Manages circuit breaker state and persistence.

    Parameters
    ----------
    persist : bool
        Whether to persist state to the database (default True).  Set to
        ``False`` in unit tests to avoid database dependencies.
    """

    def __init__(self, persist: bool = True) -> None:
        self._state: BreakerState = BreakerState.NORMAL
        self._reasons: list[str] = []
        self._triggered_at: str | None = None
        self._persist_enabled = persist

    # ── Properties ──────────────────────────────────────────────────────

    @property
    def state(self) -> BreakerState:
        return self._state

    @property
    def is_halted(self) -> bool:
        return self._state == BreakerState.HALTED

    @property
    def is_warning(self) -> bool:
        return self._state == BreakerState.WARNING

    @property
    def is_normal(self) -> bool:
        return self._state == BreakerState.NORMAL

    @property
    def reasons(self) -> list[str]:
        return list(self._reasons)

    @property
    def triggered_at(self) -> str | None:
        return self._triggered_at

    # ── Public API ──────────────────────────────────────────────────────

    async def trigger(self, reason: str, severity: str = "SOFT") -> BreakerState:
        """Activate or escalate the circuit breaker.

        Parameters
        ----------
        reason : str
            Machine-readable reason (e.g. ``"DAILY_LOSS"``).
        severity : str
            ``"SOFT"`` → NORMAL → WARNING.
            ``"HARD"`` → NORMAL or WARNING → HALTED immediately.

        Returns
        -------
        BreakerState
            The new state after triggering.
        """
        if reason in self._reasons:
            return self._state

        self._reasons.append(reason)
        self._triggered_at = datetime.now(UTC).isoformat()

        if severity.upper() == "HARD":
            self._state = BreakerState.HALTED
        elif self._state == BreakerState.NORMAL:
            self._state = BreakerState.WARNING
        elif self._state == BreakerState.WARNING:
            self._state = BreakerState.HALTED

        logger.warning(
            "Circuit breaker %s → %s (reason=%s)",
            self._state,
            self._state,
            reason,
        )
        await self._persist()
        return self._state

    async def clear(self, reason: str) -> BreakerState:
        """Clear a specific trigger reason.

        When all reasons are cleared, the state returns to NORMAL.
        """
        if reason in self._reasons:
            self._reasons.remove(reason)
            logger.info("Circuit breaker cleared: %s", reason)

        if not self._reasons:
            self._state = BreakerState.NORMAL
            self._triggered_at = None

        await self._persist()
        return self._state

    async def clear_all(self) -> BreakerState:
        """Clear all triggers and reset to NORMAL."""
        self._reasons.clear()
        self._state = BreakerState.NORMAL
        self._triggered_at = None
        logger.info("All circuit breakers cleared — state → NORMAL")
        await self._persist()
        return self._state

    async def check_and_trigger(
        self,
        daily_pnl: float,
        consecutive_losses: int,
        data_fresh: bool,
        api_healthy: bool,
        equity: float,
    ) -> BreakerState:
        """Evaluate system conditions and escalate state as needed.

        Returns the new state after evaluation.
        """
        if not data_fresh and self._state != BreakerState.HALTED:
            await self.trigger("STALE_DATA", severity="SOFT")

        if not api_healthy:
            await self.trigger("API_HEALTH", severity="HARD")

        if daily_pnl <= -(equity * settings.max_daily_loss_pct):
            await self.trigger("DAILY_LOSS", severity="HARD")

        if consecutive_losses >= settings.max_consecutive_losses:
            await self.trigger("CONSECUTIVE_LOSSES", severity="HARD")

        return self._state

    # ── Persistence ─────────────────────────────────────────────────────

    async def _persist(self) -> None:
        if not self._persist_enabled:
            return
        try:
            from app.storage.db import db

            state = json.dumps({
                "state": self._state.value,
                "reasons": self._reasons,
                "triggered_at": self._triggered_at,
            })
            now = datetime.now(UTC).isoformat()
            await db.conn.execute(
                "INSERT OR REPLACE INTO circuit_breaker_state "
                "(key, value, updated_at) VALUES (?, ?, ?)",
                ("circuit_breaker", state, now),
            )
            await db.conn.commit()
        except Exception:
            logger.exception("Failed to persist circuit breaker state")

    async def load_state(self) -> None:
        """Load persisted state from the database."""
        if not self._persist_enabled:
            return
        try:
            from app.storage.db import db

            cursor = await db.conn.execute(
                "SELECT value FROM circuit_breaker_state WHERE key = ?",
                ("circuit_breaker",),
            )
            row = await cursor.fetchone()
            if row:
                data = json.loads(row["value"])
                self._state = BreakerState(data.get("state", "NORMAL"))
                self._reasons = data.get("reasons", [])
                self._triggered_at = data.get("triggered_at")
                if self._state != BreakerState.NORMAL:
                    logger.warning(
                        "Loaded circuit breaker state: %s (%s)",
                        self._state.value,
                        self._reasons,
                    )
        except Exception:
            logger.exception("Failed to load circuit breaker state")
