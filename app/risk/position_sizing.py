"""Conservative position sizing — fixed-risk, confidence-adjusted, capped.

Design rules
------------
* **No Martingale** — position size never increases after a loss.
* **No loss-chasing** — edge and confidence only shrink the size.
* **No Kelly criterion** — Kelly can suggest aggressive sizing when edge
  appears large, which increases risk after volatile periods.
* **Confidence-adjusted** — lower confidence → proportionally smaller size.
* **Liquidity-capped** — never propose a size larger than available depth.

The sizing formula is::

    size = min(fixed_risk, liquidity, risk_limit)

    where  fixed_risk = equity × max_position_pct × confidence

This ensures the position never exceeds any single constraint.
"""

from __future__ import annotations

import logging

from app.config.settings import settings

logger = logging.getLogger(__name__)


class PositionSizer:
    """Determines conservative position sizes.

    Parameters
    ----------
    max_position_pct : float | None
        Maximum single-position size as a fraction of equity.
        Falls back to ``settings.max_position_pct``.
    """

    def __init__(
        self,
        max_position_pct: float | None = None,
    ) -> None:
        self._max_position_pct = (
            max_position_pct
            if max_position_pct is not None
            else settings.max_position_pct
        )

    def fixed_risk_size(self, equity: float, confidence: float) -> float:
        """Calculate position size using fixed-risk percentage of equity.

        ``size = equity × max_position_pct × confidence``

        Lower confidence proportionally reduces the bet.  Returns 0 when
        equity or confidence is ≤ 0.

        Parameters
        ----------
        equity : float
            Current portfolio equity (USDC).
        confidence : float
            Model confidence in [0, 1].

        Returns
        -------
        float
            Position size in contracts.
        """
        if equity <= 0 or confidence <= 0:
            return 0.0
        return equity * self._max_position_pct * confidence

    def determine_size(
        self,
        equity: float,
        confidence: float,
        net_edge: float,
        liquidity: float = float("inf"),
        risk_limit: float = float("inf"),
    ) -> float:
        """Determine final position size as the minimum of all constraints.

        Never sizes above zero when ``net_edge ≤ 0`` or ``confidence ≤ 0``.
        Never increases size based on previous losses (no Martingale).

        Parameters
        ----------
        equity : float
            Current portfolio equity.
        confidence : float
            Model confidence in [0, 1].
        net_edge : float
            Net expected edge after costs.  Must be > 0 to trade.
        liquidity : float
            Available order-book depth (caps the order).
        risk_limit : float
            Hard risk limit from portfolio manager.

        Returns
        -------
        float
            Final position size (0 if net_edge ≤ 0 or confidence ≤ 0).
        """
        # Fail-closed: zero edge or confidence → no trade
        if net_edge <= 0 or confidence <= 0:
            return 0.0

        fixed = self.fixed_risk_size(equity, confidence)

        # Conservative: min of all constraints
        size = min(fixed, liquidity, risk_limit)

        # Floor at zero
        return max(0.0, size)
