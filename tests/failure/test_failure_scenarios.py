"""Failure scenario tests — verifies system behaviour under component failures.

Each test class targets a specific failure mode and confirms the system
fails closed (rejects trades, blocks execution, or handles gracefully).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.config.settings import Settings
from app.data.validators import DataQuality, DataValidator
from app.execution.engine import ExecutionEngine
from app.execution.interface import ExecutionAdapter
from app.monitoring.health import health_status
from app.portfolio.tracker import PortfolioTracker
from app.risk.circuit_breaker import BreakerState, CircuitBreaker
from app.risk.engine import RiskDecision, RiskEngine
from app.risk.limits import RiskLimits
from app.strategies.base import Signal, StrategyDecision

# ── Helpers ──────────────────────────────────────────────────────────


def _make_signal(
    market_id: str = "mkt_001",
    strategy: str = "test",
    side: str = "YES",
    confidence: float = 0.8,
    model_probability: float = 0.6,
    implied_probability: float = 0.5,
    gross_edge: float = 0.1,
    **overrides: Any,
) -> Signal:
    return Signal(
        strategy=strategy,
        market_id=market_id,
        side=side,
        decision=StrategyDecision.CANDIDATE,
        confidence=confidence,
        model_probability=model_probability,
        implied_probability=implied_probability,
        gross_edge=gross_edge,
        feature_snapshot={
            "market_id": market_id,
            "midpoint": implied_probability,
            "spread": 0.02,
            "bid": implied_probability - 0.01,
            "ask": implied_probability + 0.01,
            "liquidity_score": 5000.0,
            "timestamp": datetime.now(UTC).isoformat(),
        },
        **overrides,
    )


def _fresh_features(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "market_id": "mkt_001",
        "midpoint": 0.50,
        "spread": 0.02,
        "bid": 0.49,
        "ask": 0.51,
        "liquidity_score": 5000.0,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    base.update(overrides)
    return base


# ══════════════════════════════════════════════════════════════════════
# 1. Database unavailable — RiskEngine rejects trades when DB is down
# ══════════════════════════════════════════════════════════════════════


class TestDatabaseUnavailable:
    """RiskEngine must reject trades when DB health check fails."""

    @pytest.mark.asyncio
    async def test_risk_engine_rejects_when_db_unavailable(self) -> None:
        portfolio = PortfolioTracker()
        risk = RiskEngine(portfolio=portfolio, breaker=CircuitBreaker(persist=False))
        signal = _make_signal()

        decision = await risk.evaluate(
            signal,
            net_edge=0.1,
            database_available=False,
        )
        assert decision.approved is False
        assert "DB_UNAVAILABLE" in decision.reason

    @pytest.mark.asyncio
    async def test_risk_engine_allows_when_db_healthy(self) -> None:
        portfolio = PortfolioTracker()
        risk = RiskEngine(portfolio=portfolio, breaker=CircuitBreaker(persist=False))
        signal = _make_signal()

        decision = await risk.evaluate(
            signal,
            net_edge=0.1,
            database_available=True,
        )
        assert decision.approved is True

    @pytest.mark.asyncio
    async def test_health_check_detects_db_failure(self) -> None:
        health_status.checks.clear()
        health_status.set_unhealthy("database")
        assert health_status.is_healthy("database") is False
        health_status.set_healthy("database")
        assert health_status.is_healthy("database") is True


# ══════════════════════════════════════════════════════════════════════
# 2. Stale data rejection — RiskEngine rejects when data is stale
# ══════════════════════════════════════════════════════════════════════


class TestStaleDataRejection:
    """RiskEngine must reject trades when data freshness check fails."""

    @pytest.mark.asyncio
    async def test_risk_engine_rejects_stale_feature_snapshot(self) -> None:
        portfolio = PortfolioTracker()
        risk = RiskEngine(portfolio=portfolio, breaker=CircuitBreaker(persist=False))
        signal = _make_signal()
        signal.feature_snapshot["timestamp"] = (
            datetime.now(UTC) - timedelta(hours=1)
        ).isoformat()

        decision = await risk.evaluate(signal, net_edge=0.1)
        assert decision.approved is False
        assert "STALE_DATA" in decision.reason

    @pytest.mark.asyncio
    async def test_risk_engine_rejects_none_timestamp(self) -> None:
        portfolio = PortfolioTracker()
        risk = RiskEngine(portfolio=portfolio, breaker=CircuitBreaker(persist=False))
        signal = _make_signal()
        signal.feature_snapshot["timestamp"] = None

        decision = await risk.evaluate(signal, net_edge=0.1)
        assert decision.approved is False
        assert "STALE_DATA" in decision.reason

    def test_risk_limits_rejects_stale_timestamp(self) -> None:
        limits = RiskLimits()
        stale = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        check = limits.check_data_freshness(stale)
        assert check.approved is False
        assert check.reason == "STALE_DATA"

    def test_risk_limits_rejects_none_timestamp(self) -> None:
        limits = RiskLimits()
        check = limits.check_data_freshness(None)
        assert check.approved is False
        assert check.reason == "STALE_DATA"

    def test_risk_limits_rejects_unparseable_timestamp(self) -> None:
        limits = RiskLimits()
        check = limits.check_data_freshness("not-a-date")
        assert check.approved is False
        assert check.reason == "STALE_DATA"

    def test_risk_limits_passes_fresh_timestamp(self) -> None:
        limits = RiskLimits()
        fresh = datetime.now(UTC).isoformat()
        check = limits.check_data_freshness(fresh)
        assert check.approved is True

    def test_data_validator_detects_stale_snapshot(self) -> None:
        validator = DataValidator(max_age_seconds=5)
        stale_features = _fresh_features(
            timestamp=(datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
        )
        quality = validator.assess(stale_features)
        assert quality == DataQuality.STALE

    def test_data_validator_passes_fresh_snapshot(self) -> None:
        validator = DataValidator(max_age_seconds=5)
        fresh_features = _fresh_features()
        quality = validator.assess(fresh_features)
        assert quality == DataQuality.HEALTHY


# ══════════════════════════════════════════════════════════════════════
# 3. API failure — Circuit breaker trips to HALTED when API fails
# ══════════════════════════════════════════════════════════════════════


class TestAPIFailure:
    """Circuit breaker trips to HALTED when API health check fails."""

    @pytest.mark.asyncio
    async def test_api_health_failure_trips_breaker_to_halted(self) -> None:
        breaker = CircuitBreaker(persist=False)
        await breaker.check_and_trigger(
            daily_pnl=0.0,
            consecutive_losses=0,
            data_fresh=True,
            api_healthy=False,
            equity=10000.0,
        )
        assert breaker.state == BreakerState.HALTED
        assert "API_HEALTH" in breaker.reasons

    @pytest.mark.asyncio
    async def test_risk_engine_rejects_when_api_down(self) -> None:
        portfolio = PortfolioTracker()
        risk = RiskEngine(portfolio=portfolio, breaker=CircuitBreaker(persist=False))
        signal = _make_signal()

        decision = await risk.evaluate(signal, net_edge=0.1, api_healthy=False)
        assert decision.approved is False
        assert "API_HEALTH" in decision.reason

    @pytest.mark.asyncio
    async def test_halted_breaker_blocks_all_trading(self) -> None:
        breaker = CircuitBreaker(persist=False)
        await breaker.trigger("API_HEALTH", severity="HARD")
        assert breaker.is_halted is True

        await breaker.check_and_trigger(
            daily_pnl=0.0,
            consecutive_losses=0,
            data_fresh=True,
            api_healthy=True,
            equity=10000.0,
        )
        assert breaker.is_halted is True

    @pytest.mark.asyncio
    async def test_risk_engine_rejects_on_halted_breaker(self) -> None:
        breaker = CircuitBreaker(persist=False)
        await breaker.trigger("TEST", severity="HARD")
        portfolio = PortfolioTracker()
        risk = RiskEngine(portfolio=portfolio, breaker=breaker)
        signal = _make_signal()

        decision = await risk.evaluate(signal, net_edge=0.1)
        assert decision.approved is False
        assert "SYSTEM_HALTED" in decision.reason

    @pytest.mark.asyncio
    async def test_soft_trigger_does_not_halt(self) -> None:
        breaker = CircuitBreaker(persist=False)
        state = await breaker.trigger("STALE_DATA", severity="SOFT")
        assert state == BreakerState.WARNING
        assert breaker.is_halted is False

    @pytest.mark.asyncio
    async def test_warning_escalates_to_halted_on_hard(self) -> None:
        breaker = CircuitBreaker(persist=False)
        await breaker.trigger("STALE_DATA", severity="SOFT")
        assert breaker.is_warning is True

        await breaker.trigger("API_HEALTH", severity="HARD")
        assert breaker.is_halted is True


# ══════════════════════════════════════════════════════════════════════
# 4. Malformed market data — Pipeline handles NaN/Inf/None gracefully
# ══════════════════════════════════════════════════════════════════════


class TestMalformedMarketData:
    """Pipeline and risk engine reject NaN/Inf/None midpoints gracefully."""

    def test_nan_midpoint_rejected_by_risk_limits(self) -> None:
        limits = RiskLimits()
        check = limits.check_data_validity({
            "market_id": "mkt",
            "midpoint": float("nan"),
            "spread": 0.02,
            "bid": 0.49,
            "ask": 0.51,
        })
        assert check.approved is False
        assert check.reason == "INVALID_DATA"

    def test_inf_spread_rejected_by_risk_limits(self) -> None:
        limits = RiskLimits()
        check = limits.check_data_validity({
            "market_id": "mkt",
            "midpoint": 0.5,
            "spread": float("inf"),
            "bid": 0.49,
            "ask": 0.51,
        })
        assert check.approved is False

    def test_none_midpoint_rejected_by_risk_limits(self) -> None:
        limits = RiskLimits()
        check = limits.check_data_validity({
            "market_id": "mkt",
            "midpoint": None,
            "spread": 0.02,
            "bid": 0.49,
            "ask": 0.51,
        })
        assert check.approved is False

    def test_missing_midpoint_rejected(self) -> None:
        limits = RiskLimits()
        check = limits.check_data_validity({
            "market_id": "mkt",
            "spread": 0.02,
            "bid": 0.49,
            "ask": 0.51,
        })
        assert check.approved is False
        assert check.reason == "INVALID_DATA"

    def test_missing_market_id_rejected(self) -> None:
        limits = RiskLimits()
        check = limits.check_data_validity({
            "midpoint": 0.5,
            "spread": 0.02,
            "bid": 0.49,
            "ask": 0.51,
        })
        assert check.approved is False

    def test_negative_midpoint_rejected(self) -> None:
        limits = RiskLimits()
        check = limits.check_data_validity({
            "market_id": "mkt",
            "midpoint": -0.5,
            "spread": 0.02,
            "bid": 0.49,
            "ask": 0.51,
        })
        assert check.approved is False

    def test_valid_data_passes(self) -> None:
        limits = RiskLimits()
        check = limits.check_data_validity({
            "market_id": "mkt",
            "midpoint": 0.5,
            "spread": 0.02,
            "bid": 0.49,
            "ask": 0.51,
        })
        assert check.approved is True

    def test_nan_midpoint_detected_by_risk_limits(self) -> None:
        limits = RiskLimits()
        check = limits.check_data_validity({
            "market_id": "mkt",
            "bid": 0.49,
            "ask": 0.51,
            "midpoint": float("nan"),
            "spread": 0.02,
        })
        assert check.approved is False
        assert check.reason == "INVALID_DATA"

    def test_data_validator_rejects_inf_spread(self) -> None:
        validator = DataValidator()
        row = {
            "market_id": "mkt",
            "bid": 0.49,
            "ask": 0.51,
            "midpoint": 0.50,
            "spread": float("inf"),
            "timestamp": datetime.now(UTC).isoformat(),
        }
        quality = validator.assess(row)
        assert quality == DataQuality.INVALID

    @pytest.mark.asyncio
    async def test_nan_model_probability_rejected_by_risk_engine(self) -> None:
        portfolio = PortfolioTracker()
        risk = RiskEngine(portfolio=portfolio, breaker=CircuitBreaker(persist=False))
        signal = _make_signal(model_probability=float("nan"))

        decision = await risk.evaluate(signal, net_edge=0.1)
        assert decision.approved is False
        assert "INVALID_NUMERIC" in decision.reason

    @pytest.mark.asyncio
    async def test_inf_confidence_rejected_by_risk_engine(self) -> None:
        portfolio = PortfolioTracker()
        risk = RiskEngine(portfolio=portfolio, breaker=CircuitBreaker(persist=False))
        signal = _make_signal(confidence=float("inf"))

        decision = await risk.evaluate(signal, net_edge=0.1)
        assert decision.approved is False
        assert "INVALID_NUMERIC" in decision.reason

    @pytest.mark.asyncio
    async def test_nan_net_edge_rejected_by_risk_engine(self) -> None:
        portfolio = PortfolioTracker()
        risk = RiskEngine(portfolio=portfolio, breaker=CircuitBreaker(persist=False))
        signal = _make_signal()

        decision = await risk.evaluate(signal, net_edge=float("nan"))
        assert decision.approved is False
        assert "INVALID_NUMERIC" in decision.reason


# ══════════════════════════════════════════════════════════════════════
# 5. Order submission failure — ExecutionEngine handles adapter exceptions
# ══════════════════════════════════════════════════════════════════════


class TestOrderSubmissionFailure:
    """ExecutionEngine returns REJECTED when the adapter throws."""

    class _FailingAdapter(ExecutionAdapter):
        async def submit(self, order: dict[str, Any]) -> dict[str, Any]:
            raise ConnectionError("Exchange unreachable")

        async def cancel(self, order_id: str) -> bool:
            return False

        async def status(self, order_id: str) -> dict[str, Any]:
            return {"order_id": order_id, "status": "NOT_FOUND"}

    class _TimeoutAdapter(ExecutionAdapter):
        async def submit(self, order: dict[str, Any]) -> dict[str, Any]:
            raise TimeoutError("Request timed out")

        async def cancel(self, order_id: str) -> bool:
            return False

        async def status(self, order_id: str) -> dict[str, Any]:
            return {"order_id": order_id, "status": "NOT_FOUND"}

    @pytest.mark.asyncio
    async def test_adapter_exception_returns_rejected(self) -> None:
        engine = ExecutionEngine(self._FailingAdapter())
        decision = RiskDecision(
            approved=True,
            market_id="mkt_001",
            side="YES",
            size=10.0,
            signal_id="sig_001",
        )

        result = await engine.execute(decision)

        assert result.status == "REJECTED"
        assert "Exchange unreachable" in (result.error or "")

    @pytest.mark.asyncio
    async def test_timeout_returns_rejected(self) -> None:
        engine = ExecutionEngine(self._TimeoutAdapter())
        decision = RiskDecision(
            approved=True,
            market_id="mkt_001",
            side="YES",
            size=10.0,
            signal_id="sig_001",
        )

        result = await engine.execute(decision)

        assert result.status == "REJECTED"
        assert "Request timed out" in (result.error or "")

    @pytest.mark.asyncio
    async def test_rejected_decision_not_submitted(self) -> None:
        adapter = self._FailingAdapter()
        engine = ExecutionEngine(adapter)
        decision = RiskDecision(
            approved=False,
            market_id="mkt_001",
            side="YES",
            size=0.0,
            reason="TEST",
        )

        result = await engine.execute(decision)

        assert result.status == "REJECTED"
        assert "unapproved" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_zero_size_not_submitted(self) -> None:
        adapter = self._FailingAdapter()
        engine = ExecutionEngine(adapter)
        decision = RiskDecision(
            approved=True,
            market_id="mkt_001",
            side="YES",
            size=0.0,
        )

        result = await engine.execute(decision)

        assert result.status == "REJECTED"
        assert "zero size" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_adapter_rejection_recorded_by_health_check(self) -> None:
        health_status.checks.clear()
        adapter = self._FailingAdapter()
        engine = ExecutionEngine(adapter)
        decision = RiskDecision(
            approved=True,
            market_id="mkt_001",
            side="YES",
            size=10.0,
            signal_id="sig_001",
        )

        await engine.execute(decision)

        exec_check = engine._exec_health
        assert exec_check is not None
        assert exec_check._consecutive_errors >= 1


# ══════════════════════════════════════════════════════════════════════
# 6. Duplicate order handling — Same order_id is idempotent in DB
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.usefixtures("test_db")
class TestDuplicateOrderHandling:
    """Same order_id submitted twice is idempotent in the database."""

    @pytest.mark.asyncio
    async def test_duplicate_order_id_ignored_in_db(self) -> None:
        from app.storage.models import Market
        from app.storage.models import Order as OrderModel
        from app.storage.repositories import MarketRepository, OrderRepository

        market_repo = MarketRepository()
        await market_repo.upsert(Market(market_id="mkt_dup", question="Dup test"))

        repo = OrderRepository()
        order = OrderModel(
            order_id="dup_ord_001",
            market_id="mkt_dup",
            side="YES",
            status="FILLED",
        )
        await repo.insert(order)
        await repo.insert(order)
        count = await repo.count()
        assert count == 1

    async def test_paper_execution_idempotent(self) -> None:
        from app.execution.paper import PaperExecution

        adapter = PaperExecution(rejection_rate=0.0, seed=42)
        order: dict[str, Any] = {
            "order_id": "idem_001",
            "market_id": "mkt",
            "side": "YES",
            "size": 10,
            "price": 0.5,
        }

        r1 = await adapter.submit(order)
        r2 = await adapter.submit(order)
        assert r1["order_id"] == r2["order_id"]
        assert r1["status"] == r2["status"]

    @pytest.mark.asyncio
    async def test_duplicate_signal_id_ignored_in_db(self) -> None:
        from app.storage.models import Market
        from app.storage.models import Signal as SignalModel
        from app.storage.repositories import MarketRepository, SignalRepository

        market_repo = MarketRepository()
        await market_repo.upsert(Market(market_id="mkt_sig", question="Sig test"))

        repo = SignalRepository()
        sig = SignalModel(
            signal_id="dup_sig_001",
            market_id="mkt_sig",
            strategy="test",
            side="YES",
            decision="CANDIDATE",
        )
        await repo.insert(sig)
        await repo.insert(sig)
        count = await repo.count()
        assert count == 1

    @pytest.mark.asyncio
    async def test_duplicate_risk_event_ignored_in_db(self) -> None:
        from app.storage.models import RiskEvent
        from app.storage.repositories import RiskEventRepository

        repo = RiskEventRepository()
        event = RiskEvent(
            event_id="dup_evt_001",
            event_type="TEST",
            severity="INFO",
            details="duplicate test",
        )
        await repo.insert(event)
        await repo.insert(event)
        count = await repo.count()
        assert count == 1


# ══════════════════════════════════════════════════════════════════════
# 7. Process restart simulation — Circuit breaker state survives via
#    save/load from SQLite
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.usefixtures("test_db")
class TestProcessRestart:
    """Circuit breaker state (daily_pnl, consecutive_losses, HALTED) survives."""

    @pytest.mark.asyncio
    async def test_circuit_breaker_persists_daily_pnl(self) -> None:
        breaker = CircuitBreaker(persist=True)
        breaker._daily_pnl = -500.0
        breaker._consecutive_losses = 3
        await breaker.trigger("TEST", severity="SOFT")

        breaker2 = CircuitBreaker(persist=True)
        await breaker2.load_state()

        assert breaker2._daily_pnl == -500.0
        assert breaker2._consecutive_losses == 3

    @pytest.mark.asyncio
    async def test_circuit_breaker_persists_halted_state(self) -> None:
        breaker = CircuitBreaker(persist=True)
        await breaker.trigger("API_HEALTH", severity="HARD")
        assert breaker.state == BreakerState.HALTED

        breaker2 = CircuitBreaker(persist=True)
        await breaker2.load_state()
        assert breaker2.state == BreakerState.HALTED
        assert "API_HEALTH" in breaker2.reasons

    @pytest.mark.asyncio
    async def test_circuit_breaker_persists_warning_state(self) -> None:
        breaker = CircuitBreaker(persist=True)
        await breaker.trigger("STALE_DATA", severity="SOFT")
        assert breaker.state == BreakerState.WARNING

        breaker2 = CircuitBreaker(persist=True)
        await breaker2.load_state()
        assert breaker2.state == BreakerState.WARNING
        assert "STALE_DATA" in breaker2.reasons

    @pytest.mark.asyncio
    async def test_circuit_breaker_persists_consecutive_losses(self) -> None:
        breaker = CircuitBreaker(persist=True)
        breaker._consecutive_losses = 4
        await breaker.trigger("TEST", severity="SOFT")

        breaker2 = CircuitBreaker(persist=True)
        await breaker2.load_state()
        assert breaker2._consecutive_losses == 4

    @pytest.mark.asyncio
    async def test_circuit_breaker_persists_triggered_at(self) -> None:
        breaker = CircuitBreaker(persist=True)
        await breaker.trigger("TEST", severity="SOFT")
        triggered_at = breaker.triggered_at
        assert triggered_at is not None

        breaker2 = CircuitBreaker(persist=True)
        await breaker2.load_state()
        assert breaker2.triggered_at == triggered_at

    @pytest.mark.asyncio
    async def test_circuit_breaker_load_defaults_when_empty(self) -> None:
        breaker = CircuitBreaker(persist=True)
        await breaker.load_state()
        assert breaker.state == BreakerState.NORMAL
        assert breaker.reasons == []
        assert breaker._daily_pnl == 0.0
        assert breaker._consecutive_losses == 0

    @pytest.mark.asyncio
    async def test_non_persist_breaker_ignores_load(self) -> None:
        breaker = CircuitBreaker(persist=False)
        await breaker.trigger("TEST", severity="HARD")
        assert breaker.state == BreakerState.HALTED

        breaker2 = CircuitBreaker(persist=False)
        await breaker2.load_state()
        assert breaker2.state == BreakerState.NORMAL


# ══════════════════════════════════════════════════════════════════════
# 8. Corrupted config — Invalid Settings values raise validation errors
# ══════════════════════════════════════════════════════════════════════


class TestCorruptedConfig:
    """Settings must reject invalid values at construction time."""

    def test_negative_max_spread_rejected(self) -> None:
        with pytest.raises(Exception):
            Settings(max_spread=-0.01)

    def test_negative_min_liquidity_rejected(self) -> None:
        with pytest.raises(Exception):
            Settings(min_liquidity=-100.0)

    def test_negative_max_position_pct_rejected(self) -> None:
        with pytest.raises(Exception):
            Settings(max_position_pct=-0.01)

    def test_max_spread_over_1_rejected(self) -> None:
        with pytest.raises(Exception):
            Settings(max_spread=1.5)

    def test_min_confidence_over_1_rejected(self) -> None:
        with pytest.raises(Exception):
            Settings(min_confidence=1.5)

    def test_zero_data_max_age_rejected(self) -> None:
        with pytest.raises(Exception):
            Settings(data_max_age_seconds=0)

    def test_negative_scan_interval_rejected(self) -> None:
        with pytest.raises(Exception):
            Settings(market_scan_interval_seconds=-1)

    def test_invalid_numeric_env_rejected(self) -> None:
        with pytest.raises(Exception):
            Settings(max_spread="not_a_number")

    def test_negative_max_daily_loss_pct_rejected(self) -> None:
        with pytest.raises(Exception):
            Settings(max_daily_loss_pct=-0.01)

    def test_negative_max_consecutive_losses_rejected(self) -> None:
        with pytest.raises(Exception):
            Settings(max_consecutive_losses=-1)

    def test_valid_settings_accepted(self) -> None:
        s = Settings(
            max_spread=0.05,
            min_liquidity=500.0,
            max_position_pct=0.02,
            data_max_age_seconds=10,
        )
        assert s.max_spread == 0.05
        assert s.min_liquidity == 500.0
