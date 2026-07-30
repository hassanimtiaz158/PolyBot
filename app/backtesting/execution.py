"""Deterministic execution adapter for backtesting.

Simulates fills using historical snapshot data with spread, slippage,
and the Polymarket fee model.  No random rejection, no latency,
no price impact — fully deterministic.

Price model
-----------
- **YES buy**: fill at YES ask = ``midpoint + half_spread``
- **NO buy**: fill at NO ask = ``(1 - midpoint) + half_spread``

Because YES and NO are complementary (YES + NO = 1), buying the
opposite side and letting ``PortfolioTracker._reduce_position``
compute the effective exit price via ``(1 - price) - entry``
correctly reflects the economic reality.
"""

from __future__ import annotations

import logging

from app.backtesting.models import MarketSnapshot
from app.portfolio.tracker import PortfolioTracker

logger = logging.getLogger(__name__)


class BacktestExecution:
    """Deterministic order execution for backtesting.

    Parameters
    ----------
    portfolio : PortfolioTracker
        Portfolio to update on each fill.
    fee_rate : float
        Taker fee coefficient Θ (default 0.05).
    """

    def __init__(
        self,
        portfolio: PortfolioTracker,
        fee_rate: float = 0.05,
    ) -> None:
        self._portfolio = portfolio
        self._fee_rate = fee_rate

    def execute(
        self,
        market_id: str,
        side: str,
        size: float,
        snapshot: MarketSnapshot,
        edge: float | None = None,
        signal_id: str | None = None,
    ) -> dict:
        """Execute a trade at the given historical snapshot's prices.

        Parameters
        ----------
        market_id : str
        side : str
            ``"YES"`` or ``"NO"``.
        size : float
            Number of contracts.
        snapshot : MarketSnapshot
            Historical market state at fill time.
        edge : float | None
            Net edge from EV engine (for record-keeping).
        signal_id : str | None
            Originating signal ID.

        Returns
        -------
        dict
            ``market_id``, ``side``, ``size``, ``fill_price``,
            ``raw_fill_price``, ``fee``, ``slippage``,
            ``pnl_change``, ``edge``, ``signal_id``.
        """
        midpoint = snapshot.midpoint
        spread = snapshot.spread
        depth = snapshot.depth
        half_spread = spread / 2.0

        # Base fill: buys always pay the ask side of the relevant book
        if side == "YES":
            raw_fill = midpoint + half_spread
        else:
            raw_fill = (1.0 - midpoint) + half_spread
        raw_fill = max(0.01, min(0.99, raw_fill))

        # Slippage when size exceeds visible depth
        slippage = 0.0
        ratio = size / max(depth, 1.0)
        if ratio > 1.0:
            excess = ratio - 1.0
            slippage = excess * half_spread * 0.5
            raw_fill += slippage  # buyer always pays more
            raw_fill = max(0.01, min(0.99, raw_fill))

        # Polymarket taker fee:  Θ × p × (1-p)
        fee_per_share = self._fee_rate * raw_fill * (1.0 - raw_fill)
        total_fee = fee_per_share * size

        effective_fill = raw_fill + fee_per_share  # buyer absorbs fee
        effective_fill = max(0.01, min(0.99, effective_fill))

        # Track P&L change caused by this fill
        realised_before = self._portfolio.total_realised_pnl()
        self._portfolio.add_trade(
            market_id, side, size, effective_fill, total_fee,
        )
        realised_after = self._portfolio.total_realised_pnl()

        return {
            "market_id": market_id,
            "side": side,
            "size": size,
            "fill_price": effective_fill,
            "raw_fill_price": raw_fill,
            "fee": total_fee,
            "slippage": slippage,
            "pnl_change": realised_after - realised_before,
            "edge": edge,
            "signal_id": signal_id,
        }
