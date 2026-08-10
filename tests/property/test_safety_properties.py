"""Property-based safety tests — invariants that hold for ALL inputs.

Every test uses Hypothesis to generate arbitrary inputs and verifies
that critical safety properties are never violated.
"""

from datetime import UTC, datetime
from typing import Any

import pytest
from hypothesis import given
from hypothesis import settings as hyp_settings
from hypothesis.strategies import floats

from app.portfolio.tracker import PortfolioTracker
from app.risk.circuit_breaker import BreakerState, CircuitBreaker
from app.risk.engine import RiskEngine
from app.risk.position_sizing import PositionSizer
from app.strategies.base import Signal, StrategyDecision


def make_candidate(
    market_id: str = "mkt_prop",
    side: str = "YES",
    confidence: float = 0.85,
    timestamp: str | None = None,
    **extra: Any,
) -> Signal:
    ts = timestamp or datetime.now(UTC).isoformat()
    feat = {
        "market_id": market_id,
        "midpoint": 0.50,
        "spread": 0.02,
        "bid": 0.49,
        "ask": 0.51,
        "liquidity_score": 5000.0,
        "timestamp": ts,
    }
    feat.update(extra.pop("feature_snapshot", {}))
    return Signal(
        strategy="property_test",
        market_id=market_id,
        side=side,
        decision=StrategyDecision.CANDIDATE,
        model_probability=0.60,
        implied_probability=0.50,
        confidence=confidence,
        feature_snapshot=feat,
        **extra,
    )


class FakePortfolio(PortfolioTracker):
    def __init__(self, equity: float = 10000.0) -> None:
        super().__init__()
        self._equity = equity
        self._positions: dict[str, dict[str, Any]] = {}

    @property
    def equity(self) -> float:
        return self._equity

    def total_exposure(self) -> float:
        return sum(float(p.get("size", 0)) for p in self._positions.values())

    def summary(self) -> dict[str, Any]:
        return {"positions": self._positions, "equity": self._equity}


# ═══════════════════════════════════════════════════════════════════════════
# 1. Position size never exceeds equity * max_position_pct
# ═══════════════════════════════════════════════════════════════════════════


class TestPropertyPositionSizeCap:
    @hyp_settings(max_examples=50)
    @given(
        equity=floats(min_value=1.0, max_value=1_000_000, allow_nan=False),
        confidence=floats(min_value=0.0, max_value=1.0, allow_nan=False),
    )
    def test_size_never_exceeds_equity_times_max_pct(
        self, equity: float, confidence: float,
    ) -> None:
        sizer = PositionSizer()
        size = sizer.determine_size(
            equity=equity, confidence=confidence, net_edge=0.1,
        )
        max_allowed = equity * 0.01
        assert size <= max_allowed
        assert size >= 0.0


# ═══════════════════════════════════════════════════════════════════════════
# 2. Halted system cannot submit
# ═══════════════════════════════════════════════════════════════════════════


class TestPropertyHaltedRejectsAll:
    @pytest.mark.asyncio
    @hyp_settings(max_examples=50)
    @given(
        confidence=floats(min_value=0.7, max_value=1.0, allow_nan=False),
        net_edge=floats(min_value=0.05, max_value=0.5, allow_nan=False),
    )
    async def test_halted_always_rejects(
        self, confidence: float, net_edge: float,
    ) -> None:
        portfolio = FakePortfolio(equity=10000)
        breaker = CircuitBreaker(persist=False)
        engine = RiskEngine(portfolio=portfolio, breaker=breaker)

        await breaker.trigger("TEST_HALT", severity="HARD")
        assert breaker.state == BreakerState.HALTED

        signal = make_candidate(confidence=confidence)
        decision = await engine.evaluate(signal, net_edge=net_edge)

        assert decision.approved is False
        assert decision.size == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# 3. Stale data cannot approve an order
# ═══════════════════════════════════════════════════════════════════════════


class TestPropertyStaleDataRejectsAll:
    @pytest.mark.asyncio
    @hyp_settings(max_examples=50)
    @given(
        confidence=floats(min_value=0.7, max_value=1.0, allow_nan=False),
        net_edge=floats(min_value=0.05, max_value=0.5, allow_nan=False),
    )
    async def test_stale_data_always_rejects(
        self, confidence: float, net_edge: float,
    ) -> None:
        portfolio = FakePortfolio(equity=10000)
        engine = RiskEngine(portfolio=portfolio)

        stale_ts = "2020-01-01T00:00:00+00:00"
        signal = make_candidate(
            confidence=confidence, timestamp=stale_ts,
        )
        decision = await engine.evaluate(signal, net_edge=net_edge)

        assert decision.approved is False
        assert decision.size == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# 4. Negative net edge cannot pass normal gate
# ═══════════════════════════════════════════════════════════════════════════


class TestPropertyNegativeEdgeRejectsAll:
    @pytest.mark.asyncio
    @hyp_settings(max_examples=50)
    @given(
        confidence=floats(min_value=0.7, max_value=1.0, allow_nan=False),
        net_edge=floats(
            min_value=-1.0, max_value=-0.001, allow_nan=False,
        ),
    )
    async def test_negative_edge_always_rejects(
        self, confidence: float, net_edge: float,
    ) -> None:
        portfolio = FakePortfolio(equity=10000)
        engine = RiskEngine(portfolio=portfolio)

        signal = make_candidate(confidence=confidence)
        decision = await engine.evaluate(signal, net_edge=net_edge)

        assert decision.approved is False
        assert decision.size == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# 5. Zero confidence produces zero position size
# ═══════════════════════════════════════════════════════════════════════════


class TestPropertyZeroConfidenceZeroSize:
    @hyp_settings(max_examples=50)
    @given(
        equity=floats(min_value=1.0, max_value=1_000_000, allow_nan=False),
    )
    def test_zero_confidence_returns_zero(self, equity: float) -> None:
        sizer = PositionSizer()
        size = sizer.determine_size(
            equity=equity, confidence=0.0, net_edge=0.1,
        )
        assert size == 0.0
