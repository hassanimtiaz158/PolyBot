"""Expected value calculation: gross edge → net edge → trade decision.

Flow
----
1. Compute **gross edge** = ``model_probability - implied_probability``.
2. Estimate **execution costs** via ``CostEstimator`` (spread, slippage,
   fees, partial-fill risk, uncertainty).
3. **Net edge** = gross edge − total cost.
4. If ``net_edge ≥ min_net_edge``, the opportunity is **tradeable**;
   otherwise it is ``NO_TRADE``.

Polymarket fee integration
--------------------------
The taker fee follows ``fee = C × Θ × p × (1-p)``, with category-specific
Θ values defined in :mod:`app.ev.costs`.  The engine applies the fee as a
per-share deduction in probability units.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.config.settings import settings
from app.ev.costs import DEFAULT_FEE_RATE, CostEstimator

logger = logging.getLogger(__name__)


@dataclass
class EVResult:
    """Complete result of an expected-value evaluation.

    All penalty fields are in **probability units** (0.01 = 1 percentage
    point).  A ``tradeable=False`` result means the opportunity should be
    downgraded to ``NO_TRADE``.
    """

    gross_edge: float
    spread_penalty: float
    slippage_penalty: float
    fee_penalty: float
    partial_fill_penalty: float
    uncertainty_penalty: float
    total_cost: float
    net_edge: float
    confidence: float
    tradeable: bool
    reason: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    # ── Convenience ──────────────────────────────────────────────────

    @property
    def cost_breakdown(self) -> dict[str, float]:
        """Return a human-readable breakdown of cost components."""
        return {
            "spread_penalty": self.spread_penalty,
            "slippage_penalty": self.slippage_penalty,
            "fee_penalty": self.fee_penalty,
            "partial_fill_penalty": self.partial_fill_penalty,
            "uncertainty_penalty": self.uncertainty_penalty,
            "total_cost": self.total_cost,
        }


class ExpectedValueEngine:
    """Calculates gross and net expected value for a potential trade.

    Parameters
    ----------
    cost_estimator : CostEstimator | None
        Injected cost estimator.  A fresh instance is created when ``None``.
    min_net_edge : float | None
        Minimum acceptable net edge.  Falls back to ``settings.min_net_edge``.
    """

    def __init__(
        self,
        cost_estimator: CostEstimator | None = None,
        min_net_edge: float | None = None,
    ) -> None:
        self._cost_estimator = cost_estimator or CostEstimator()
        self._min_net_edge = (
            min_net_edge if min_net_edge is not None else settings.min_net_edge
        )

    # ── Public API ───────────────────────────────────────────────────

    def evaluate(
        self,
        model_probability: float,
        implied_probability: float,
        spread: float,
        price: float,
        size: float,
        depth: float,
        confidence: float,
        fee_rate: float = DEFAULT_FEE_RATE,
    ) -> EVResult:
        """Evaluate the expected value of a potential trade.

        Parameters
        ----------
        model_probability : float
            Model-estimated true probability of the outcome (0–1).
        implied_probability : float
            Market-implied probability (midpoint price, 0–1).
        spread : float
            Absolute bid-ask spread in probability units.
        price : float
            Midpoint price (same as *implied_probability* in most cases).
        size : float
            Desired order size in contracts.
        depth : float
            Visible depth at the best price level.
        confidence : float
            Model confidence in [0, 1].
        fee_rate : float
            Taker-fee coefficient Θ (default 0.05).

        Returns
        -------
        EVResult
            Evaluation result with full component breakdown.
        """
        gross = self.gross_edge(model_probability, implied_probability)

        sp = self._cost_estimator.spread_cost(spread, price)
        sl = self._cost_estimator.slippage_cost(size, depth, spread, price)
        fp = self._cost_estimator.fee_cost(price, fee_rate)
        pf = self._cost_estimator.partial_fill_risk(size, depth)
        up = self._cost_estimator.uncertainty_penalty(confidence)
        total = sp + sl + fp + pf + up

        net = gross - total
        tradeable = net >= self._min_net_edge

        if tradeable:
            reason = ""
        else:
            reason = (
                f"Net edge {net:.4f} < threshold {self._min_net_edge}"
            )

        return EVResult(
            gross_edge=gross,
            spread_penalty=sp,
            slippage_penalty=sl,
            fee_penalty=fp,
            partial_fill_penalty=pf,
            uncertainty_penalty=up,
            total_cost=total,
            net_edge=net,
            confidence=confidence,
            tradeable=tradeable,
            reason=reason,
        )

    def gross_edge(
        self, model_probability: float, implied_probability: float
    ) -> float:
        """Calculate gross edge as the difference between probabilities.

        Parameters
        ----------
        model_probability : float
            Model-estimated true probability.
        implied_probability : float
            Market-implied probability.

        Returns
        -------
        float
            ``model_probability - implied_probability``.
        """
        return model_probability - implied_probability

    def net_edge(
        self,
        model_probability: float,
        implied_probability: float,
        spread: float,
        price: float,
        size: float,
        depth: float,
        confidence: float,
        fee_rate: float = DEFAULT_FEE_RATE,
    ) -> float:
        """Compute net edge after all execution costs.

        This is a convenience wrapper around :meth:`evaluate` returning
        only the numeric net edge.

        Returns
        -------
        float
            Net edge in probability units.
        """
        return self.evaluate(
            model_probability,
            implied_probability,
            spread,
            price,
            size,
            depth,
            confidence,
            fee_rate,
        ).net_edge

    def is_acceptable(self, net_edge: float) -> bool:
        """Check if *net_edge* exceeds the configured minimum threshold.

        Parameters
        ----------
        net_edge : float
            Net edge value to test.

        Returns
        -------
        bool
            ``True`` if ``net_edge >= min_net_edge``.
        """
        return net_edge >= self._min_net_edge
