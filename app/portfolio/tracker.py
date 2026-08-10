"""Portfolio tracking — positions, P&L, equity, and exposure.

Beyond per-market P&L, the tracker exposes portfolio-level exposure
queries used by the risk engine to prevent excessive concentration:

* ``total_exposure``        — sum of all position sizes.
* ``market_exposure``       — exposure to a single market.
* ``strategy_exposure``     — exposure attributable to one strategy.
* ``exposure_for``          — exposure across a set of markets
  (used for correlated-event and resolution-time aggregation).
* ``directional_exposure``  — signed exposure to an underlying event,
  where each market carries a direction: ``+1`` means YES on the
  market = the event occurs, ``-1`` means YES = the event does NOT
  occur.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Iterable, Mapping
from typing import Any

logger = logging.getLogger(__name__)

_INITIAL_EQUITY = 10_000.0


class PortfolioTracker:
    """Tracks positions, equity, and P&L across all markets.

    * ``add_trade`` with the same side as an existing position →
      weighted-average entry price.
    * ``add_trade`` with the opposite side → partial or full close
      with realised P&L.  In a binary prediction market YES + NO = 1,
      so the effective exit price for the position is ``1 - price``
      where *price* is the new trade's fill price.
    * Realised P&L is accumulated globally and preserved even after
      a position is fully closed.
    * Equity = initial_equity + total_realised_pnl + total_unrealised_pnl.
    """

    def __init__(self, initial_equity: float = _INITIAL_EQUITY) -> None:
        self._initial_equity = max(0.0, initial_equity)
        self._positions: dict[str, dict[str, Any]] = {}
        self._total_realised_pnl: float = 0.0

    # ── Properties ─────────────────────────────────────────────────

    @property
    def equity(self) -> float:
        return self._initial_equity + self._total_realised_pnl + self.total_unrealised_pnl()

    @property
    def initial_equity(self) -> float:
        return self._initial_equity

    # ── Trade lifecycle ────────────────────────────────────────────

    def add_trade(
        self,
        market_id: str,
        side: str,
        size: float,
        price: float,
        fee: float = 0.0,
        strategy: str = "",
    ) -> None:
        """Record a filled trade and update the portfolio.

        Parameters
        ----------
        market_id : str
        side : str
            ``"YES"`` or ``"NO"`` — side of the **new** trade.
        size : float
            Number of contracts filled.
        price : float
            Effective fill price **in this trade's denomination**:
            YES price for YES trades, NO price for NO trades.
        fee : float
            Total fee paid for this fill.
        strategy : str
            Name of the strategy that opened the position.  Used to
            attribute exposure per strategy.
        """
        if size <= 0 or price <= 0:
            return

        pos = self._positions.get(market_id)

        if pos is None:
            self._positions[market_id] = {
                "market_id": market_id,
                "side": side,
                "size": size,
                "average_entry": price,
                "current_price": price,
                "realised_pnl": -fee,
                "unrealised_pnl": 0.0,
                "strategy": strategy,
            }
            self._total_realised_pnl -= fee
            return

        if pos["side"] == side:
            self._add_to_position(pos, size, price, fee)
        else:
            self._reduce_position(pos, size, price, fee, side)

    def close_position(self, market_id: str, price: float) -> None:
        """Fully close a position at *price* and realise its P&L.

        *price* is the effective fill price of the **opposite**-side
        trade that closes the position (e.g. the NO price when closing
        a YES position).
        """
        pos = self._positions.get(market_id)
        if pos is None or pos["size"] <= 0:
            return
        opposite = "YES" if pos["side"] == "NO" else "NO"
        self._reduce_position(pos, pos["size"], price, fee=0.0, new_side=opposite)

    def update_price(self, market_id: str, current_price: float) -> None:
        """Mark a position to market.

        *current_price* is the **YES** price of the market.
        It is automatically converted to the position's denomination.
        """
        pos = self._positions.get(market_id)
        if pos is None or pos["size"] <= 0:
            return
        if pos["side"] == "NO":
            pos["current_price"] = 1.0 - current_price
        else:
            pos["current_price"] = current_price
        pos["unrealised_pnl"] = self._calculate_unrealised(pos)

    # ── Query ──────────────────────────────────────────────────────

    def position_for(self, market_id: str) -> dict[str, Any] | None:
        """Return a copy of the position dict, or ``None``."""
        pos = self._positions.get(market_id)
        return copy.deepcopy(pos) if pos else None

    def total_exposure(self) -> float:
        """Sum of absolute position sizes."""
        return sum(float(p.get("size", 0)) for p in self._positions.values())

    def market_exposure(self, market_id: str) -> float:
        """Exposure to a single market by ID."""
        pos = self._positions.get(market_id)
        return float(pos["size"]) if pos else 0.0

    def strategy_exposure(self, strategy: str) -> float:
        """Total position size opened by a strategy.

        Positions opened without a strategy label (``""``) are
        counted under that empty label only.
        """
        return sum(
            float(p.get("size", 0))
            for p in self._positions.values()
            if p.get("strategy", "") == strategy
        )

    def exposure_for(self, market_ids: Iterable[str]) -> float:
        """Total position size across a set of markets.

        Used to aggregate correlated exposure: markets that depend on
        the same underlying event, or markets that resolve at the same
        time, are passed together so their sizes sum into one bucket.
        Duplicate market ids are naturally counted once (dict lookup).
        """
        return sum(
            float(p.get("size", 0))
            for market_id in set(market_ids)
            if (p := self._positions.get(market_id)) is not None
        )

    def directional_exposure(
        self, market_directions: Mapping[str, float]
    ) -> float:
        """Signed exposure to an underlying event.

        Each market maps to a direction: ``+1`` when YES on that
        market means the event occurs, ``-1`` when YES means the
        event does NOT occur.  A YES position contributes
        ``+direction``, a NO position ``-direction``.  Offsetting
        positions (e.g. YES on the ``+1`` market and YES on the
        ``-1`` market) therefore cancel out as hedges.
        """
        total = 0.0
        for market_id, direction in market_directions.items():
            pos = self._positions.get(market_id)
            if pos is None:
                continue
            side_sign = 1.0 if pos.get("side") == "YES" else -1.0
            total += direction * side_sign * float(pos.get("size", 0))
        return total

    def num_positions(self) -> int:
        """Number of open positions (size > 0)."""
        return sum(1 for p in self._positions.values() if float(p.get("size", 0)) > 0)

    def total_realised_pnl(self) -> float:
        """Total realised P&L across all trades (persists after close)."""
        return self._total_realised_pnl

    def total_unrealised_pnl(self) -> float:
        return sum(float(p.get("unrealised_pnl", 0)) for p in self._positions.values())

    def total_pnl(self) -> float:
        return self.total_realised_pnl() + self.total_unrealised_pnl()

    # ── Legacy compat ──────────────────────────────────────────────

    def update_position(self, position: dict[str, Any]) -> None:
        """Legacy — update or add a position dict directly."""
        self._positions[position["market_id"]] = position

    def unrealised_pnl(self) -> float:
        return self.total_unrealised_pnl()

    def realised_pnl(self) -> float:
        return self.total_realised_pnl()

    def summary(self) -> dict[str, Any]:
        return {
            "equity": self.equity,
            "total_exposure": self.total_exposure(),
            "unrealised_pnl": self.total_unrealised_pnl(),
            "realised_pnl": self.total_realised_pnl(),
            "total_pnl": self.total_pnl(),
            "open_positions": self.num_positions(),
            "positions": dict(self._positions),
        }

    # ── Private helpers ────────────────────────────────────────────

    def _add_to_position(
        self, pos: dict[str, Any], size: float, price: float, fee: float
    ) -> None:
        total_size = pos["size"] + size
        total_cost = pos["size"] * pos["average_entry"] + size * price
        pos["average_entry"] = total_cost / total_size if total_size > 0 else price
        pos["size"] = total_size
        pos["current_price"] = price
        pos["unrealised_pnl"] = self._calculate_unrealised(pos)
        pos["realised_pnl"] -= fee
        self._total_realised_pnl -= fee

    def _reduce_position(
        self, pos: dict[str, Any], size: float, price: float, fee: float, new_side: str
    ) -> None:
        close_size = min(size, pos["size"])
        entry = pos["average_entry"]

        # Opposite-side trade: the effective exit price for this position
        # is (1 - new_trade_price) because YES + NO = 1.
        realised = close_size * ((1.0 - price) - entry) - fee

        pos["size"] -= close_size
        pos["realised_pnl"] += realised
        self._total_realised_pnl += realised

        remaining = size - close_size
        if remaining > 0:
            # Flip: closing order exceeded the position size
            self._positions[pos["market_id"]] = {
                "market_id": pos["market_id"],
                "side": new_side,
                "size": remaining,
                "average_entry": price,
                "current_price": price,
                "realised_pnl": pos.get("realised_pnl", 0.0),
                "unrealised_pnl": 0.0,
                "strategy": pos.get("strategy", ""),
            }
            return

        if pos["size"] <= 0:
            del self._positions[pos["market_id"]]
        else:
            pos["current_price"] = 1.0 - price
            pos["unrealised_pnl"] = self._calculate_unrealised(pos)

    @staticmethod
    def _calculate_unrealised(pos: dict[str, Any]) -> float:
        size = float(pos["size"])
        if size <= 0:
            return 0.0
        return size * (float(pos["current_price"]) - float(pos["average_entry"]))
