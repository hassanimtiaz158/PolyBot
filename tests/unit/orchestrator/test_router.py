"""Tests for the signal router (app.orchestrator.router)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.modes.state import ModeState, OperatingMode
from app.orchestrator.pipeline import PipelineResult, TradePipeline
from app.orchestrator.router import SignalRouter
from app.strategies.base import Signal, Strategy, StrategyDecision


class DummyStrategy(Strategy):
    name = "dummy"

    def generate_signal(
        self,
        features: dict,
        context: dict | None = None,
    ) -> Signal:
        return Signal(
            strategy=self.name,
            market_id=features.get("market_id", "unknown"),
            side="YES",
            decision=StrategyDecision.CANDIDATE,
            model_probability=0.55,
            implied_probability=0.50,
            confidence=0.80,
            reason="dummy signal",
            feature_snapshot=dict(features),
        )


class SilentStrategy(Strategy):
    name = "silent"

    def generate_signal(
        self,
        features: dict,
        context: dict | None = None,
    ) -> Signal:
        return Signal(
            strategy=self.name,
            market_id=features.get("market_id", "unknown"),
            side="NO_SIDE",
        )


class FailingStrategy(Strategy):
    name = "failing"

    def generate_signal(
        self,
        features: dict,
        context: dict | None = None,
    ) -> Signal:
        msg = "Something went wrong"
        raise RuntimeError(msg)


@pytest.fixture
def pipeline() -> MagicMock:
    p = MagicMock(spec=TradePipeline)
    p.run = AsyncMock(
        return_value=PipelineResult(
            signal=Signal(
                strategy="dummy",
                market_id="mkt_001",
                side="YES",
                decision=StrategyDecision.CANDIDATE,
                model_probability=0.55,
                implied_probability=0.50,
                confidence=0.80,
                reason="dummy signal",
            ),
        )
    )
    return p


@pytest.fixture
def features() -> dict:
    return {
        "market_id": "mkt_001",
        "midpoint": 0.50,
        "spread": 0.02,
        "bid": 0.49,
        "ask": 0.51,
        "liquidity_score": 5000.0,
        "timestamp": "2026-07-30T00:00:00Z",
    }


class TestSignalRouter:
    def test_register_strategy(self) -> None:
        router = SignalRouter(
            pipeline=MagicMock(spec=TradePipeline),
            mode=ModeState(OperatingMode.PAPER),
        )
        router.register_strategy("dummy", DummyStrategy(), enabled=True)
        assert "dummy" in router.registered_strategies
        assert router.is_enabled("dummy")

    def test_enable_disable(self) -> None:
        router = SignalRouter(
            pipeline=MagicMock(spec=TradePipeline),
            mode=ModeState(OperatingMode.PAPER),
        )
        router.register_strategy("dummy", DummyStrategy(), enabled=False)
        assert not router.is_enabled("dummy")
        router.enable_strategy("dummy")
        assert router.is_enabled("dummy")
        router.disable_strategy("dummy")
        assert not router.is_enabled("dummy")

    def test_active_strategies(self) -> None:
        router = SignalRouter(
            pipeline=MagicMock(spec=TradePipeline),
            mode=ModeState(OperatingMode.PAPER),
        )
        router.register_strategy("a", DummyStrategy(), enabled=True)
        router.register_strategy("b", DummyStrategy(), enabled=False)
        router.register_strategy("c", DummyStrategy(), enabled=True)
        assert router.active_strategies == ["a", "c"]

    @pytest.mark.asyncio
    async def test_route_all_gates_by_mode(
        self, pipeline: MagicMock, features: dict
    ) -> None:
        router = SignalRouter(
            pipeline=pipeline,
            mode=ModeState(OperatingMode.RESEARCH),
        )
        router.register_strategy("dummy", DummyStrategy(), enabled=True)

        results = await router.route_all(
            market_id="mkt_001",
            features=features,
        )

        assert len(results) == 1
        assert results[0].error is not None
        assert "Gated by mode" in str(results[0].error)
        pipeline.run.assert_not_called()

    @pytest.mark.asyncio
    async def test_route_all_paper_mode_executes(
        self, pipeline: MagicMock, features: dict
    ) -> None:
        router = SignalRouter(
            pipeline=pipeline,
            mode=ModeState(OperatingMode.PAPER),
        )
        router.register_strategy("dummy", DummyStrategy(), enabled=True)

        results = await router.route_all(
            market_id="mkt_001",
            features=features,
        )

        assert len(results) == 1
        pipeline.run.assert_called_once()

    @pytest.mark.asyncio
    async def test_handles_strategy_error(
        self, pipeline: MagicMock, features: dict
    ) -> None:
        router = SignalRouter(
            pipeline=pipeline,
            mode=ModeState(OperatingMode.PAPER),
        )
        router.register_strategy("failing", FailingStrategy(), enabled=True)

        results = await router.route_all(
            market_id="mkt_001",
            features=features,
        )

        assert len(results) == 1
        assert results[0].error is not None
        assert "Strategy error" in str(results[0].error)

    @pytest.mark.asyncio
    async def test_skips_silent_strategies(
        self, pipeline: MagicMock, features: dict
    ) -> None:
        router = SignalRouter(
            pipeline=pipeline,
            mode=ModeState(OperatingMode.PAPER),
        )
        router.register_strategy("silent", SilentStrategy(), enabled=True)

        results = await router.route_all(
            market_id="mkt_001",
            features=features,
        )

        assert len(results) == 1
        assert results[0].signal.decision == StrategyDecision.NO_SIGNAL
        pipeline.run.assert_not_called()

    @pytest.mark.asyncio
    async def test_live_guarded_mode_executes(
        self, pipeline: MagicMock, features: dict
    ) -> None:
        router = SignalRouter(
            pipeline=pipeline,
            mode=ModeState(OperatingMode.LIVE_GUARDED),
        )
        router.register_strategy("dummy", DummyStrategy(), enabled=True)

        results = await router.route_all(
            market_id="mkt_001",
            features=features,
        )

        assert len(results) == 1
        pipeline.run.assert_called_once()

    @pytest.mark.asyncio
    async def test_backtest_mode_gates(
        self, pipeline: MagicMock, features: dict
    ) -> None:
        router = SignalRouter(
            pipeline=pipeline,
            mode=ModeState(OperatingMode.BACKTEST),
        )
        router.register_strategy("dummy", DummyStrategy(), enabled=True)

        results = await router.route_all(
            market_id="mkt_001",
            features=features,
        )

        assert len(results) == 1
        assert results[0].error is not None
        pipeline.run.assert_not_called()
