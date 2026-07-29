"""Circuit breaker state machine with persistence."""

import json
import logging
from datetime import UTC, datetime

from app.config.settings import settings

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """Manages circuit breaker state and persistence.

    Breaker state is persisted to SQLite so it survives restarts.
    On startup the system loads the persisted state and stays HALTED
    until the operator explicitly clears breakers.
    """

    def __init__(self) -> None:
        self._active_breakers: list[str] = []
        self._triggered_at: str | None = None

    @property
    def is_halted(self) -> bool:
        """Return True if any breaker is active."""
        return len(self._active_breakers) > 0

    @property
    def active_breakers(self) -> list[str]:
        return list(self._active_breakers)

    async def trigger(self, reason: str) -> None:
        """Activate a circuit breaker and persist state."""
        if reason not in self._active_breakers:
            self._active_breakers.append(reason)
            self._triggered_at = datetime.now(UTC).isoformat()
            logger.warning("Circuit breaker triggered: %s", reason)
            await self._persist()

    async def clear(self, reason: str) -> None:
        """Clear a specific breaker."""
        if reason in self._active_breakers:
            self._active_breakers.remove(reason)
            logger.info("Circuit breaker cleared: %s", reason)
            await self._persist()

    async def clear_all(self) -> None:
        """Clear all active breakers."""
        self._active_breakers.clear()
        self._triggered_at = None
        logger.info("All circuit breakers cleared")
        await self._persist()

    async def check_and_trigger(
        self,
        daily_pnl: float,
        consecutive_losses: int,
        data_fresh: bool,
        api_healthy: bool,
        equity: float,
    ) -> None:
        """Evaluate conditions and trigger breakers as needed."""
        if daily_pnl <= -(equity * settings.max_daily_loss_pct):
            await self.trigger("DAILY_LOSS")
        if consecutive_losses >= settings.max_consecutive_losses:
            await self.trigger("CONSECUTIVE_LOSSES")
        if not data_fresh:
            await self.trigger("STALE_DATA")
        if not api_healthy:
            await self.trigger("API_HEALTH")

    async def _persist(self) -> None:
        """Write breaker state to the database."""
        try:
            from app.storage.db import db

            state = json.dumps({
                "breakers": self._active_breakers,
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
        """Load breaker state from the database on startup."""
        try:
            from app.storage.db import db

            cursor = await db.conn.execute(
                "SELECT value FROM circuit_breaker_state WHERE key = ?",
                ("circuit_breaker",),
            )
            row = await cursor.fetchone()
            if row:
                data = json.loads(row["value"])
                self._active_breakers = data.get("breakers", [])
                self._triggered_at = data.get("triggered_at")
                if self._active_breakers:
                    logger.warning(
                        "Loaded circuit breaker state: %s",
                        self._active_breakers,
                    )
        except Exception:
            logger.exception("Failed to load circuit breaker state")
