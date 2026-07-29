"""Abstract base class and data types for all strategies.

Every strategy produces a ``Signal`` with a ``decision`` of either
``NO_SIGNAL`` (no opportunity found) or ``CANDIDATE`` (potential
opportunity — must pass the risk engine before execution).

Strategies perform data-quality gating internally (stale data,
insufficient liquidity, malformed features) and return ``NO_SIGNAL``
when the data is unfit.  They **never** bypass the risk engine.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class StrategyDecision(StrEnum):
    """Gateway decision from a strategy evaluation.

    ``NO_SIGNAL`` — no actionable opportunity (data unfit, low
    confidence, insufficient edge, etc.).

    ``CANDIDATE`` — the strategy has identified a potential opportunity
    that should be forwarded to the risk engine for final approval.
    """

    NO_SIGNAL = "NO_SIGNAL"
    CANDIDATE = "CANDIDATE"


@dataclass
class Signal:
    """Output of a single strategy evaluation.

    The ``decision`` field distinguishes between "no signal" (the
    strategy sees no opportunity) and "candidate" (the strategy sees
    a potential opportunity that must still pass the risk engine).

    ``feature_snapshot`` captures the feature values at evaluation time
    so downstream consumers can inspect what drove the decision.
    """

    strategy: str
    market_id: str
    side: str
    decision: StrategyDecision = StrategyDecision.NO_SIGNAL

    model_probability: float | None = None
    implied_probability: float | None = None
    gross_edge: float | None = None
    confidence: float | None = None

    reason: str = ""
    signal_id: str = ""
    timestamp: str = ""
    feature_snapshot: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.signal_id:
            self.signal_id = uuid.uuid4().hex[:12]
        if not self.timestamp:
            self.timestamp = datetime.now(UTC).isoformat()
        if (
            self.decision == StrategyDecision.CANDIDATE
            and self.model_probability is not None
            and self.implied_probability is not None
            and self.gross_edge is None
        ):
            self.gross_edge = self.model_probability - self.implied_probability


class Strategy(ABC):
    """Interface every concrete strategy must implement.

    Parameters
    ----------
    min_confidence : float
        Minimum confidence threshold.  Signals below this are downgraded
        to ``NO_SIGNAL``.
    min_liquidity_score : float
        Minimum liquidity score (0–1).  Markets below this are skipped.
    max_data_age_seconds : int
        Maximum acceptable age for feature data.
    """

    name: str = "base"

    def __init__(
        self,
        min_confidence: float = 0.6,
        min_liquidity_score: float = 0.3,
        max_data_age_seconds: int = 5,
    ) -> None:
        self.min_confidence = min_confidence
        self.min_liquidity_score = min_liquidity_score
        self.max_data_age_seconds = max_data_age_seconds

    # ── Public interface ─────────────────────────────────────────────

    @abstractmethod
    def generate_signal(
        self,
        features: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> Signal:
        """Evaluate market features and return a ``Signal``.

        Parameters
        ----------
        features : dict[str, Any]
            Computed feature dictionary (order-book, momentum, liquidity,
            market-quality scores, etc.).
        context : dict[str, Any] | None
            Optional context such as related-market data or relationship
            definitions.  Strategy-specific.

        Returns
        -------
        Signal
            Always returns a ``Signal`` — never ``None``.
        """

    # ── Shared gating helpers ───────────────────────────────────────

    def _reject(
        self, market_id: str, reason: str, side: str = "NO_SIDE"
    ) -> Signal:
        """Build a ``NO_SIGNAL`` with the given rejection reason."""
        return Signal(
            strategy=self.name,
            market_id=market_id,
            side=side,
            decision=StrategyDecision.NO_SIGNAL,
            reason=reason,
        )

    def _candidate(
        self,
        market_id: str,
        side: str,
        model_probability: float,
        implied_probability: float,
        confidence: float,
        reason: str,
        feature_snapshot: dict[str, Any] | None = None,
    ) -> Signal:
        """Build a ``CANDIDATE`` signal.

        ``gross_edge`` is computed automatically.
        """
        return Signal(
            strategy=self.name,
            market_id=market_id,
            side=side,
            decision=StrategyDecision.CANDIDATE,
            model_probability=model_probability,
            implied_probability=implied_probability,
            confidence=confidence,
            reason=reason,
            feature_snapshot=feature_snapshot or {},
        )

    def _check_data_quality(
        self, features: dict[str, Any]
    ) -> str | None:
        """Return a rejection reason if data quality is insufficient.

        Checks performed:
        - Timestamp presence and freshness
        - Feature-validator errors
        - Liquidity score vs ``min_liquidity_score``
        """
        ts = features.get("timestamp")
        if ts is None:
            return "Missing timestamp"
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            age = (datetime.now(UTC) - dt).total_seconds()
            if age > self.max_data_age_seconds:
                return f"Stale data ({age:.1f}s old)"
        except (ValueError, TypeError):
            return f"Unparseable timestamp: {ts!r}"

        liq = features.get("liquidity_score")
        if liq is not None and liq < self.min_liquidity_score:
            return (
                f"Insufficient liquidity (score={liq:.3f} < "
                f"{self.min_liquidity_score})"
            )

        return None

    def _check_confidence(
        self, confidence: float | None
    ) -> str | None:
        """Return a rejection reason if confidence is too low."""
        if confidence is None:
            return "Missing confidence"
        if confidence < self.min_confidence:
            return (
                f"Low confidence ({confidence:.3f} < "
                f"{self.min_confidence})"
            )
        return None
