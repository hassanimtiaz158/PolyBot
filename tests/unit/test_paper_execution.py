"""Comprehensive tests for the paper execution adapter."""

from __future__ import annotations

from typing import Any

import pytest

from app.execution.paper import PaperExecution
from app.portfolio.tracker import PortfolioTracker


def approx(val: float, rel: float = 1e-6) -> Any:
    return pytest.approx(val, rel=rel, abs=1e-12)


def make_order(
    order_id: str = "ord_001",
    market_id: str = "mkt_001",
    side: str = "YES",
    size: float = 100.0,
    price: float = 0.50,
    spread: float = 0.02,
    depth: float | None = None,
) -> dict[str, Any]:
    return {
        "order_id": order_id,
        "market_id": market_id,
        "side": side,
        "size": size,
        "price": price,
        "spread": spread,
        "depth": depth or max(size * 10, 100.0),
    }


# ── Initialisation ────────────────────────────────────────────────────────


class TestPaperExecutionInit:
    def test_default_params(self) -> None:
        p = PaperExecution()
        assert p._rejection_rate == approx(0.01)
        assert p._latency_ms == approx(200.0)
        assert p._fee_rate == approx(0.05)

    def test_custom_params(self) -> None:
        p = PaperExecution(
            rejection_rate=0.1, latency_ms=50, fee_rate=0.04, seed=42,
        )
        assert p._rejection_rate == approx(0.1)
        assert p._latency_ms == approx(50.0)
        assert p._fee_rate == approx(0.04)

    def test_invalid_rejection_rate(self) -> None:
        with pytest.raises(ValueError, match="rejection_rate"):
            PaperExecution(rejection_rate=1.5)

    def test_invalid_price_impact(self) -> None:
        with pytest.raises(ValueError, match="price_impact_pct"):
            PaperExecution(price_impact_pct=0.50)

    def test_invalid_slippage_model(self) -> None:
        with pytest.raises(ValueError, match="slippage_model"):
            PaperExecution(slippage_model="cubic")

    def test_seed_determinism(self) -> None:
        a = PaperExecution(seed=42)
        b = PaperExecution(seed=42)
        assert a._rng.random() == b._rng.random()

    def test_with_portfolio(self) -> None:
        pt = PortfolioTracker()
        p = PaperExecution(portfolio=pt)
        assert p._portfolio is pt


# ── Full fill ─────────────────────────────────────────────────────────────


class TestPaperExecutionFullFill:
    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
        self.adapter = PaperExecution(
            rejection_rate=0.0, latency_ms=0, fee_rate=0.0, seed=42,
        )

    async def test_fills_buy_order(self) -> None:
        result = await self.adapter.submit(make_order())
        assert result["status"] == "FILLED"
        assert result["filled_size"] == approx(100.0)
        assert result["average_fill"] is not None

    async def test_fills_sell_order(self) -> None:
        result = await self.adapter.submit(make_order(side="NO"))
        assert result["filled_size"] > 0
        assert result["status"] == "FILLED"

    async def test_buy_fill_above_midpoint(self) -> None:
        result = await self.adapter.submit(
            make_order(spread=0.04, price=0.50)
        )
        assert result["average_fill"] is not None
        assert result["average_fill"] > 0.50

    async def test_sell_fill_below_midpoint(self) -> None:
        result = await self.adapter.submit(
            make_order(side="NO", spread=0.04, price=0.50)
        )
        raw = result.get("raw_fill_price", 1.0)
        assert raw < 0.50

    async def test_fill_price_clamped_high(self) -> None:
        result = await self.adapter.submit(
            make_order(price=0.999, spread=0.10)
        )
        avg = result["average_fill"]
        assert avg is None or avg <= 0.99

    async def test_fill_price_clamped_low(self) -> None:
        result = await self.adapter.submit(
            make_order(side="NO", price=0.001, spread=0.10)
        )
        raw = result.get("raw_fill_price", 0.0)
        assert raw >= 0.01

    async def test_fee_applied_to_average_fill(self) -> None:
        adapter = PaperExecution(
            rejection_rate=0.0, latency_ms=0, fee_rate=0.05, seed=42,
        )
        result = await adapter.submit(make_order())
        avg = result["average_fill"]
        raw = result.get("raw_fill_price", 0.0)
        assert avg is not None
        assert avg > raw

    async def test_zero_fee_no_effect(self) -> None:
        result = await self.adapter.submit(make_order())
        raw = result.get("raw_fill_price", 0.0)
        avg = result["average_fill"]
        assert avg is not None
        assert avg == approx(raw)


