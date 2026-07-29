"""Execution cost estimation: spread, slippage, fees, partial fill, and uncertainty penalty.

All cost methods return values in **probability units** (same dimension as
``gross_edge`` in ``ExpectedValueEngine``), so they can be subtracted directly
from the gross edge.

Polymarket fee model
--------------------
Taker fee (USDC): ``fee = C × Θ × p × (1-p)``

- ``C``  = number of contracts
- ``p``  = trade price (0.01 – 0.99)
- ``Θ``  = fee coefficient by market category (default 0.05)

Per-share fee = ``Θ × p × (1-p)`` — symmetric around ``p=0.50``.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Default fee coefficient (Θ) for "Other/General" category.
DEFAULT_FEE_RATE = 0.05

# Category-specific fee coefficients per Polymarket fee schedule.
FEE_RATES_BY_CATEGORY: dict[str, float] = {
    "crypto": 0.07,
    "sports": 0.05,
    "finance": 0.04,
    "politics": 0.04,
    "economics": 0.05,
    "culture": 0.05,
    "weather": 0.05,
    "other": 0.05,
    "mentions": 0.04,
    "tech": 0.04,
    "geopolitics": 0.0,  # fee-free
}


class CostEstimator:
    """Estimates total execution cost for a potential trade.

    All public methods return costs in **probability units** (0.01 = 1
    percentage point), compatible with gross edge subtraction.
    """

    @staticmethod
    def spread_cost(spread: float, price: float) -> float:
        """Half-spread cost of crossing the book once, in probability units.

        Parameters
        ----------
        spread : float
            Absolute bid-ask spread in probability units (e.g. 0.02 = 2¢).
        price : float
            Midpoint price (probability).

        Returns
        -------
        float
            0 if *price* ≤ 0 or *spread* ≤ 0, else ``spread / 2``.
        """
        if price <= 0 or spread <= 0:
            return 0.0
        return spread / 2.0

    @staticmethod
    def slippage_cost(size: float, depth: float, spread: float, price: float) -> float:
        """Price impact when order size exceeds visible depth.

        Models the extra cost of moving up the order book beyond the best
        level.  Each additional depth-tier increment adds roughly half the
        spread.  Capped at 3¢ to prevent extreme values.

        Parameters
        ----------
        size : float
            Order size in contracts.
        depth : float
            Visible depth at the best price level.
        spread : float
            Absolute bid-ask spread.
        price : float
            Midpoint price.

        Returns
        -------
        float
            Estimated slippage cost in probability units, clamped to [0, 0.03].
        """
        if depth <= 0 or size <= 0 or price <= 0:
            return 0.0
        ratio = size / depth
        if ratio <= 1.0:
            return 0.0
        extra = (ratio - 1.0) * spread / 2.0
        return min(extra, 0.03)

    @staticmethod
    def fee_cost(price: float, fee_rate: float = DEFAULT_FEE_RATE) -> float:
        """Polymarket taker fee per share: ``Θ × p × (1-p)``.

        Parameters
        ----------
        price : float
            Trade price (midpoint / implied probability).
        fee_rate : float
            Fee coefficient Θ (default 0.05 for "Other/General").

        Returns
        -------
        float
            Fee per share in probability units.
        """
        return fee_rate * price * (1.0 - price)

    @staticmethod
    def partial_fill_risk(size: float, depth: float) -> float:
        """Penalty for risk of partial fill on orders exceeding visible depth.

        When *size* exceeds *depth*, there is a non-zero probability that
        the order only partially fills.  This penalty linearly scales with
        the excess ratio, capped at 10 %.

        Parameters
        ----------
        size : float
            Order size in contracts.
        depth : float
            Visible depth at the best level.

        Returns
        -------
        float
            Estimated partial-fill penalty in probability units, [0, 0.10].
        """
        if depth <= 0 or size <= 0:
            return 0.0
        ratio = size / depth
        if ratio <= 1.0:
            return 0.0
        return min(0.2 * (ratio - 1.0), 0.10)

    @staticmethod
    def uncertainty_penalty(confidence: float, base_penalty: float = 0.01) -> float:
        """Penalty proportional to model uncertainty (1 - confidence).

        Parameters
        ----------
        confidence : float
            Model confidence in [0, 1].
        base_penalty : float
            Maximum penalty when confidence = 0.

        Returns
        -------
        float
            ``base_penalty × (1 - confidence)``.
        """
        return base_penalty * (1.0 - confidence)

    def total_cost(
        self,
        spread: float,
        price: float,
        size: float,
        depth: float,
        confidence: float,
        fee_rate: float = DEFAULT_FEE_RATE,
    ) -> float:
        """Aggregate all cost components into a single number.

        Parameters
        ----------
        spread : float
            Absolute bid-ask spread.
        price : float
            Midpoint price.
        size : float
            Order size.
        depth : float
            Visible depth at best level.
        confidence : float
            Model confidence in [0, 1].
        fee_rate : float
            Taker-fee coefficient Θ (default 0.05).

        Returns
        -------
        float
            Total estimated cost in probability units.
        """
        return (
            self.spread_cost(spread, price)
            + self.slippage_cost(size, depth, spread, price)
            + self.fee_cost(price, fee_rate)
            + self.partial_fill_risk(size, depth)
            + self.uncertainty_penalty(confidence)
        )
