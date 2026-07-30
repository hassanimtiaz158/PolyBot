"""Tests for the trade pipeline (app.orchestrator.pipeline)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.ev.expected_value import ExpectedValueEngine
from app.execution.engine import ExecutionEngine, OrderResult
from app.orchestrator.pipeline import PipelineResult, TradePipeline
from app.portfolio.tracker import PortfolioTracker
from app.risk.engine import RiskDecision, RiskEngine
from app.storage.repositories import (
    OrderRepository,
    PositionRepository,
    RiskEventRepository,
    SignalRepository,
)
from app.strategies.base import Signal, StrategyDecision


@pytest.fixture
def portfolio() -> PortfolioTracker:
    return PortfolioTracker()


@pytest.fixture
def ev_engine() -> ExpectedValueEngine:
    return ExpectedValueEngine(min_net_edge=0.01)


@pytest.fixture
def risk_engine(portfolio: PortfolioTracker) -> RiskEngine:
    eng = RiskEngine(portfolio=portfolio)
    eng.evaluate = AsyncMock(  # type: ignore[method-assign]
        return_value=RiskDecision(
            approved=True,
            market_id="mkt_001",
            side="YES",
            size=50.0,
            reason="",
            signal_id="sig_001",
            net_edge=0.02,
            risk_metrics={"equity": 10000, "entry_price": 0.50},
        )
    )
    return eng


@pytest.fixture
def exec_engine() -> ExecutionEngine:
    eng = MagicMock(spec=ExecutionEngine)
    eng.execute = AsyncMock(
        return_value=OrderResult(
            order_id="ord_001",
            market_id="mkt_001",
            side="YES",
            status="FILLED",
            requested_size=50.0,
            filled_size=50.0,
            average_fill=0.52,
        )
    )
    return eng


@pytest.fixture
def repos() -> dict:
    return {
        "signal_repo": MagicMock(spec=SignalRepository),
        "order_repo": MagicMock(spec=OrderRepository),
        "position_repo": MagicMock(spec=PositionRepository),
        "risk_repo": MagicMock(spec=RiskEventRepository),
    }


@pytest.fixture
def pipeline(
    ev_engine: ExpectedValueEngine,
    risk_engine: RiskEngine,
    exec_engine: ExecutionEngine,
    portfolio: PortfolioTracker,
    repos: dict,
) -> TradePipeline:
    return TradePipeline(
        ev_engine=ev_engine,
        risk_engine=risk_engine,
        exec_engine=exec_engine,
        portfolio=portfolio,
        signal_repo=repos["signal_repo"],
        order_repo=repos["order_repo"],
        position_repo=repos["position_repo"],
        risk_repo=repos["risk_repo"],
    )


def _candidate_signal(**overrides: object) -> Signal:
    params: dict = {
        "strategy": "test",
        "market_id": "mkt_001",
        "side": "YES",
        "decision": StrategyDecision.CANDIDATE,
        "model_probability": 0.55,
        "implied_probability": 0.50,
        "confidence": 0.80,
        "reason": "test signal",
    }
    params.update(overrides)
    return Signal(**params)


def _features(**overrides: float) -> dict:
    params = {
        "market_id": "mkt_001",
        "midpoint": 0.50,
        "spread": 0.02,
        "bid": 0.49,
        "ask": 0.51,
        "liquidity_score": 5000.0,
        "timestamp": "2026-07-30T00:00:00Z",
    }
    params.update(overrides)
    return params


class TestTradePipeline:
    @pytest.mark.asyncio
    async def test_rejects_no_signal(
        self, pipeline: TradePipeline, repos: dict
    ) -> None:
        signal = Signal(
            strategy="test",
            market_id="mkt_001",
            side="YES",
            decision=StrategyDecision.NO_SIGNAL,
        )
        result = await pipeline.run(signal, features=_features())
        assert result.order_result is None
        assert result.risk_decision is None
        assert result.ev_result is None
        repos["signal_repo"].insert.assert_not_called()

    @pytest.mark.asyncio
    async def test_full_pipeline_approved(
        self, pipeline: TradePipeline, repos: dict
    ) -> None:
        signal = _candidate_signal()
        result = await pipeline.run(signal, features=_features())

        assert result.ev_result is not None
        assert result.ev_result.tradeable
        assert result.risk_decision is not None
        assert result.risk_decision.approved
        assert result.order_result is not None
        assert result.order_result.status == "FILLED"
        assert result.error is None

        repos["signal_repo"].insert.assert_called_once()
        repos["order_repo"].insert.assert_called_once()

    @pytest.mark.asyncio
    async def test_rejected_by_risk(
        self, pipeline: TradePipeline, risk_engine: RiskEngine, repos: dict
    ) -> None:
        risk_engine.evaluate = AsyncMock(  # type: ignore[method-assign]
            return_value=RiskDecision(
                approved=False,
                market_id="mkt_001",
                side="YES",
                size=0.0,
                reason="SPREAD_TOO_HIGH",
                signal_id="sig_001",
            )
        )
        signal = _candidate_signal()
        result = await pipeline.run(signal, features=_features())

        assert result.ev_result is not None
        assert result.risk_decision is not None
        assert not result.risk_decision.approved
        assert result.risk_decision.reason == "SPREAD_TOO_HIGH"
        assert result.order_result is None

        repos["risk_repo"].insert.assert_called_once()

    @pytest.mark.asyncio
    async def test_ev_rejection(
        self, pipeline: TradePipeline, risk_engine: RiskEngine, repos: dict
    ) -> None:
        signal = _candidate_signal(
            model_probability=0.51,
            implied_probability=0.50,
            confidence=0.10,
        )
        result = await pipeline.run(signal, features=_features())

        assert result.ev_result is not None
        assert not result.ev_result.tradeable
        risk_engine.evaluate.assert_called_once()
        _, kwargs = risk_engine.evaluate.call_args
        assert kwargs.get("net_edge") is None

    @pytest.mark.asyncio
    async def test_pipeline_error_handling(
        self, ev_engine: ExpectedValueEngine, risk_engine: RiskEngine, repos: dict
    ) -> None:
        bad_engine = MagicMock(spec=ExecutionEngine)
        bad_engine.execute = AsyncMock(side_effect=RuntimeError("Connection lost"))

        pipeline = TradePipeline(
            ev_engine=ev_engine,
            risk_engine=risk_engine,
            exec_engine=bad_engine,
            portfolio=PortfolioTracker(),
            signal_repo=repos["signal_repo"],
            order_repo=repos["order_repo"],
            position_repo=repos["position_repo"],
            risk_repo=repos["risk_repo"],
        )

        signal = _candidate_signal()
        result = await pipeline.run(signal, features=_features())

        assert result.error is not None
        assert "Connection lost" in str(result.error)
        assert result.order_result is None

    @pytest.mark.asyncio
    async def test_persist_signal_even_on_rejection(
        self, pipeline: TradePipeline, risk_engine: RiskEngine, repos: dict
    ) -> None:
        risk_engine.evaluate = AsyncMock(  # type: ignore[method-assign]
            return_value=RiskDecision(
                approved=False,
                market_id="mkt_001",
                side="YES",
                size=0.0,
                reason="NET_EDGE_BELOW_THRESHOLD",
                signal_id="sig_001",
            )
        )
        signal = _candidate_signal()
        await pipeline.run(signal, features=_features())

        repos["signal_repo"].insert.assert_called_once()

    def test_pipeline_result_defaults(self) -> None:
        signal = _candidate_signal()
        result = PipelineResult(signal=signal)
        assert result.ev_result is None
        assert result.risk_decision is None
        assert result.order_result is None
        assert result.error is None
        assert result.extra == {}
