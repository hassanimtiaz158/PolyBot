"""Alert dispatcher for notifying operators about important events.

Supports Telegram and other channels.  MVP uses logging as the default.
"""

import logging
from typing import Any

from app.config.settings import settings

logger = logging.getLogger(__name__)


class AlertDispatcher:
    """Sends alerts to configured notification channels."""

    def __init__(self) -> None:
        self._enabled = bool(settings.poly_api_key)

    async def send(self, message: str, level: str = "INFO") -> None:
        """Dispatch an alert message."""
        logger.log(getattr(logging, level, logging.INFO), "ALERT: %s", message)

    async def send_trade_alert(self, details: dict[str, Any]) -> None:
        """Send an alert about a trade event."""
        await self.send(
            f"Trade: {details.get('action', 'unknown')} "
            f"for {details.get('market_id', '?')}"
        )

    async def send_error_alert(
        self, error: str, details: dict[str, Any] | None = None
    ) -> None:
        """Send an alert about an error condition."""
        await self.send(f"Error: {error}", level="ERROR")