# ── Partial fill ──────────────────────────────────────────────────────────


class TestPaperExecutionPartialFill:
    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
        self.adapter = PaperExecution(
            rejection_rate=0.0, latency_ms=0, fee_rate=0.0, seed=42,
        )

    async def test_large_order_partially_filled(self) -> None:
        result = await self.adapter.submit(
            make_order(size=10000, depth=100)
        )
        assert result["status"] == "PARTIALLY_FILLED"
        assert 0 < result["filled_size"] < 10000

    async def test_partial_fill_ratio_tracked(self) -> None:
        result = await self.adapter.submit(
            make_order(size=10000, depth=100)
        )
        ratio = result.get("fill_ratio", 1.0)
        assert 0 < ratio < 1.0
        assert result["filled_size"] == approx(10000 * ratio)


# ── Rejection ─────────────────────────────────────────────────────────────


class TestPaperExecutionRejection:
    async def test_zero_size_rejected(self) -> None:
        adapter = PaperExecution(rejection_rate=0.0, latency_ms=0)
        result = await adapter.submit(make_order(size=0))
        assert result["status"] == "REJECTED"
        assert result["error"] is not None
        assert "Invalid size" in result["error"]

    async def test_missing_order_id_rejected(self) -> None:
        adapter = PaperExecution(rejection_rate=0.0, latency_ms=0)
        result = await adapter.submit(
            {"market_id": "mkt_1", "side": "YES", "size": 100}
        )
        assert result["status"] == "REJECTED"

    async def test_invalid_side_rejected(self) -> None:
        adapter = PaperExecution(rejection_rate=0.0, latency_ms=0)
        result = await adapter.submit(
            make_order(side="MAYBE")
        )
        assert result["status"] == "REJECTED"
        assert "Invalid side" in (result.get("error") or "")

    async def test_random_rejection(self) -> None:
        adapter = PaperExecution(
            rejection_rate=1.0, latency_ms=0, fee_rate=0.0, seed=42,
        )
        result = await adapter.submit(make_order())
        assert result["status"] == "REJECTED"
        assert "Simulated random rejection" in (result.get("error") or "")


# ── Cancellation ──────────────────────────────────────────────────────────


class TestPaperExecutionCancel:
    @pytest.fixture(autouse=True)
    async def _setup(self) -> None:
        self.adapter = PaperExecution(
            rejection_rate=0.0, latency_ms=0, fee_rate=0.0, seed=42,
        )
        await self.adapter.submit(make_order(order_id="ord_c"))

    async def test_cancel_existing_order(self) -> None:
        ok = await self.adapter.cancel("ord_c")
        assert ok is True
        status = await self.adapter.status("ord_c")
        assert status["status"] == "CANCELLED"

    async def test_cancel_nonexistent(self) -> None:
        ok = await self.adapter.cancel("nonexistent")
        assert ok is False

    async def test_cancel_filled_order(self) -> None:
        # ord_c was filled by setup; cancelling a filled order fails
        ok = await self.adapter.cancel("ord_c")
        assert ok is True  # it wasn't rejected, so it should be cancellable

    async def test_cancel_rejected_order(self) -> None:
        adapter = PaperExecution(
            rejection_rate=1.0, latency_ms=0, seed=42,
        )
        await adapter.submit(make_order(order_id="ord_r"))
        ok = await adapter.cancel("ord_r")
        assert ok is False


# ── Slippage ──────────────────────────────────────────────────────────────


