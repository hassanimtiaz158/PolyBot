"""Live market-data collection -- the ``data_provider`` for the Orchestrator.

For every market already registered by :class:`MarketScanner`, fetches the
current CLOB order book, normalises and validates it, persists a snapshot,
computes order-book/liquidity features, and returns the per-market feature
dict the strategy pipeline expects.

This is the missing link between market discovery (``markets`` table) and
signal generation: without it, the orchestrator's ``data_provider`` has
nothing to return, so strategies are never evaluated no matter how many
eligible markets exist.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app.data.clob import ClobAdapter
from app.data.gamma import GammaAdapter
from app.data.normalizer import DataNormalizer
from app.data.validators import DataQuality, DataValidator
from app.features.liquidity import LiquidityFeatures
from app.features.orderbook import OrderBookFeatures
from app.monitoring.health import checks
from app.storage.models import MarketSnapshot
from app.storage.repositories import MarketRepository, SnapshotRepository

logger = logging.getLogger(__name__)


class LiveDataFeed:
    """Collects live order-book data for every registered market.

    Parameters
    ----------
    market_repo : MarketRepository
        Source of the eligible markets already persisted by the scanner.
    snapshot_repo : SnapshotRepository
        Where fetched order-book snapshots are persisted.
    gamma : GammaAdapter | None
        Used only to resolve each market's CLOB token IDs (not persisted
        by the scanner today). Created internally when ``None``.
    clob : ClobAdapter | None
        Order-book source. Created internally when ``None``.
    validator : DataValidator | None
        Freshness/structural validation gate. Created internally when
        ``None``.
    """

    def __init__(
        self,
        market_repo: MarketRepository | None = None,
        snapshot_repo: SnapshotRepository | None = None,
        gamma: GammaAdapter | None = None,
        clob: ClobAdapter | None = None,
        validator: DataValidator | None = None,
    ) -> None:
        self._market_repo = market_repo or MarketRepository()
        self._snapshot_repo = snapshot_repo or SnapshotRepository()
        self._gamma = gamma or GammaAdapter()
        self._clob = clob or ClobAdapter()
        self._validator = validator or DataValidator()

    async def collect(self) -> dict[str, dict[str, Any]]:
        """Fetch, validate, persist, and featurise data for every eligible market.

        Returns ``{market_id: feature_dict}`` -- exactly the shape the
        Orchestrator's ``data_provider`` callback is expected to return.
        Markets with unhealthy data (stale, malformed, no token id, no
        book) are silently skipped, matching the fail-closed principle:
        no data for a market simply means no signal for that market this
        iteration, not an error.
        """
        eligible = await self._market_repo.list_all()
        if not eligible:
            return {}
        eligible_ids = {m.market_id for m in eligible}

        try:
            raw_markets = await self._gamma.list_markets(
                closed=False, active=True, limit=100
            )
        except Exception:
            logger.exception("LiveDataFeed: failed to fetch markets from Gamma API")
            return {}

        token_map: dict[str, list[str]] = {}
        for raw in raw_markets:
            market_id = str(raw.get("id", ""))
            if market_id in eligible_ids:
                token_map[market_id] = DataNormalizer.extract_token_ids(raw)

        features_by_market: dict[str, dict[str, Any]] = {}
        any_healthy = False

        for market_id, token_ids in token_map.items():
            if not token_ids:
                continue
            # First outcome token (YES side) drives the book for this market.
            token_id = token_ids[0]

            try:
                book = await self._clob.get_order_book(token_id)
            except Exception:
                logger.exception(
                    "LiveDataFeed: order-book fetch failed for %s", market_id
                )
                continue
            if not book:
                continue

            snapshot = DataNormalizer.normalize_snapshot(market_id, book)
            if not snapshot.get("timestamp"):
                snapshot["timestamp"] = datetime.now(UTC).isoformat()

            report = self._validator.check_snapshot(snapshot)
            if report.quality != DataQuality.HEALTHY:
                logger.debug(
                    "LiveDataFeed: skipping %s -- %s (%s)",
                    market_id,
                    report.quality.value,
                    report.reason,
                )
                continue

            try:
                await self._snapshot_repo.insert(MarketSnapshot.from_row(snapshot))
            except Exception:
                logger.exception(
                    "LiveDataFeed: failed to persist snapshot for %s", market_id
                )

            ob_features = OrderBookFeatures().compute(snapshot)
            liq_features = LiquidityFeatures().compute(snapshot)

            features_by_market[market_id] = {
                "market_id": market_id,
                "token_id": token_id,
                "bid": snapshot["bid"],
                "ask": snapshot["ask"],
                **ob_features,
                **liq_features,
            }
            any_healthy = True

        if any_healthy:
            checks["data_freshness"].record_data()

        logger.info(
            "LiveDataFeed: collected data for %d/%d eligible markets",
            len(features_by_market),
            len(eligible_ids),
        )
        return features_by_market

    async def close(self) -> None:
        """Release the underlying HTTP clients."""
        await self._clob.close()
