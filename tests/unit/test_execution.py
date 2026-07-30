"""Unit tests for execution engine, paper adapter, state machine, and order lifecycle."""

from typing import Any

import pytest

from app.execution.engine import ExecutionEngine, OrderRequest, OrderResult
from app.execution.interface import ExecutionAdapter
from app.execution.paper import PaperExecution
from app.execution.state_machine import OrderState, OrderStateMachine
from app.risk.engine import RiskDecision


def approx(val: float, rel: float = 1e-6) -> Any:
    return pytest.approx(val, rel=rel, abs=1e-12)


# ── OrderStateMachine ────────────────────────────────────────────────────


class TestOrderStateMachine:
    def test_initial_state(self) -> None:
        sm = OrderStateMachine()
        assert sm.state == OrderState.CREATED

    def test_initial_state_custom(self) -> None:
        sm = OrderStateMachine(OrderState.SUBMITTED)
        assert sm.state == OrderState.SUBMITTED

    def test_valid_transition_created_to_risk_approved(self) -> None:
        sm = OrderStateMachine()
        sm.transition(OrderState.RISK_APPROVED)
        assert sm.state == OrderState.RISK_APPROVED

    def test_valid_transition_full_path(self) -> None:
        sm = OrderStateMachine()
        sm.transition(OrderState.RISK_APPROVED)
        sm.transition(OrderState.SUBMITTED)
        sm.transition(OrderState.FILLED)
        assert sm.state == OrderState.FILLED

    def test_invalid_transition_raises(self) -> None:
        sm = OrderStateMachine()
        with pytest.raises(ValueError, match="Invalid transition"):
            sm.transition(OrderState.FILLED)

    def test_can_transition_valid(self) -> None:
        sm = OrderStateMachine()
        assert sm.can_transition(OrderState.RISK_APPROVED) is True
        assert sm.can_transition(OrderState.FILLED) is False

    def test_is_terminal_rejected(self) -> None:
        sm = OrderStateMachine()
        sm.transition(OrderState.REJECTED)
        assert sm.is_terminal() is True

    def test_is_terminal_filled(self) -> None:
        sm = OrderStateMachine()
        sm.transition(OrderState.RISK_APPROVED)
        sm.transition(OrderState.SUBMITTED)
        sm.transition(OrderState.FILLED)
        assert sm.is_terminal() is True

    def test_is_terminal_cancelled(self) -> None:
        sm = OrderStateMachine()
        sm.transition(OrderState.RISK_APPROVED)
        sm.transition(OrderState.SUBMITTED)
        sm.transition(OrderState.CANCELLED)
        assert sm.is_terminal() is True

    def test_is_not_terminal(self) -> None:
        sm = OrderStateMachine()
        assert sm.is_terminal() is False

    def test_partial_fill_allowed_from_submitted(self) -> None:
        sm = OrderStateMachine()
        sm.transition(OrderState.RISK_APPROVED)
        sm.transition(OrderState.SUBMITTED)
        sm.transition(OrderState.PARTIALLY_FILLED)
        assert sm.state == OrderState.PARTIALLY_FILLED

    def test_partial_fill_to_filled(self) -> None:
        sm = OrderStateMachine()
        sm.transition(OrderState.RISK_APPROVED)
        sm.transition(OrderState.SUBMITTED)
        sm.transition(OrderState.PARTIALLY_FILLED)
        sm.transition(OrderState.FILLED)
        assert sm.state == OrderState.FILLED


# ── OrderRequest dataclass ──────────────────────────────────────────────


class TestOrderRequest:
    def test_minimal(self) -> None:
        req = OrderRequest(market_id="mkt_1", side="YES", size=100, price=0.50)
        assert req.market_id == "mkt_1"
        assert req.side == "YES"
        assert req.size == approx(100.0)
        assert req.price == approx(0.50)

    def test_defaults(self) -> None:
        req = OrderRequest(market_id="mkt_1", side="NO", size=50, price=0.30)
        assert req.order_type == "MARKET"
        assert req.signal_id == ""
        assert req.extra == {}

    def test_full(self) -> None:
        req = OrderRequest(
            market_id="mkt_1", side="YES", size=100, price=0.50,
            signal_id="sig_1", order_type="LIMIT",
            extra={"source": "test"},
        )
        assert req.signal_id == "sig_1"
        assert req.order_type == "LIMIT"
        assert req.extra == {"source": "test"}