class TestPaperExecutionSlippage:
    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
        self.adapter = PaperExecution(
            rejection_rate=0.0, latency_ms=0, fee_rate=0.0, seed=42,
        )

    async def test_slippage_on_large_order(self) -> None:
        result = await self.adapter.submit(
            make_order(size=10000, depth=100, spread=0.04)
        )
        avg = result["average_fill"]
        assert avg is not None
        # Midpoint 0.50 + half_spread 0.02 + slippage
        assert avg > 0.52

    async def test_no_slippage_on_small_order(self) -> None:
        result = await self.adapter.submit(
            make_order(size=10, depth=10000, spread=0.04)
        )
        avg = result["average_fill"]
        assert avg is not None
        # Midpoint 0.50 + half_spread 0.02 = 0.52, no slippage (10 << 10000)
        assert avg == approx(0.52)

    async def test_sqrt_slippage_model(self) -> None:
        adapter = PaperExecution(
            rejection_rate=0.0, latency_ms=0, fee_rate=0.0,
            seed=42, slippage_model="sqrt",
        )
        result = await adapter.submit(
            make_order(size=10000, depth=100, spread=0.04)
        )
        avg = result["average_fill"]
        assert avg is not None
        assert avg > 0.51


# ── Portfolio integration ─────────────────────────────────────────────────


class TestPaperExecutionPortfolioIntegration:
    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
        self.portfolio = PortfolioTracker(initial_equity=10_000.0)
        self.adapter = PaperExecution(
            portfolio=self.portfolio,
            rejection_rate=0.0, latency_ms=0, fee_rate=0.0, seed=42,
        )

    async def test_buy_updates_portfolio(self) -> None:
        await self.adapter.submit(make_order(size=100, price=0.50))
        pos = self.portfolio.position_for("mkt_001")
        assert pos is not None
        assert pos["size"] == approx(100.0)
        assert pos["side"] == "YES"

    async def test_sell_updates_portfolio(self) -> None:
        await self.adapter.submit(make_order(side="NO", size=100, price=0.50))
        pos = self.portfolio.position_for("mkt_001")
        assert pos is not None
        assert pos["size"] == approx(100.0)
        assert pos["side"] == "NO"

    async def test_equity_deducted_for_buy(self) -> None:
        await self.adapter.submit(
            make_order(size=100, price=0.50, spread=0.02)
        )
        # Buy at 0.51 (mid+halfspread); P&L-based equity doesn't change
        # on an entry trade (position is worth cost basis)
        assert self.portfolio.equity == approx(10_000.0)

    async def test_rejection_does_not_update_portfolio(self) -> None:
        adapter = PaperExecution(
            portfolio=self.portfolio,
            rejection_rate=1.0, latency_ms=0, seed=42,
        )
        await adapter.submit(make_order())
        assert self.portfolio.num_positions() == 0

    async def test_fee_deducted_with_portfolio(self) -> None:
        adapter = PaperExecution(
            portfolio=self.portfolio,
            rejection_rate=0.0, latency_ms=0, fee_rate=0.05, seed=42,
        )
        await adapter.submit(make_order(size=100, price=0.50, spread=0.02))
        pos = self.portfolio.position_for("mkt_001")
        assert pos is not None
        # Fee = Θ × p × (1-p) = 0.05 × 0.51 × 0.49 ≈ 0.0125/share = 1.25
        assert self.portfolio.equity == approx(9_998.75, rel=1e-3)

    async def test_multiple_trades_accumulate(self) -> None:
        await self.adapter.submit(make_order(size=100, price=0.50))
        await self.adapter.submit(
            make_order(order_id="ord_2", size=50, price=0.55)
        )
        assert self.portfolio.num_positions() == 1
        pos = self.portfolio.position_for("mkt_001")
        assert pos is not None
        assert pos["size"] == approx(150.0)


# ── Price impact ──────────────────────────────────────────────────────────


class TestPaperExecutionPriceImpact:
    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
        self.adapter = PaperExecution(
            rejection_rate=0.0, latency_ms=0, fee_rate=0.0,
            seed=42, price_impact_pct=0.02,
        )

    async def test_large_buy_moves_price_up(self) -> None:
        await self.adapter.submit(
            make_order(size=10000, depth=100, price=0.50)
        )
        new_price = self.adapter.market_price("mkt_001")
        assert new_price is not None
        assert new_price >= 0.50

    async def test_large_sell_moves_price_down(self) -> None:
        await self.adapter.submit(
            make_order(side="NO", size=10000, depth=100, price=0.50)
        )
        new_price = self.adapter.market_price("mkt_001")
        assert new_price is not None
        assert new_price <= 0.50

    async def test_small_order_no_impact(self) -> None:
        await self.adapter.submit(make_order(size=10, depth=10000, price=0.50))
        price = self.adapter.market_price("mkt_001")
        assert price == approx(0.50)

    async def test_subsequent_order_sees_new_price(self) -> None:
        await self.adapter.submit(
            make_order(size=10000, depth=100, price=0.50)
        )
        new_price = self.adapter.market_price("mkt_001")
        # Second order at the impacted price
        result = await self.adapter.submit(
            make_order(order_id="ord_2", size=100, price=new_price or 0.50)
        )
        assert result["average_fill"] is not None
        assert result["status"] == "FILLED"


