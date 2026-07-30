"""Tests for the orchestrator engine (app.orchestrator.engine)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.modes.state import ModeState, OperatingMode
from app.orchestrator.engine import Orchestrator
from app.orchestrator.router import SignalRouter
from app.risk.circuit_breaker import BreakerState, CircuitBreaker


@pytest.fixture
def router() -> MagicMock:
    r = MagicMock(spec=SignalRouter)
    r.route_all = AsyncMock(return_value=[])
    return r


@pytest.fixture
def breaker() -> CircuitBreaker:
    return CircuitBreaker(persist=False)


@pytest.fixture
def mode() -> ModeState:
    return ModeState(OperatingMode.PAPER)


class TestOrchestrator:
    @pytest.mark.asyncio
    async def test_startup_halted_logs_warning(
        self, router: MagicMock, breaker: CircuitBreaker
    ) -> None:
        orch = Orchestrator(
            router=router,
            breaker=breaker,
            mode=ModeState(OperatingMode.HALTED),
        )
        await orch.startup()
        assert orch._running is False

    @pytest.mark.asyncio
    async def test_run_iteration_skipped_when_halted(
        self, router: MagicMock, breaker: CircuitBreaker, mode: ModeState
    ) -> None:
        breaker._state = BreakerState.HALTED
        orch = Orchestrator(
            router=router,
            breaker=breaker,
            mode=mode,
        )
        orch._running = True
        await orch._run_iteration()
        router.route_all.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_iteration_calls_router(
        self, router: MagicMock, breaker: CircuitBreaker, mode: ModeState
    ) -> None:
        async def data_provider() -> dict:
            return {
                "mkt_001": {
                    "market_id": "mkt_001",
                    "midpoint": 0.50,
                    "spread": 0.02,
                },
            }

        orch = Orchestrator(
            router=router,
            breaker=breaker,
            mode=mode,
            data_provider=data_provider,
        )
        orch._running = True
        await orch._run_iteration()
        router.route_all.assert_called_once()

    @pytest.mark.asyncio
    async def test_data_provider_error_handled(
        self, router: MagicMock, breaker: CircuitBreaker, mode: ModeState
    ) -> None:
        async def failing_provider() -> dict:
            raise RuntimeError("Data fetch failed")

        orch = Orchestrator(
            router=router,
            breaker=breaker,
            mode=mode,
            data_provider=failing_provider,
        )
        orch._running = True
        await orch._run_iteration()
        router.route_all.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop_sets_running_false(
        self, router: MagicMock, breaker: CircuitBreaker, mode: ModeState
    ) -> None:
        orch = Orchestrator(router=router, breaker=breaker, mode=mode)
        assert orch._running is False
        orch._running = True
        orch.stop()
        assert orch._running is False

    def test_reset_daily_tracking(
        self, router: MagicMock, breaker: CircuitBreaker, mode: ModeState
    ) -> None:
        orch = Orchestrator(router=router, breaker=breaker, mode=mode)
        orch._daily_pnl = -500.0
        orch._consecutive_losses = 3
        orch.reset_daily_tracking()
        assert orch._daily_pnl == 0.0
        assert orch._consecutive_losses == 0

    @pytest.mark.asyncio
    async def test_get_equity_default(
        self, router: MagicMock, breaker: CircuitBreaker, mode: ModeState
    ) -> None:
        orch = Orchestrator(router=router, breaker=breaker, mode=mode)
        assert orch._get_equity() == 10_000.0

    @pytest.mark.asyncio
    async def test_get_equity_custom(
        self, router: MagicMock, breaker: CircuitBreaker, mode: ModeState
    ) -> None:
        orch = Orchestrator(
            router=router,
            breaker=breaker,
            mode=mode,
            get_equity=lambda: 50_000.0,
        )
        assert orch._get_equity() == 50_000.0

    @pytest.mark.asyncio
    async def test_run_iteration_empty_markets(
        self, router: MagicMock, breaker: CircuitBreaker, mode: ModeState
    ) -> None:
        orch = Orchestrator(
            router=router,
            breaker=breaker,
            mode=mode,
            data_provider=lambda: {},
        )
        orch._running = True
        await orch._run_iteration()
        router.route_all.assert_not_called()
