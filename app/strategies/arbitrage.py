"""Cross-market / logical inconsistency detection strategy (S2).

Detects explicit relationships between markets — implication, mutual
exclusivity, complement, and sum constraints — and identifies pricing
inconsistencies.

Relationships are **explicitly declared** in the ``context``; the
strategy never infers relationships from semantic similarity.

Context format::

    context = {
        "relationships": [
            {
                "type": "IMPLIES",  # MUTUALLY_EXCLUSIVE | COMPLEMENT | SUM_CONSTRAINT
                "markets": ["mkt_A", "mkt_B"],
                "params": {"sum_target": 1.0},  # only for SUM_CONSTRAINT
            },
        ],
        "market_data": {
            "mkt_A": {
                "midpoint": 0.60,
                "liquidity_score": 0.7,
                "timestamp": "...",
                "market_id": "mkt_A",
                "spread": 0.05,
            },
            "mkt_B": { ... },
        },
    }
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from app.strategies.base import Signal, Strategy

# Maximum allowed pricing inconsistency before a signal fires.
MAX_LEGACY_EPSILON = 0.02  # 2 % price units


class RelationshipType(StrEnum):
    """Explicit logical relationship between two markets."""

    IMPLIES = "IMPLIES"
    MUTUALLY_EXCLUSIVE = "MUTUALLY_EXCLUSIVE"
    COMPLEMENT = "COMPLEMENT"
    SUM_CONSTRAINT = "SUM_CONSTRAINT"


class ArbitrageStrategy(Strategy):
    """Generates signals from cross-market pricing inconsistencies."""

    name = "arbitrage"

    def __init__(
        self,
        min_confidence: float = 0.6,
        min_liquidity_score: float = 0.3,
        max_data_age_seconds: int = 5,
        max_inconsistency: float = MAX_LEGACY_EPSILON,
    ) -> None:
        super().__init__(
            min_confidence=min_confidence,
            min_liquidity_score=min_liquidity_score,
            max_data_age_seconds=max_data_age_seconds,
        )
        self.max_inconsistency = max_inconsistency

    def generate_signal(
        self,
        features: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> Signal:
        market_id: str = features.get("market_id", "unknown")

        if context is None:
            return self._reject(market_id, "No context provided")

        market_data: dict[str, dict[str, Any]] = context.get(
            "market_data", {}
        )
        relationships: list[dict[str, Any]] = context.get(
            "relationships", []
        )

        if not relationships:
            return self._reject(market_id, "No relationships defined")
        if not market_data:
            return self._reject(market_id, "No market_data in context")

        # Check every relationship that involves this market
        for rel in relationships:
            rel_type = rel.get("type", "")
            rel_markets: list[str] = rel.get("markets", [])

            if market_id not in rel_markets:
                continue

            result = self._evaluate_relationship(
                rel_type, rel_markets, market_id, market_data, rel.get("params", {})
            )
            if result is not None:
                return result

        return self._reject(
            market_id, "No inconsistency detected in relationships"
        )

    # ── Per-relationship evaluators ─────────────────────────────────

    def _evaluate_relationship(
        self,
        rel_type: str,
        markets: list[str],
        this_market: str,
        market_data: dict[str, dict[str, Any]],
        params: dict[str, Any],
    ) -> Signal | None:
        """Evaluate a single relationship.  Returns a Signal or None."""

        if rel_type == RelationshipType.IMPLIES.value:
            return self._check_implies(markets, this_market, market_data)
        elif rel_type == RelationshipType.MUTUALLY_EXCLUSIVE.value:
            return self._check_mutually_exclusive(
                markets, this_market, market_data
            )
        elif rel_type == RelationshipType.COMPLEMENT.value:
            return self._check_complement(markets, this_market, market_data)
        elif rel_type == RelationshipType.SUM_CONSTRAINT.value:
            return self._check_sum_constraint(
                markets, this_market, market_data, params
            )
        return None

    def _check_implies(
        self,
        markets: list[str],
        this_market: str,
        market_data: dict[str, dict[str, Any]],
    ) -> Signal | None:
        """A → B : P(B) should be >= P(A)."""
        if len(markets) < 2:
            return None
        # markets[0] = A (antecedent), markets[1] = B (consequent)
        p_a = self._mid(markets[0], market_data)
        p_b = self._mid(markets[1], market_data)
        if p_a is None or p_b is None:
            return None
        if p_a > p_b + self.max_inconsistency:
            inconsistency = p_a - p_b
            return self._build_inconsistency_signal(
                this_market,
                f"IMPLIES violation: P({markets[0]})={p_a:.3f} > "
                f"P({markets[1]})={p_b:.3f} (Δ={inconsistency:.3f})",
                market_data,
            )
        return None

    def _check_mutually_exclusive(
        self,
        markets: list[str],
        this_market: str,
        market_data: dict[str, dict[str, Any]],
    ) -> Signal | None:
        """A XOR B: P(A) + P(B) should be <= 1.0."""
        if len(markets) < 2:
            return None
        p_a = self._mid(markets[0], market_data)
        p_b = self._mid(markets[1], market_data)
        if p_a is None or p_b is None:
            return None
        total = p_a + p_b
        if total > 1.0 + self.max_inconsistency:
            return self._build_inconsistency_signal(
                this_market,
                f"MUTUALLY_EXCLUSIVE violation: P({markets[0]})={p_a:.3f} + "
                f"P({markets[1]})={p_b:.3f} = {total:.3f} > 1.0",
                market_data,
            )
        return None

    def _check_complement(
        self,
        markets: list[str],
        this_market: str,
        market_data: dict[str, dict[str, Any]],
    ) -> Signal | None:
        """A = ¬B: P(A) + P(B) should be ~1.0."""
        if len(markets) < 2:
            return None
        p_a = self._mid(markets[0], market_data)
        p_b = self._mid(markets[1], market_data)
        if p_a is None or p_b is None:
            return None
        total = p_a + p_b
        if abs(total - 1.0) > self.max_inconsistency:
            return self._build_inconsistency_signal(
                this_market,
                f"COMPLEMENT violation: P({markets[0]})={p_a:.3f} + "
                f"P({markets[1]})={p_b:.3f} = {total:.3f} (expected 1.0)",
                market_data,
            )
        return None

    def _check_sum_constraint(
        self,
        markets: list[str],
        this_market: str,
        market_data: dict[str, dict[str, Any]],
        params: dict[str, Any],
    ) -> Signal | None:
        """Σ P(market_i) should be ~sum_target."""
        sum_target = params.get("sum_target", 1.0)
        prices: list[float] = []
        for m in markets:
            p = self._mid(m, market_data)
            if p is not None:
                prices.append(p)
        if not prices:
            return None
        total = sum(prices)
        if abs(total - sum_target) > self.max_inconsistency:
            return self._build_inconsistency_signal(
                this_market,
                f"SUM_CONSTRAINT violation: Σ = {total:.3f} "
                f"(expected {sum_target:.3f})",
                market_data,
            )
        return None

    # ── Helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _mid(
        market_id: str, market_data: dict[str, dict[str, Any]]
    ) -> float | None:
        data = market_data.get(market_id)
        if data is None:
            return None
        return data.get("midpoint")

    def _build_inconsistency_signal(
        self,
        market_id: str,
        reason: str,
        market_data: dict[str, dict[str, Any]],
    ) -> Signal:
        data = market_data.get(market_id, {})
        midpoint = data.get("midpoint", 0.5)

        return self._candidate(
            market_id=market_id,
            side="YES",
            model_probability=0.5,
            implied_probability=midpoint,
            confidence=self.min_confidence,
            reason=reason,
            feature_snapshot=dict(data),
        )