# ── OrderResult dataclass ────────────────────────────────────────────────


class TestOrderResult:
    def test_minimal(self) -> None:
        res = OrderResult(
            order_id="ord_1", market_id="mkt_1", side="YES",
            status="FILLED", requested_size=100.0,
        )
        assert res.filled_size == 0.0
        assert res.average_fill is None
        assert res.error is None

    def test_full(self) -> None:
        res = OrderResult(
            order_id="ord_1", market_id="mkt_1", side="YES",
            status="FILLED", requested_size=100.0,
            filled_size=100.0, average_fill=0.55,
            error=None, extra={"key": "val"},
        )
        assert res.filled_size == approx(100.0)
        assert res.average_fill == approx(0.55)
        assert res.extra == {"key": "val"}


# ── PaperExecution ──────────────────────────────────────────────────────


class TestPaperExecutionInit:
    def test_default_params(self) -> None:
        adapter = PaperExecution()
        assert adapter._rejection_rate == 0.01
        assert adapter._latency_ms == 200.0
        assert adapter._fee_rate == 0.05

    def test_custom_params(self) -> None:
        adapter = PaperExecution(rejection_rate=0.1, latency_ms=100, fee_rate=0.04)
        assert adapter._rejection_rate == approx(0.1)
        assert adapter._latency_ms == approx(100.0)
        assert adapter._fee_rate == approx(0.04)

    def test_invalid_rejection_rate(self) -> None:
        with pytest.raises(ValueError, match="rejection_rate"):
            PaperExecution(rejection_rate=1.5)

    def test_seed_determinism(self) -> None:
        a = PaperExecution(seed=42)
        b = PaperExecution(seed=42)
        r1 = a._rng.random()
        r2 = b._rng.random()
        assert r1 == r2


class TestPaperExecutionSubmit:
    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
        self.adapter = PaperExecution(
            rejection_rate=0.0, latency_ms=0, fee_rate=0.0, seed=42,
        )

    async def test_fills_buy_order(self) -> None:
        result = await self.adapter.submit({
            "order_id": "ord_1", "market_id": "mkt_1",
            "side": "YES", "size": 100, "price": 0.50,
        })
        assert result["status"] in ("FILLED", "PARTIALLY_FILLED")
        assert result["filled_size"] > 0
        assert result["average_fill"] is not None

    async def test_fills_sell_order(self) -> None:
        result = await self.adapter.submit({
            "order_id": "ord_2", "market_id": "mkt_1",
            "side": "NO", "size": 100, "price": 0.50,
        })
        assert result["filled_size"] > 0

    async def test_buy_fill_price_above_midpoint(self) -> None:
        result = await self.adapter.submit({
            "order_id": "ord_3", "market_id": "mkt_1",
            "side": "YES", "size": 100, "price": 0.50, "spread": 0.04,
        })
        price = result["average_fill"]
        # Buy price should be > 0.50 (half-spread added)
        assert price > 0.50

    async def test_sell_fill_price_below_midpoint(self) -> None:
        result = await self.adapter.submit({
            "order_id": "ord_4", "market_id": "mkt_1",
            "side": "NO", "size": 100, "price": 0.50, "spread": 0.04,
        })
        price = result["raw_fill_price"]
        # Sell price should be < 0.50 (half-spread subtracted)
        assert price < 0.50

    async def test_zero_size_rejected(self) -> None:
        result = await self.adapter.submit({
            "order_id": "ord_5", "market_id": "mkt_1",
            "side": "YES", "size": 0, "price": 0.50,
        })
        assert result["status"] == "REJECTED"

    async def test_fee_applied_to_average_fill(self) -> None:
        adapter = PaperExecution(
            rejection_rate=0.0, latency_ms=0, fee_rate=0.05, seed=42,
        )
        result = await adapter.submit({
            "order_id": "ord_6", "market_id": "mkt_1",
            "side": "YES", "size": 100, "price": 0.50,
        })
        avg_fill = result["average_fill"]
        assert avg_fill is not None
        # Fee should make effective price > raw price for buys
        assert avg_fill > result["raw_fill_price"]

    async def test_large_order_slippage(self) -> None:
        result = await self.adapter.submit({
            "order_id": "ord_7", "market_id": "mkt_1",
            "side": "YES", "size": 10000, "price": 0.50,
            "depth": 100, "spread": 0.04,
        })
        # Large order should have slippage > standard half-spread
        assert result["average_fill"] is not None
        assert result["average_fill"] > 0.52

    async def test_partial_fill_on_large_order(self) -> None:
        adapter = PaperExecution(
            rejection_rate=0.0, latency_ms=0, fee_rate=0.0, seed=42,
        )
        result = await adapter.submit({
            "order_id": "ord_8", "market_id": "mkt_1",
            "side": "YES", "size": 10000, "price": 0.50,
            "depth": 100,
        })
        assert result["status"] == "PARTIALLY_FILLED"
        assert result["filled_size"] < 10000

    async def test_fill_price_clamped(self) -> None:
        result = await self.adapter.submit({
            "order_id": "ord_9", "market_id": "mkt_1",
            "side": "YES", "size": 100, "price": 0.999, "spread": 0.10,
        })
        assert result["average_fill"] is None or result["average_fill"] <= 0.99

    async def test_rejection_rate(self) -> None:
        adapter = PaperExecution(
            rejection_rate=1.0, latency_ms=0, fee_rate=0.0, seed=42,
        )
        result = await adapter.submit({
            "order_id": "ord_r", "market_id": "mkt_1",
            "side": "YES", "size": 100, "price": 0.50,
        })
        assert result["status"] == "REJECTED"


