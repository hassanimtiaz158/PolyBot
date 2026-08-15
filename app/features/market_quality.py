"""Composite market-quality score and related health metrics.

The quality score combines liquidity, spread, volume, depth, and data
freshness into a single 0–1 score.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.features.orderbook import OrderBookFeatures
from app.storage.models import MarketSnapshot

# Default weights (from PRD §7)
DEFAULT_WEIGHTS: dict[str, float] = {
    "liquidity": 0.25,
    "spread": 0.20,
    "volume": 0.20,
    "depth": 0.20,
    "freshness": 0.15,
}


class MarketQuality:
    """Computes market-quality scores and health indicators.

    Usage::

        mq = MarketQuality()
        result = mq.compute(snapshot, orderbook_features, liquidity_features)
    """

    def __init__(self, weights: dict[str, float] | None = None) -> None:
        self.weights = weights or dict(DEFAULT_WEIGHTS)

    # ── Standalone calculators ─────────────────────────────────────

    @staticmethod
    def time_to_resolution(resolution_time: str | None) -> float | None:
        """Seconds remaining until market resolution.

        Returns ``None`` when the resolution time is missing or
        unparseable.  Returns ``0.0`` for already-expired markets.
        """
        if resolution_time is None:
            return None
        try:
            rt = datetime.fromisoformat(resolution_time.replace("Z", "+00:00"))
            remaining = (rt - datetime.now(UTC)).total_seconds()
            return max(0.0, remaining)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def data_freshness(
        timestamp_str: str | None, max_age_seconds: int = 5
    ) -> bool:
        """``True`` if the data timestamp is recent enough."""
        return OrderBookFeatures.data_freshness(
            timestamp_str, max_age_seconds=max_age_seconds
        )

    @staticmethod
    def spread_quality(spread: float | None) -> float:
        """0–1 quality score derived from absolute spread.

        Score = max(0, 1 - spread * 10).  A spread of 0 → 1.0,
        0.05 → 0.5, ≥ 0.10 → 0.0.
        """
        if spread is None:
            return 0.0
        return max(0.0, 1.0 - spread * 10)

    # ── Composite score ────────────────────────────────────────────

    def composite_score(
        self,
        liquidity_score: float | None,
        spread: float | None,
        volume: float | None,
        total_depth: float | None,
        data_fresh: bool,
    ) -> float:
        """Calculate a 0–1 composite market-quality score.

        Uses the configured ``weights`` dict to blend components.
        Missing components are treated as 0.
        """
        w = self.weights

        liq = (liquidity_score or 0.0) * w.get("liquidity", 0.25)
        sprd = self.spread_quality(spread) * w.get("spread", 0.20)
        vol = min(1.0, (volume or 0.0) / 100_000.0) * w.get("volume", 0.20)
        dep = min(1.0, (total_depth or 0.0) / 50_000.0) * w.get("depth", 0.20)
        fresh = (1.0 if data_fresh else 0.0) * w.get("freshness", 0.15)

        return liq + sprd + vol + dep + fresh

    # ── Batch compute ──────────────────────────────────────────────

    def compute(
        self,
        snapshot: MarketSnapshot | dict[str, Any],
        orderbook_features: dict[str, Any] | None = None,
        liquidity_features: dict[str, Any] | None = None,
        resolution_time: str | None = None,
    ) -> dict[str, Any]:
        """Compute quality metrics for a market.

        Parameters
        ----------
        snapshot:
            Current market snapshot.
        orderbook_features:
            Output of ``OrderBookFeatures.compute()`` (optional, used for
            ``data_fresh``).
        liquidity_features:
            Output of ``LiquidityFeatures.compute()`` (optional, used for
            ``liquidity_score`` and ``total_depth``).
        resolution_time:
            ``Market.resolution_time`` (optional).  If omitted,
            ``time_to_resolution`` will be ``None``.

        Returns a dict with keys ``time_to_resolution``, ``data_fresh``,
        ``liquidity_score``, ``spread_quality``, ``composite_score``,
        and ``timestamp``.
        """
        s = _unwrap(snapshot)
        obf = orderbook_features or {}
        lf = liquidity_features or {}

        ttr = self.time_to_resolution(resolution_time)
        fresh = obf.get("data_fresh", self.data_freshness(s.timestamp))
        liq_score = lf.get("liquidity_score")
        total_depth = lf.get("total_depth")

        composite = self.composite_score(
            liquidity_score=liq_score,
            spread=s.spread,
            volume=s.volume,
            total_depth=total_depth,
            data_fresh=fresh,
        )

        return {
            "time_to_resolution": ttr,
            "data_fresh": fresh,
            "liquidity_score": liq_score,
            "spread_quality": self.spread_quality(s.spread),
            "composite_score": composite,
            "timestamp": s.timestamp or datetime.now(UTC).isoformat(),
        }


def _unwrap(
    snapshot: MarketSnapshot | dict[str, Any],
) -> MarketSnapshot:
    if isinstance(snapshot, MarketSnapshot):
        return snapshot
    return MarketSnapshot.from_row(snapshot)