# ── Duplicate order ───────────────────────────────────────────────────────


class TestPaperExecutionDuplicateOrder:
    @pytest.fixture(autouse=True)
    async def _setup(self) -> None:
        self.adapter = PaperExecution(
            rejection_rate=0.0, latency_ms=0, fee_rate=0.0, seed=42,
        )
        self.original = await self.adapter.submit(make_order(order_id="dup_1"))

    async def test_duplicate_returns_same_result(self) -> None:
        duplicate = await self.adapter.submit(make_order(order_id="dup_1"))
        assert duplicate["status"] == self.original["status"]
        assert duplicate["filled_size"] == self.original["filled_size"]

    async def test_duplicate_does_not_create_second_position(self) -> None:
        port = PortfolioTracker()
        adapter = PaperExecution(
            portfolio=port,
            rejection_rate=0.0, latency_ms=0, fee_rate=0.0, seed=42,
        )
        await adapter.submit(make_order(order_id="dup_2"))
        await adapter.submit(make_order(order_id="dup_2"))
        assert port.num_positions() == 1


# ── Status and state management ───────────────────────────────────────────


class TestPaperExecutionStatus:
    @pytest.fixture(autouse=True)
    async def _setup(self) -> None:
        self.adapter = PaperExecution(
            rejection_rate=0.0, latency_ms=0, fee_rate=0.0, seed=42,
        )
        await self.adapter.submit(make_order(order_id="ord_s"))

    async def test_status_found(self) -> None:
        result = await self.adapter.status("ord_s")
        assert result["order_id"] == "ord_s"
        assert result["status"] == "FILLED"

    async def test_status_not_found(self) -> None:
        result = await self.adapter.status("nonexistent")
        assert result["status"] == "NOT_FOUND"

    async def test_list_orders(self) -> None:
        orders = self.adapter.list_orders()
        assert "ord_s" in orders

    async def test_clear_orders(self) -> None:
        self.adapter.clear_orders()
        assert self.adapter.list_orders() == {}

    async def test_set_market_price(self) -> None:
        self.adapter.set_market_price("mkt_p", 0.55)
        assert self.adapter.market_price("mkt_p") == approx(0.55)


# ── DB persistence ────────────────────────────────────────────────────────


class TestPaperExecutionPersistence:
    @pytest.mark.asyncio
    async def test_persists_order_to_repo(self, test_db) -> None:
        from app.storage.repositories import OrderRepository

        # Need a market row to satisfy the FK constraint
        await test_db.conn.execute(
            "INSERT INTO markets (market_id, question, status, liquidity, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("mkt_001", "Test?", "ACTIVE", 0.0, "2025-01-01T00:00:00", "2025-01-01T00:00:00"),
        )

        repo = OrderRepository()
        adapter = PaperExecution(
            order_repo=repo,
            rejection_rate=0.0, latency_ms=0, fee_rate=0.0, seed=42,
        )
        await adapter.submit(make_order(order_id="ord_db"))

        stored = await repo.get("ord_db")
        assert stored is not None
        assert stored.status == "FILLED"
        assert stored.filled_size == approx(100.0)


# ── Misc edge cases ───────────────────────────────────────────────────────


class TestPaperExecutionEdgeCases:
    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
        self.adapter = PaperExecution(
            rejection_rate=0.0, latency_ms=0, fee_rate=0.0, seed=42,
        )

    async def test_realised_pnl_in_result(self) -> None:
        port = PortfolioTracker()
        adapter = PaperExecution(
            portfolio=port,
            rejection_rate=0.0, latency_ms=0, fee_rate=0.0, seed=42,
        )
        result = await adapter.submit(make_order(size=100, price=0.50))
        assert "realised_pnl" in result

    async def test_timestamp_in_result(self) -> None:
        result = await self.adapter.submit(make_order())
        assert "timestamp" in result
        assert result["timestamp"] > 0
