"""Periodic market discovery and registration."""

import asyncio
import logging
from typing import Any

from app.config.settings import settings
from app.discovery.eligibility import MarketEligibility

logger = logging.getLogger(__name__)


class MarketScanner:
    """Periodically scans Polymarket for new eligible markets."""

    def __init__(self) -> None:
        self._eligibility = MarketEligibility()
        self._running = False

    async def scan(self) -> list[dict[str, Any]]:
        """Discover and return eligible markets."""
        logger.debug("MarketScanner.scan called")
        return []

    async def run_loop(self, interval: int | None = None) -> None:
        """Continuously scan for new markets at the configured interval."""
        self._running = True
        interval = interval or settings.market_scan_interval_seconds
        while self._running:
            await self.scan()
            await asyncio.sleep(interval)
