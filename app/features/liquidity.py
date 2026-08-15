"""Liquidity estimation from order-book depth, volume, and spread."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.storage.models import MarketSnapshot


class LiquidityFeatures:
    """Estimates market liquidity from depth, volume, and spread.

    Usage::

        lf = LiquidityFeatures()
        features = lf.compute(snapshot)
    """

    # ── Standalone calculators ─────────────────────────────────────

    @staticmethod
    def depth_liquidity(
        bid_depth: float | None, ask_depth: float | None
    ) -> float | None:
        """Total depth on both sides.  Returns None if either is missing."""
        if bid_depth is None or ask_depth is None:
            return None
        return bid_depth + ask_depth

    @staticmethod
    def liquidity_score(
        bid_depth: float | None,
        ask_depth: float | None,
        volume: float | None,
        spread: float | None,
    ) -> float | None:
        """Composite 0–1 liquidity score.

        Components (weighted average):
            - Depth score (40 %): total depth capped at 50 000.
            - Volume score (30 %): volume capped at 100 000.
            - Spread penalty (30 %): wider spread reduces score.

        Returns ``None`` when depth is unavailable.
        """
        if bid_depth is None or ask_depth is None:
            return None

        total_depth = bid_depth + ask_depth
        depth_score = min(1.0, total_depth / 50_000.0)

        vol_score = min(1.0, (volume or 0.0) / 100_000.0)

        spread_penalty = 1.0
        if spread is not None and spread > 0:
            spread_penalty = max(0.0, 1.0 - spread * 10)

        return 0.4 * depth_score + 0.3 * vol_score + 0.3 * spread_penalty

    # ── Batch compute ──────────────────────────────────────────────

    def compute(
        self, snapshot: MarketSnapshot | dict[str, Any]
    ) -> dict[str, Any]:
        """Compute liquidity metrics from a snapshot.

        Returns a dict with keys ``total_depth``, ``volume``,
        ``liquidity_score``, and ``timestamp``.
        """
        s = _unwrap(snapshot)

        return {
            "total_depth": self.depth_liquidity(s.bid_depth, s.ask_depth),
            "volume": s.volume,
            "liquidity_score": self.liquidity_score(
                s.bid_depth, s.ask_depth, s.volume, s.spread
            ),
            "timestamp": s.timestamp or datetime.now(UTC).isoformat(),
        }


def _unwrap(
    snapshot: MarketSnapshot | dict[str, Any],
) -> MarketSnapshot:
    if isinstance(snapshot, MarketSnapshot):
        return snapshot
    return MarketSnapshot.from_row(snapshot)
