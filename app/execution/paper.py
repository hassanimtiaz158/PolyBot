"""Paper execution adapter — simulates fills with spread, slippage,
latency, partial fills, rejection, cancellation, and price movement.

Integrates with ``PortfolioTracker`` for automatic position/P&L updates
and with ``OrderRepository`` for optional database persistence.

Simulation model
----------------
- **Spread**: buys fill at ask, sells fill at bid (never midpoint).
- **Slippage**: when size exceeds visible depth, additional contracts
  fill at progressively worse prices.
- **Partial fills**: random probability based on size/depth ratio.
- **Fees**: Polymarket taker fee formula ``Θ × C × p × (1-p)``.
- **Rejection**: configurable random rejection rate.
- **Latency**: configurable simulated delay.
- **Price impact**: large fills shift the market price.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any

from app.execution.interface import ExecutionAdapter
from app.portfolio.tracker import PortfolioTracker
from app.storage.models import Order as OrderModel
from app.storage.repositories import OrderRepository

logger = logging.getLogger(__name__)


class PaperExecution(ExecutionAdapter):
    """Simulates order execution for paper trading.

    Parameters
    ----------
    portfolio : PortfolioTracker | None
        When provided, trades automatically update positions and P&L.
    order_repo : OrderRepository | None
        When provided, all orders are persisted to the database.
    rejection_rate : float
        Probability (0–1) of random rejection (default 0.01).
    latency_ms : float
        Simulated submission latency in ms (default 200).
    fee_rate : float
        Taker fee coefficient Θ (default 0.05).
    seed : int | None
        Random seed for deterministic simulations.
    slippage_model : str
        ``"linear"`` (default) or ``"sqrt"`` — how slippage scales.
    price_impact_pct : float
        Max fractional price shift from a large fill (default 0.02).
    """

    def __init__(
        self,
        portfolio: PortfolioTracker | None = None,
        order_repo: OrderRepository | None = None,
        rejection_rate: float = 0.01,
        latency_ms: float = 200.0,
        fee_rate: float = 0.05,
        seed: int | None = None,
        slippage_model: str = "linear",
        price_impact_pct: float = 0.02,
    ) -> None:
        if not 0.0 <= rejection_rate <= 1.0:
            raise ValueError("rejection_rate must be in [0, 1]")
        if not 0.0 <= price_impact_pct <= 0.10:
            raise ValueError("price_impact_pct must be in [0, 0.10]")
        if slippage_model not in ("linear", "sqrt"):
            raise ValueError("slippage_model must be 'linear' or 'sqrt'")

        self._portfolio = portfolio
        self._order_repo = order_repo
        self._rejection_rate = rejection_rate
        self._latency_ms = max(0.0, latency_ms)
        self._fee_rate = fee_rate
        self._slippage_model = slippage_model
        self._price_impact_pct = price_impact_pct
        self._rng = random.Random(seed)
        self._orders: dict[str, dict[str, Any]] = {}
        self._market_prices: dict[str, float] = {}

    # ── Public API (ExecutionAdapter) ───────────────────────────────

    async def submit(self, order: dict[str, Any]) -> dict[str, Any]:
        """Simulate an order submission.

        Parameters
        ----------
        order : dict
            Keys: ``order_id``, ``market_id``, ``side``, ``size``,
            ``price`` (midpoint), ``spread``, ``depth``, ``signal_id``.

        Returns
        -------
        dict
            ``order_id``, ``status``, ``filled_size``, ``average_fill``,
            ``realised_pnl``, ``error``.
        """
        order_id = str(order.get("order_id", "unknown"))
        market_id = str(order.get("market_id", "unknown"))
        side = str(order.get("side", "YES")).upper()
        size = float(order.get("size", 0))
        price = float(order.get("price", 0.50))
        spread = float(order.get("spread", 0.02))
        depth = float(order.get("depth", max(size * 10, 100.0)))

        # ── Validation ──────────────────────────────────────────────
        if not order_id or order_id == "unknown":
            return self._reject(
                order_id, market_id, side, size, "Missing order_id"
            )
        if order_id in self._orders:
            existing = self._orders[order_id]
            return dict(existing)
        if side not in ("YES", "NO"):
            return self._reject(
                order_id, market_id, side, size, f"Invalid side: {side}"
            )
        if size <= 0:
            return self._reject(
                order_id, market_id, side, size, "Invalid size"
            )

        # ── Simulate latency ────────────────────────────────────────
        if self._latency_ms > 0:
            import asyncio
            await asyncio.sleep(self._latency_ms / 1000.0)

        # ── Random rejection ────────────────────────────────────────
        if self._rng.random() < self._rejection_rate:
            result = self._build_result(
                order_id, market_id, side, size,
                status="REJECTED",
                filled_size=0.0,
                error="Simulated random rejection",
            )
            self._orders[order_id] = result
            await self._persist_order(result)
            return result

        # ── Track market price ──────────────────────────────────────
        self._market_prices.setdefault(market_id, price)

        # ── Fill price (buy at ask, sell at bid) ────────────────────
        half_spread = spread / 2.0
        if side == "YES":
            raw_fill_price = self._market_prices[market_id] + half_spread
        else:
            raw_fill_price = self._market_prices[market_id] - half_spread

        # ── Slippage ────────────────────────────────────────────────
        ratio = size / max(depth, 1.0)
        if ratio > 1.0:
            excess = ratio - 1.0
            if self._slippage_model == "sqrt":
                slippage = (excess**0.5) * half_spread
            else:
                slippage = excess * half_spread * 0.5
            raw_fill_price += slippage if side == "YES" else -slippage

        raw_fill_price = max(0.01, min(0.99, raw_fill_price))

        # ── Partial fill ────────────────────────────────────────────
        fill_ratio = self._simulate_fill_ratio(size, depth)
        filled_size = size * fill_ratio

        if fill_ratio <= 0:
            result = self._build_result(
                order_id, market_id, side, size,
                status="REJECTED",
                filled_size=0.0,
                error="No fill simulated",
            )
            self._orders[order_id] = result
            await self._persist_order(result)
            return result

        # ── Fee calculation ─────────────────────────────────────────
        fee_per_share = self._fee_rate * raw_fill_price * (1.0 - raw_fill_price)
        total_fee = fee_per_share * filled_size

        if side == "YES":
            effective_fill = raw_fill_price + fee_per_share
        else:
            effective_fill = raw_fill_price - fee_per_share
        effective_fill = max(0.01, min(0.99, effective_fill))

        # ── Price impact ────────────────────────────────────────────
        if fill_ratio > 1.0:
            impact = self._price_impact_pct * (fill_ratio - 1.0) * price
            if side == "YES":
                self._market_prices[market_id] += impact
            else:
                self._market_prices[market_id] -= impact
            self._market_prices[market_id] = max(
                0.01, min(0.99, self._market_prices[market_id])
            )

        # ── Update portfolio ────────────────────────────────────────
        realised_pnl = 0.0
        if self._portfolio is not None:
            self._portfolio.add_trade(
                market_id=market_id,
                side=side,
                size=filled_size,
                price=effective_fill,
                fee=total_fee,
            )
            realised_pnl = self._portfolio.total_realised_pnl()

        # ── Build result ────────────────────────────────────────────
        status = "FILLED" if fill_ratio >= 0.999 else "PARTIALLY_FILLED"
        result = self._build_result(
            order_id=order_id,
            market_id=market_id,
            side=side,
            requested_size=size,
            status=status,
            filled_size=filled_size,
            average_fill=effective_fill,
            fee=total_fee,
            realised_pnl=realised_pnl,
            extra={
                "raw_fill_price": raw_fill_price,
                "fee_per_share": fee_per_share,
                "fill_ratio": fill_ratio,
                "spread": spread,
                "slippage": raw_fill_price - (
                    self._market_prices.get(market_id, price)
                    + half_spread * (1 if side == "YES" else -1)
                ),
            },
        )
        self._orders[order_id] = result
        await self._persist_order(result)
        return result

    async def cancel(self, order_id: str) -> bool:
        """Simulate cancellation — marks as CANCELLED if not rejected."""
        existing = self._orders.get(order_id)
        if existing is None or existing.get("status") in ("REJECTED", "CANCELLED"):
            return False
        existing["status"] = "CANCELLED"
        existing["error"] = "Cancelled by operator"
        await self._persist_order(existing)
        return True

    async def status(self, order_id: str) -> dict[str, Any]:
        """Return current simulated status of an order."""
        return dict(self._orders.get(
            order_id,
            {"order_id": order_id, "status": "NOT_FOUND"},
        ))

    # ── State management ───────────────────────────────────────────

    def list_orders(self) -> dict[str, dict[str, Any]]:
        """Return all tracked orders (read-only snapshot)."""
        return dict(self._orders)

    def clear_orders(self) -> None:
        """Clear all in-memory orders."""
        self._orders.clear()

    def market_price(self, market_id: str) -> float | None:
        """Return the tracked market price for *market_id*."""
        return self._market_prices.get(market_id)

    def set_market_price(self, market_id: str, price: float) -> None:
        """Manually set a tracked market price."""
        self._market_prices[market_id] = max(0.01, min(0.99, price))

    # ── Private helpers ────────────────────────────────────────────

    def _simulate_fill_ratio(self, size: float, depth: float) -> float:
        if depth <= 0 or size <= 0:
            return 0.0
        ratio = size / depth
        if ratio <= 1.0:
            return 1.0 if self._rng.random() < 0.95 else 0.98
        partial_prob = min(0.5, 0.1 * (ratio - 1.0))
        if self._rng.random() < partial_prob:
            return self._rng.uniform(0.5, min(0.95, 1.0 / ratio))
        return 1.0

    def _reject(
        self,
        order_id: str,
        market_id: str,
        side: str,
        size: float,
        error: str,
    ) -> dict[str, Any]:
        result = self._build_result(
            order_id=order_id,
            market_id=market_id,
            side=side,
            requested_size=size,
            status="REJECTED",
            filled_size=0.0,
            error=error,
        )
        self._orders[order_id] = result
        return result

    def _build_result(
        self,
        order_id: str,
        market_id: str,
        side: str,
        requested_size: float,
        status: str = "REJECTED",
        filled_size: float = 0.0,
        average_fill: float | None = None,
        fee: float = 0.0,
        realised_pnl: float = 0.0,
        error: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "order_id": order_id,
            "market_id": market_id,
            "side": side,
            "status": status,
            "requested_size": requested_size,
            "filled_size": filled_size,
            "average_fill": average_fill,
            "fee": fee,
            "realised_pnl": realised_pnl,
            "error": error,
            "timestamp": time.time(),
            **(extra or {}),
        }

    async def _persist_order(self, result: dict[str, Any]) -> None:
        if self._order_repo is None:
            return
        try:
            await self._order_repo.insert(
                OrderModel(
                    order_id=str(result.get("order_id", "")),
                    market_id=str(result.get("market_id", "")),
                    side=str(result.get("side", "")),
                    status=str(result.get("status", "UNKNOWN")),
                    requested_price=result.get("average_fill"),
                    requested_size=float(result.get("requested_size", 0)),
                    filled_size=float(result.get("filled_size", 0)),
                    average_fill=result.get("average_fill"),
                )
            )
        except Exception:
            logger.exception("Failed to persist order %s", result.get("order_id"))