class TestPaperExecutionCancel:
    @pytest.fixture(autouse=True)
    async def _setup(self) -> None:
        self.adapter = PaperExecution(
            rejection_rate=0.0, latency_ms=0, fee_rate=0.0, seed=42,
        )
        await self.adapter.submit({
            "order_id": "ord_c", "market_id": "mkt_1",
            "side": "YES", "size": 100, "price": 0.50,
        })

    async def test_cancel_existing_order(self) -> None:
        result = await self.adapter.cancel("ord_c")
        assert result is True

    async def test_cancel_nonexistent_order(self) -> None:
        result = await self.adapter.cancel("nonexistent")
        assert result is False


class TestPaperExecutionStatus:
    @pytest.fixture(autouse=True)
    async def _setup(self) -> None:
        self.adapter = PaperExecution(
            rejection_rate=0.0, latency_ms=0, fee_rate=0.0, seed=42,
        )
        await self.adapter.submit({
            "order_id": "ord_s", "market_id": "mkt_1",
            "side": "YES", "size": 100, "price": 0.50,
        })

    async def test_status_found(self) -> None:
        result = await self.adapter.status("ord_s")
        assert result["order_id"] == "ord_s"
        assert result["status"] in ("FILLED", "PARTIALLY_FILLED")

    async def test_status_not_found(self) -> None:
        result = await self.adapter.status("nonexistent")
        assert result["status"] == "NOT_FOUND"


# ── ExecutionEngine ─────────────────────────────────────────────────────


class FakeAdapter(ExecutionAdapter):
    """Minimal adapter stub for engine tests."""

    def __init__(self) -> None:
        self.submit_calls: list[dict[str, Any]] = []
        self._result: dict[str, Any] = {
            "status": "FILLED",
            "filled_size": 100.0,
            "average_fill": 0.52,
        }

    async def submit(self, order: dict[str, Any]) -> dict[str, Any]:
        self.submit_calls.append(order)
        return {
            "order_id": order.get("order_id"),
            **self._result,
        }

    async def cancel(self, order_id: str) -> bool:
        return True

    async def status(self, order_id: str) -> dict[str, Any]:
        return {"order_id": order_id, "status": "FILLED"}


def make_approved_decision(
    market_id: str = "mkt_1",
    side: str = "YES",
    size: float = 100.0,
    edge: float = 0.05,
) -> RiskDecision:
    return RiskDecision(
        approved=True,
        market_id=market_id,
        side=side,
        size=size,
        risk_metrics={"equity": 10000, "entry_price": 0.50, "open_positions": 3},
        signal_id="sig_1",
        net_edge=edge,
    )


