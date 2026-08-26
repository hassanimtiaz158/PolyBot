"""Periodic market discovery and registration.

Queries the Polymarket Gamma API for active markets, evaluates them
against eligibility criteria, and registers qualifying markets in
the local database.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.config.settings import settings
from app.data.gamma import GammaAdapter
from app.discovery.eligibility import MarketEligibility
from app.storage.models import Market
from app.storage.repositories import MarketRepository

logger = logging.getLogger(__name__)


class MarketScanner:
    """Periodically scans Polymarket for new eligible markets.

    Parameters
    ----------
    gamma : GammaAdapter | None
        Gamma API adapter.  Created internally when ``None``.
    market_repo : MarketRepository | None
        Repository for persisting discovered markets.  Defaults to the
        global singleton.
    eligibility : MarketEligibility | None
        Eligibility evaluator.  Created internally when ``None``.
    """

    def __init__(
        self,
        gamma: GammaAdapter | None = None,
        market_repo: MarketRepository | None = None,
        eligibility: MarketEligibility | None = None,
    ) -> None:
        self._gamma = gamma or GammaAdapter()
        self._market_repo = market_repo or MarketRepository()
        self._eligibility = eligibility or MarketEligibility()
        self._running = False

    async def scan(self) -> list[dict[str, Any]]:
        """Discover and return eligible markets.

        Queries the Gamma API for active, open markets with minimum
        liquidity, evaluates each against the eligibility scorer, and
        persists qualifying markets to the database.

        Returns a list of dicts describing newly discovered eligible
        markets (those not already in the database).
        """
        logger.info("MarketScanner: starting scan")

        try:
            raw_markets = await self._gamma.list_markets(
                closed=False,
                active=True,
                limit=100,
            )
        except Exception:
            logger.exception("MarketScanner: failed to fetch markets from Gamma API")
            return []

        if not raw_markets:
            logger.info("MarketScanner: no markets returned from Gamma API")
            return []

        logger.info("MarketScanner: fetched %d raw markets", len(raw_markets))

        new_markets: list[dict[str, Any]] = []

        for raw in raw_markets:
            market_id = str(raw.get("id", ""))
            if not market_id:
                continue

            question = raw.get("question", "")
            condition_id = raw.get("conditionId")
            status = raw.get("active", True)
            resolution_time = raw.get("endDateIso")
            liquidity = float(raw.get("liquidity", 0) or 0)

            # Evaluate eligibility using available metrics. Historical
            # signal quality, model confidence, and execution quality
            # cannot be known for a market that has never been observed
            # or traded -- score them neutral (0.5) rather than worst-case
            # (0.0) so eligibility for a brand-new market is driven by its
            # actual, measurable liquidity and spread instead of being
            # unconditionally capped below the threshold.
            result = self._eligibility.evaluate(
                market_id,
                liquidity=liquidity,
                spread=float(raw.get("spread", 1.0) or 1.0),
                historical_signal_quality=0.5,
                model_confidence=0.5,
                execution_quality=0.5,
            )

            if not result.eligible:
                logger.debug(
                    "MarketScanner: market %s (%s) not eligible: %s",
                    market_id,
                    question[:50],
                    result.reasons,
                )
                continue

            # Check if market already exists in DB
            existing = await self._market_repo.get(market_id)
            if existing is not None:
                continue

            # Persist the new market
            market = Market(
                market_id=market_id,
                question=question,
                condition_id=condition_id,
                status="ACTIVE" if status else "INACTIVE",
                resolution_time=resolution_time,
                liquidity=liquidity,
            )
            try:
                await self._market_repo.upsert(market)
                new_markets.append({
                    "market_id": market_id,
                    "question": question,
                    "score": result.score,
                    "liquidity": liquidity,
                })
                logger.info(
                    "MarketScanner: registered market %s (%s) — score=%.2f",
                    market_id,
                    question[:50],
                    result.score,
                )
            except Exception:
                logger.exception("MarketScanner: failed to persist market %s", market_id)

        if new_markets:
            logger.info("MarketScanner: discovered %d new eligible markets", len(new_markets))
        else:
            logger.info("MarketScanner: no new eligible markets found")

        return new_markets

    async def run_loop(self, interval: int | None = None) -> None:
        """Continuously scan for new markets at the configured interval."""
        self._running = True
        interval = interval or settings.market_scan_interval_seconds
        logger.info("MarketScanner: starting run loop (interval=%ds)", interval)
        while self._running:
            try:
                await self.scan()
            except Exception:
                logger.exception("MarketScanner: scan iteration failed")
            await asyncio.sleep(interval)

    def stop(self) -> None:
        """Stop the continuous scan loop."""
        self._running = False
        logger.info("MarketScanner: stop requested")
