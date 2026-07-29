"""Market microstructure strategy (S1).

Uses order-book imbalance, spread, and momentum to detect short-term
pricing inefficiencies.

Logic
-----
1.  Reject if data quality checks fail (stale / low liquidity).
2.  Reject if ``|OBI|`` is below a configurable threshold (no signal).
3.  Determine direction from OBI sign and momentum alignment.
4.  Compute implied probability from midpoint.
5.  Return ``CANDIDATE`` if confidence exceeds ``min_confidence``.
"""

from __future__ import annotations

from typing import Any

from app.strategies.base import Signal, Strategy


class MicrostructureStrategy(Strategy):
    """Generates signals from order-book microstructure features."""

    name = "microstructure"

    def __init__(
        self,
        min_confidence: float = 0.6,
        min_liquidity_score: float = 0.3,
        max_data_age_seconds: int = 5,
        min_obi_abs: float = 0.05,
        min_edge_bps: float = 10.0,
    ) -> None:
        super().__init__(
            min_confidence=min_confidence,
            min_liquidity_score=min_liquidity_score,
            max_data_age_seconds=max_data_age_seconds,
        )
        self.min_obi_abs = min_obi_abs
        self.min_edge_bps = min_edge_bps

    def generate_signal(
        self,
        features: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> Signal:
        market_id: str = features.get("market_id", "unknown")

        # ── Data-quality gating ─────────────────────────────────────
        reason = self._check_data_quality(features)
        if reason is not None:
            return self._reject(market_id, reason)

        # ── Extract features ────────────────────────────────────────
        obi = features.get("obi")
        midpoint = features.get("midpoint")
        spread = features.get("absolute_spread") or features.get("spread")
        bid = features.get("bid")
        ask = features.get("ask")

        if midpoint is None or midpoint <= 0:
            return self._reject(market_id, "Missing or zero midpoint")
        if bid is None or ask is None:
            return self._reject(market_id, "Missing bid or ask")
        if obi is None:
            return self._reject(market_id, "Missing order-book imbalance")

        # ── OBI threshold gate ──────────────────────────────────────
        if abs(obi) < self.min_obi_abs:
            return self._reject(
                market_id,
                f"OBI magnitude too small ({abs(obi):.4f} < "
                f"{self.min_obi_abs})",
            )

        # ── Direction from OBI ──────────────────────────────────────
        side = "YES" if obi > 0 else "NO"
        implied_probability = midpoint

        # ── Model probability: adjust toward the dominant side ──────
        # Stronger OBI → larger adjustment
        adjustment = abs(obi) * 0.10
        if side == "YES":
            model_probability = min(0.99, implied_probability + adjustment)
        else:
            model_probability = max(0.01, implied_probability - adjustment)

        gross_edge = model_probability - implied_probability

        # ── Edge threshold gate ─────────────────────────────────────
        edge_bps = gross_edge * 10_000
        if abs(edge_bps) < self.min_edge_bps:
            return self._reject(
                market_id,
                f"Gross edge too small ({edge_bps:.1f} bps, abs < "
                f"{self.min_edge_bps} bps)",
            )

        # ── Confidence from spread quality ──────────────────────────
        spread_quality = 1.0 - min(1.0, (spread or 0.0) * 10)
        momentum_aligned = self._momentum_aligned(features, side)
        confidence = 0.5 + 0.3 * abs(obi) + 0.2 * spread_quality
        if momentum_aligned:
            confidence = min(0.99, confidence + 0.10)

        # ── Confidence gate ─────────────────────────────────────────
        reason = self._check_confidence(confidence)
        if reason is not None:
            return self._reject(market_id, reason)

        return self._candidate(
            market_id=market_id,
            side=side,
            model_probability=model_probability,
            implied_probability=implied_probability,
            confidence=confidence,
            reason=(
                f"OBI={obi:.4f}, spread_quality={spread_quality:.2f}, "
                f"edge={edge_bps:.1f}bps"
            ),
            feature_snapshot=dict(features),
        )

    # ── Private helpers ─────────────────────────────────────────────

    @staticmethod
    def _momentum_aligned(
        features: dict[str, Any], side: str
    ) -> bool:
        """Check whether recent momentum aligns with the signal side."""
        velocity = features.get("velocity_60s")
        if velocity is None or not isinstance(velocity, (int, float)):
            return False
        if side == "YES":
            return bool(velocity > 0.0)
        return bool(velocity < 0.0)