def make_rejected_decision(reason: str = "Test rejection") -> RiskDecision:
    return RiskDecision(
        approved=False,
        market_id="mkt_1",
        side="YES",
        size=0.0,
        reason=reason,
    )


class TestExecutionEngine:
    async def test_execute_approved_decision(self) -> None:
        adapter = FakeAdapter()
        engine = ExecutionEngine(adapter)
        decision = make_approved_decision()

        result = await engine.execute(decision)

        assert result.status == "FILLED"
        assert result.filled_size == approx(100.0)
        assert result.average_fill == approx(0.52)
        assert result.error is None

    async def test_execute_rejected_decision(self) -> None:
        adapter = FakeAdapter()
        engine = ExecutionEngine(adapter)
        decision = make_rejected_decision()

        result = await engine.execute(decision)

        assert result.status == "REJECTED"
        assert result.requested_size == 0.0
        assert "unapproved" in (result.error or "").lower()

    async def test_execute_zero_size(self) -> None:
        adapter = FakeAdapter()
        engine = ExecutionEngine(adapter)
        decision = make_approved_decision(size=0.0)

        result = await engine.execute(decision)

        assert result.status == "REJECTED"
        assert "zero size" in (result.error or "").lower()

    async def test_execute_returns_order_result(self) -> None:
        adapter = FakeAdapter()
        engine = ExecutionEngine(adapter)
        decision = make_approved_decision()

        result = await engine.execute(decision)

        assert isinstance(result, OrderResult)
        assert result.market_id == "mkt_1"
        assert result.side == "YES"
        assert result.requested_size == approx(100.0)

    async def test_execute_passes_payload_to_adapter(self) -> None:
        adapter = FakeAdapter()
        engine = ExecutionEngine(adapter)
        decision = make_approved_decision()

        await engine.execute(decision)

        assert len(adapter.submit_calls) == 1
        payload = adapter.submit_calls[0]
        assert payload["market_id"] == "mkt_1"
        assert payload["side"] == "YES"
        assert payload["size"] == approx(100.0)

    async def test_execute_generates_order_id(self) -> None:
        adapter = FakeAdapter()
        engine = ExecutionEngine(adapter)
        decision = make_approved_decision()

        result = await engine.execute(decision)

        assert len(result.order_id) > 0

    async def test_execute_with_partial_fill(self) -> None:
        adapter = FakeAdapter()
        adapter._result = {
            "status": "PARTIALLY_FILLED",
            "filled_size": 40.0,
            "average_fill": 0.51,
        }
        engine = ExecutionEngine(adapter)
        decision = make_approved_decision()

        result = await engine.execute(decision)

        assert result.status == "PARTIALLY_FILLED"
        assert result.filled_size == approx(40.0)

    async def test_execute_adapter_exception(self) -> None:
        class FailingAdapter(FakeAdapter):
            async def submit(self, order: dict[str, Any]) -> dict[str, Any]:
                msg = "Connection refused"
                raise ConnectionError(msg)

        engine = ExecutionEngine(FailingAdapter())
        decision = make_approved_decision()

        result = await engine.execute(decision)

        assert result.status == "REJECTED"
        assert "Connection refused" in (result.error or "")

    async def test_state_machine_in_result(self) -> None:
        adapter = FakeAdapter()
        engine = ExecutionEngine(adapter)
        decision = make_approved_decision()

        result = await engine.execute(decision)

        assert result.state_machine is not None
        assert result.state_machine.state == OrderState.FILLED

    async def test_extra_includes_raw_result(self) -> None:
        adapter = FakeAdapter()
        engine = ExecutionEngine(adapter)
        decision = make_approved_decision()

        result = await engine.execute(decision)

        assert "raw_adapter_result" in result.extra

    async def test_cancel_order(self) -> None:
        adapter = FakeAdapter()
        engine = ExecutionEngine(adapter)

        cancelled = await engine.cancel_order("ord_1")

        assert cancelled is True

    async def test_order_status(self) -> None:
        adapter = FakeAdapter()
        engine = ExecutionEngine(adapter)

        status = await engine.order_status("ord_1")

        assert status["status"] == "FILLED"
