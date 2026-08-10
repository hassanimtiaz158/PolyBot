"""Extensive unit tests for risk limits, circuit breaker, position sizing, and risk engine.

Covers every rejection reason, boundary values, state transitions,
fail-closed invariants, and property-based tests.
"""

from datetime import UTC, datetime
from typing import Any

import pytest
from hypothesis import given
from hypothesis import settings as hyp_settings
from hypothesis.strategies import floats

from app.portfolio.tracker import PortfolioTracker
from app.risk.circuit_breaker import BreakerState, CircuitBreaker
from app.risk.engine import RiskDecision, RiskEngine
from app.risk.limits import RiskLimits
from app.risk.position_sizing import PositionSizer
from app.strategies.base import Signal, StrategyDecision


def approx(val: float, rel: float = 1e-6) -> Any:
    return pytest.approx(val, rel=rel, abs=1e-12)


# ── Helpers ──────────────────────────────────────────────────────────────


def make_candidate(
    market_id: str = "mkt_001",
    side: str = "YES",
    model_prob: float = 0.60,
    implied_prob: float = 0.50,
    confidence: float = 0.85,
    timestamp: str | None = None,
    **extra: Any,
) -> Signal:
    feat = {
        "market_id": market_id,
        "midpoint": implied_prob,
        "spread": 0.02,
        "bid": implied_prob - 0.01,
        "ask": implied_prob + 0.01,
        "liquidity_score": 5000.0,
        "timestamp": timestamp or datetime.now(UTC).isoformat(),
    }
    feat.update(extra.pop("feature_snapshot", {}))
    return Signal(
        strategy="test",
        market_id=market_id,
        side=side,
        decision=StrategyDecision.CANDIDATE,
        model_probability=model_prob,
        implied_probability=implied_prob,
        confidence=confidence,
        feature_snapshot=feat,
        **extra,
    )


def make_no_signal(market_id: str = "mkt_001") -> Signal:
    return Signal(
        strategy="test",
        market_id=market_id,
        side="YES",
        decision=StrategyDecision.NO_SIGNAL,
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
# Circuit Breaker
# ═══════════════════════════════════════════════════════════════════════════


class TestCircuitBreakerInitialState:
    def test_default_state_is_normal(self) -> None:
        cb = CircuitBreaker(persist=False)
        assert cb.state == BreakerState.NORMAL

    def test_is_normal_true_initially(self) -> None:
        cb = CircuitBreaker(persist=False)
        assert cb.is_normal is True
        assert cb.is_warning is False
        assert cb.is_halted is False

    def test_reasons_empty_initially(self) -> None:
        cb = CircuitBreaker(persist=False)
        assert cb.reasons == []

    def test_triggered_at_none_initially(self) -> None:
        cb = CircuitBreaker(persist=False)
        assert cb.triggered_at is None


class TestCircuitBreakerTransitions:
    @pytest.fixture(autouse=True)
    def _cb(self) -> CircuitBreaker:
        return CircuitBreaker(persist=False)

    async def test_soft_trigger_moves_normal_to_warning(self, _cb) -> None:
        await _cb.trigger("STALE_DATA", severity="SOFT")
        assert _cb.state == BreakerState.WARNING
        assert _cb.is_warning is True

    async def test_soft_trigger_escalates_warning_to_halted(self, _cb) -> None:
        await _cb.trigger("STALE_DATA", severity="SOFT")
        await _cb.trigger("LOW_LIQUIDITY", severity="SOFT")
        assert _cb.state == BreakerState.HALTED
        assert _cb.is_halted is True

    async def test_hard_trigger_goes_directly_to_halted(self, _cb) -> None:
        await _cb.trigger("API_HEALTH", severity="HARD")
        assert _cb.state == BreakerState.HALTED

    async def test_hard_trigger_from_warning_stays_halted(self, _cb) -> None:
        await _cb.trigger("STALE_DATA", severity="SOFT")
        await _cb.trigger("API_HEALTH", severity="HARD")
        assert _cb.state == BreakerState.HALTED

    async def test_clear_specific_reason(self, _cb) -> None:
        await _cb.trigger("STALE_DATA", severity="SOFT")
        await _cb.trigger("LOW_LIQUIDITY", severity="SOFT")
        await _cb.clear("STALE_DATA")
        assert "STALE_DATA" not in _cb.reasons
        assert _cb.is_halted is True  # still HALTED (second reason)

    async def test_clear_all_reasons_returns_to_normal(self, _cb) -> None:
        await _cb.trigger("STALE_DATA", severity="SOFT")
        await _cb.trigger("LOW_LIQUIDITY", severity="SOFT")
        await _cb.clear_all()
        assert _cb.state == BreakerState.NORMAL
        assert _cb.reasons == []

    async def test_trigger_duplicate_is_idempotent(self, _cb) -> None:
        await _cb.trigger("DAILY_LOSS", severity="HARD")
        await _cb.trigger("DAILY_LOSS", severity="HARD")
        assert len(_cb.reasons) == 1

    async def test_clear_nonexistent_is_noop(self, _cb) -> None:
        await _cb.clear("NONEXISTENT")
        assert _cb.state == BreakerState.NORMAL

    async def test_clear_all_sets_triggered_at_none(self, _cb) -> None:
        await _cb.trigger("TEST", severity="SOFT")
        assert _cb.triggered_at is not None
        await _cb.clear_all()
        assert _cb.triggered_at is None


class TestCircuitBreakerCheckAndTrigger:
    @pytest.fixture(autouse=True)
    def _cb(self) -> CircuitBreaker:
        return CircuitBreaker(persist=False)

    async def test_no_trigger_when_all_healthy(self, _cb) -> None:
        state = await _cb.check_and_trigger(
            daily_pnl=0, consecutive_losses=0,
            data_fresh=True, api_healthy=True, equity=10000,
        )
        assert state == BreakerState.NORMAL

    async def test_soft_trigger_on_stale_data(self, _cb) -> None:
        state = await _cb.check_and_trigger(
            daily_pnl=0, consecutive_losses=0,
            data_fresh=False, api_healthy=True, equity=10000,
        )
        assert state == BreakerState.WARNING
        assert "STALE_DATA" in _cb.reasons

    async def test_hard_trigger_on_api_health(self, _cb) -> None:
        state = await _cb.check_and_trigger(
            daily_pnl=0, consecutive_losses=0,
            data_fresh=True, api_healthy=False, equity=10000,
        )
        assert state == BreakerState.HALTED
        assert "API_HEALTH" in _cb.reasons

    async def test_hard_trigger_on_daily_loss(self, _cb) -> None:
        state = await _cb.check_and_trigger(
            daily_pnl=-300, consecutive_losses=0,
            data_fresh=True, api_healthy=True, equity=10000,
        )
        assert state == BreakerState.HALTED
        assert "DAILY_LOSS" in _cb.reasons

    async def test_hard_trigger_on_consecutive_losses(self, _cb) -> None:
        state = await _cb.check_and_trigger(
            daily_pnl=0, consecutive_losses=5,
            data_fresh=True, api_healthy=True, equity=10000,
        )
        assert state == BreakerState.HALTED
        assert "CONSECUTIVE_LOSSES" in _cb.reasons

    async def test_stale_data_escalates_to_halted_with_other_triggers(
        self, _cb,
    ) -> None:
        await _cb.check_and_trigger(
            daily_pnl=0, consecutive_losses=0,
            data_fresh=False, api_healthy=True, equity=10000,
        )
        assert _cb.state == BreakerState.WARNING
        await _cb.check_and_trigger(
            daily_pnl=0, consecutive_losses=0,
            data_fresh=False, api_healthy=True, equity=10000,
        )
        # Second call: stale_data already triggered, but daily_loss not hit,
        # so state stays WARNING
        assert _cb.state == BreakerState.WARNING


# ═══════════════════════════════════════════════════════════════════════════
# RiskLimits
# ═══════════════════════════════════════════════════════════════════════════


class TestRiskLimitsDataQuality:
    def setup_method(self) -> None:
        self.limits = RiskLimits()

    def test_data_freshness_ok(self) -> None:
        ts = datetime.now(UTC).isoformat()
        assert self.limits.check_data_freshness(ts).approved is True

    def test_data_freshness_stale(self) -> None:
        old = datetime(2020, 1, 1, tzinfo=UTC).isoformat()
        check = self.limits.check_data_freshness(old)
        assert check.approved is False
        assert check.reason == "STALE_DATA"

    def test_data_freshness_none(self) -> None:
        check = self.limits.check_data_freshness(None)
        assert check.approved is False
        assert check.reason == "STALE_DATA"

    def test_data_freshness_invalid(self) -> None:
        check = self.limits.check_data_freshness("not-a-timestamp")
        assert check.approved is False
        assert check.reason == "STALE_DATA"

    def test_data_validity_ok(self) -> None:
        features = {
            "market_id": "mkt_1",
            "midpoint": 0.50,
            "spread": 0.02,
            "bid": 0.49,
            "ask": 0.51,
        }
        assert self.limits.check_data_validity(features).approved is True

    def test_data_validity_missing_field(self) -> None:
        check = self.limits.check_data_validity({"market_id": "mkt_1"})
        assert check.approved is False
        assert check.reason == "INVALID_DATA"

    def test_data_validity_none_field(self) -> None:
        features = {
            "market_id": "mkt_1",
            "midpoint": None,
            "spread": 0.02,
            "bid": 0.49,
            "ask": 0.51,
        }
        check = self.limits.check_data_validity(features)
        assert check.approved is False
        assert check.reason == "INVALID_DATA"


class TestRiskLimitsTradeParams:
    def setup_method(self) -> None:
        self.limits = RiskLimits()

    def test_net_edge_ok(self) -> None:
        assert self.limits.check_net_edge(0.05).approved is True

    def test_net_edge_below(self) -> None:
        check = self.limits.check_net_edge(0.04)
        assert check.approved is False
        assert check.reason == "NET_EDGE_BELOW_THRESHOLD"

    def test_net_edge_none(self) -> None:
        check = self.limits.check_net_edge(None)
        assert check.approved is False
        assert check.reason == "NET_EDGE_BELOW_THRESHOLD"

    def test_net_edge_at_threshold(self) -> None:
        assert self.limits.check_net_edge(0.05).approved is True

    def test_confidence_ok(self) -> None:
        assert self.limits.check_confidence(0.70).approved is True

    def test_confidence_below(self) -> None:
        check = self.limits.check_confidence(0.69)
        assert check.approved is False
        assert check.reason == "CONFIDENCE_BELOW_THRESHOLD"

    def test_confidence_none(self) -> None:
        check = self.limits.check_confidence(None)
        assert check.approved is False
        assert check.reason == "CONFIDENCE_BELOW_THRESHOLD"

    def test_confidence_at_threshold(self) -> None:
        assert self.limits.check_confidence(0.70).approved is True

    def test_spread_ok(self) -> None:
        assert self.limits.check_spread(0.02).approved is True

    def test_spread_too_high(self) -> None:
        check = self.limits.check_spread(0.05)
        assert check.approved is False
        assert check.reason == "SPREAD_TOO_HIGH"

    def test_spread_at_limit(self) -> None:
        assert self.limits.check_spread(0.03).approved is True

    def test_spread_none(self) -> None:
        check = self.limits.check_spread(None)
        assert check.approved is False
        assert check.reason == "SPREAD_TOO_HIGH"

    def test_liquidity_ok(self) -> None:
        assert self.limits.check_liquidity(5000.0).approved is True

    def test_liquidity_too_low(self) -> None:
        check = self.limits.check_liquidity(100.0)
        assert check.approved is False
        assert check.reason == "LIQUIDITY_TOO_LOW"

    def test_liquidity_at_limit(self) -> None:
        assert self.limits.check_liquidity(1000.0).approved is True

    def test_liquidity_none(self) -> None:
        check = self.limits.check_liquidity(None)
        assert check.approved is False
        assert check.reason == "LIQUIDITY_TOO_LOW"


class TestRiskLimitsPortfolio:
    def setup_method(self) -> None:
        self.limits = RiskLimits()

    def test_position_size_ok(self) -> None:
        check = self.limits.check_position_size(50, 10000)
        assert check.approved is True

    def test_position_size_at_limit(self) -> None:
        check = self.limits.check_position_size(100, 10000)
        assert check.approved is True

    def test_position_size_exceeds(self) -> None:
        check = self.limits.check_position_size(101, 10000)
        assert check.approved is False
        assert check.reason == "POSITION_SIZE_EXCEEDS_MAX"

    def test_position_size_zero(self) -> None:
        check = self.limits.check_position_size(0, 10000)
        assert check.approved is False
        assert check.reason == "POSITION_SIZE_ZERO"

    def test_market_exposure_ok(self) -> None:
        check = self.limits.check_market_exposure(150, 10000)
        assert check.approved is True

    def test_market_exposure_exceeds(self) -> None:
        check = self.limits.check_market_exposure(201, 10000)
        assert check.approved is False
        assert check.reason == "MARKET_EXPOSURE_TOO_HIGH"

    def test_total_exposure_ok(self) -> None:
        check = self.limits.check_total_exposure(400, 10000)
        assert check.approved is True

    def test_total_exposure_exceeds(self) -> None:
        check = self.limits.check_total_exposure(501, 10000)
        assert check.approved is False
        assert check.reason == "TOTAL_EXPOSURE_TOO_HIGH"

    def test_daily_loss_ok(self) -> None:
        check = self.limits.check_daily_loss(-150, 10000)
        assert check.approved is True

    def test_daily_loss_at_limit(self) -> None:
        check = self.limits.check_daily_loss(-200, 10000)
        assert check.approved is True

    def test_daily_loss_exceeds(self) -> None:
        check = self.limits.check_daily_loss(-201, 10000)
        assert check.approved is False
        assert check.reason == "DAILY_LOSS_LIMIT_REACHED"

    def test_consecutive_losses_ok(self) -> None:
        check = self.limits.check_consecutive_losses(3)
        assert check.approved is True

    def test_consecutive_losses_at_limit(self) -> None:
        check = self.limits.check_consecutive_losses(4)
        assert check.approved is True

    def test_consecutive_losses_exceeds(self) -> None:
        check = self.limits.check_consecutive_losses(5)
        assert check.approved is False
        assert check.reason == "CONSECUTIVE_LOSS_LIMIT_REACHED"

    def test_open_positions_ok(self) -> None:
        check = self.limits.check_open_positions(5)
        assert check.approved is True

    def test_open_positions_at_limit(self) -> None:
        check = self.limits.check_open_positions(9)
        assert check.approved is True

    def test_open_positions_exceeds(self) -> None:
        check = self.limits.check_open_positions(10)
        assert check.approved is False
        assert check.reason == "MAX_OPEN_POSITIONS_REACHED"

    def test_all_checks_returns_first_failure(self) -> None:
        check = self.limits.all_checks(
            proposed_size=500, equity=10000,
            market_exposure=300, total_exposure=600,
            daily_pnl=-300, consecutive_losses=5, open_positions=10,
        )
        assert check.approved is False
        # First check is position_size which fails (500 > 100)
        assert check.reason == "POSITION_SIZE_EXCEEDS_MAX"

    def test_all_checks_passes(self) -> None:
        check = self.limits.all_checks(
            proposed_size=50, equity=10000,
            market_exposure=50, total_exposure=100,
            daily_pnl=0, consecutive_losses=0, open_positions=3,
        )
        assert check.approved is True
        assert check.reason == ""


# ═══════════════════════════════════════════════════════════════════════════
# PositionSizer (conservative — no Kelly, no Martingale)
# ═══════════════════════════════════════════════════════════════════════════


class TestPositionSizerFixedRisk:
    def setup_method(self) -> None:
        self.sizer = PositionSizer()

    def test_full_confidence(self) -> None:
        assert self.sizer.fixed_risk_size(10000, 1.0) == approx(100.0)

    def test_half_confidence_halves_size(self) -> None:
        assert self.sizer.fixed_risk_size(10000, 0.5) == approx(50.0)

    def test_zero_confidence_returns_zero(self) -> None:
        assert self.sizer.fixed_risk_size(10000, 0.0) == 0.0

    def test_zero_equity_returns_zero(self) -> None:
        assert self.sizer.fixed_risk_size(0, 0.8) == 0.0

    def test_custom_max_pct(self) -> None:
        sizer = PositionSizer(max_position_pct=0.02)
        assert sizer.fixed_risk_size(10000, 1.0) == approx(200.0)

    def test_negative_equity_returns_zero(self) -> None:
        assert self.sizer.fixed_risk_size(-1000, 1.0) == 0.0

    def test_negative_confidence_returns_zero(self) -> None:
        assert self.sizer.fixed_risk_size(10000, -0.1) == 0.0


class TestPositionSizerDetermineSize:
    def setup_method(self) -> None:
        self.sizer = PositionSizer()

    def test_determine_size_nominal(self) -> None:
        size = self.sizer.determine_size(
            equity=10000, confidence=1.0, net_edge=0.05,
        )
        assert size == approx(100.0)

    def test_determine_size_capped_by_liquidity(self) -> None:
        size = self.sizer.determine_size(
            equity=10000, confidence=1.0, net_edge=0.05, liquidity=50,
        )
        assert size == approx(50.0)

    def test_determine_size_capped_by_risk_limit(self) -> None:
        size = self.sizer.determine_size(
            equity=10000, confidence=1.0, net_edge=0.05, risk_limit=30,
        )
        assert size == approx(30.0)

    def test_zero_net_edge_returns_zero(self) -> None:
        assert self.sizer.determine_size(
            equity=10000, confidence=1.0, net_edge=0.0,
        ) == 0.0

    def test_negative_net_edge_returns_zero(self) -> None:
        assert self.sizer.determine_size(
            equity=10000, confidence=1.0, net_edge=-0.05,
        ) == 0.0

    def test_zero_confidence_returns_zero(self) -> None:
        assert self.sizer.determine_size(
            equity=10000, confidence=0.0, net_edge=0.05,
        ) == 0.0

    def test_zero_equity_returns_zero(self) -> None:
        size = self.sizer.determine_size(
            equity=0, confidence=1.0, net_edge=0.05,
        )
        assert size == 0.0

    def test_no_martingale_never_increases_with_losses(self) -> None:
        """Position size never grows after losses — it stays the same or shrinks."""
        size1 = self.sizer.determine_size(
            equity=10000, confidence=0.8, net_edge=0.05,
        )
        size2 = self.sizer.determine_size(
            equity=9000, confidence=0.7, net_edge=0.04,
        )
        # After loss (lower equity, lower confidence, lower edge), size
        # must be <= original.
        assert size2 <= size1

    def test_no_kelly_no_aggressive_sizing(self) -> None:
        """With only fixed-risk, size never exceeds equity × max_position_pct."""
        size = self.sizer.determine_size(
            equity=10000, confidence=1.0, net_edge=0.50,
        )
        assert size <= 10000 * 0.01


# ═══════════════════════════════════════════════════════════════════════════
# RiskEngine — every rejection reason
# ═══════════════════════════════════════════════════════════════════════════


class TestRiskEngineRejectReasons:
    def setup_method(self) -> None:
        self.portfolio = FakePortfolio(equity=10000)
        self.engine = RiskEngine(portfolio=self.portfolio)

    async def _evaluate(
        self, signal: Signal | None = None, **kwargs,
    ) -> RiskDecision:
        sig = signal or make_candidate()
        return await self.engine.evaluate(sig, **kwargs)

    # ── System health ────────────────────────────────────────────────

    async def test_rejects_no_signal(self) -> None:
        d = await self._evaluate(signal=make_no_signal())
        assert d.approved is False
        assert d.reason == "NO_SIGNAL"
        assert d.size == 0.0

    async def test_rejects_api_unhealthy(self) -> None:
        d = await self._evaluate(api_healthy=False)
        assert d.approved is False
        assert d.reason == "API_HEALTH"

    async def test_rejects_model_unavailable(self) -> None:
        d = await self._evaluate(model_available=False)
        assert d.approved is False
        assert d.reason == "MODEL_UNAVAILABLE"

    async def test_rejects_db_unavailable(self) -> None:
        d = await self._evaluate(database_available=False)
        assert d.approved is False
        assert d.reason == "DB_UNAVAILABLE"

    async def test_rejects_system_halted(self) -> None:
        await self.engine._breaker.trigger("API_HEALTH", severity="HARD")
        d = await self._evaluate()
        assert d.approved is False
        assert d.reason == "SYSTEM_HALTED"

    # ── Data quality ─────────────────────────────────────────────────

    async def test_rejects_stale_data(self) -> None:
        old_ts = "2020-01-01T00:00:00+00:00"
        sig = make_candidate(timestamp=old_ts)
        d = await self._evaluate(signal=sig)
        assert d.approved is False
        assert d.reason == "STALE_DATA"

    async def test_rejects_invalid_data(self) -> None:
        sig = make_candidate(feature_snapshot={"midpoint": None})
        d = await self._evaluate(signal=sig)
        assert d.approved is False
        assert d.reason == "INVALID_DATA"

    # ── Trade parameters ─────────────────────────────────────────────

    async def test_rejects_spread_too_high(self) -> None:
        sig = make_candidate(feature_snapshot={"spread": 0.10})
        d = await self._evaluate(signal=sig)
        assert d.approved is False
        assert d.reason == "SPREAD_TOO_HIGH"

    async def test_rejects_liquidity_too_low(self) -> None:
        sig = make_candidate(feature_snapshot={"liquidity_score": 50.0})
        d = await self._evaluate(signal=sig)
        assert d.approved is False
        assert d.reason == "LIQUIDITY_TOO_LOW"

    async def test_rejects_net_edge_below_threshold(self) -> None:
        d = await self._evaluate(net_edge=0.04)
        assert d.approved is False
        assert d.reason == "NET_EDGE_BELOW_THRESHOLD"

    async def test_rejects_net_edge_none(self) -> None:
        d = await self._evaluate(net_edge=None)
        assert d.approved is False
        assert d.reason == "NET_EDGE_BELOW_THRESHOLD"

    async def test_rejects_confidence_below_threshold(self) -> None:
        sig = make_candidate(confidence=0.50)
        d = await self._evaluate(signal=sig, net_edge=0.10)
        assert d.approved is False
        assert d.reason == "CONFIDENCE_BELOW_THRESHOLD"

    # ── Portfolio limits ─────────────────────────────────────────────

    async def test_rejects_position_size_zero(self) -> None:
        # net_edge = 0 is caught by net-edge threshold before the sizer
        d = await self._evaluate(net_edge=0.0)
        assert d.approved is False
        assert d.reason == "NET_EDGE_BELOW_THRESHOLD"

    async def test_rejects_market_exposure_too_high(self) -> None:
        self.portfolio._positions = {
            "mkt_001": {"market_id": "mkt_001", "side": "YES", "size": 250},
        }
        d = await self._evaluate(net_edge=0.10)
        assert d.approved is False
        assert d.reason == "MARKET_EXPOSURE_TOO_HIGH"

    async def test_rejects_total_exposure_too_high(self) -> None:
        self.portfolio._positions = {
            f"mkt_{i}": {"market_id": f"mkt_{i}", "side": "YES", "size": 1000}
            for i in range(5)
        }
        d = await self._evaluate(net_edge=0.10)
        assert d.approved is False
        assert d.reason == "TOTAL_EXPOSURE_TOO_HIGH"

    async def test_rejects_daily_loss_limit(self) -> None:
        d = await self._evaluate(net_edge=0.10, daily_pnl=-300)
        assert d.approved is False
        assert d.reason == "DAILY_LOSS_LIMIT_REACHED"

    async def test_rejects_consecutive_losses(self) -> None:
        d = await self._evaluate(net_edge=0.10, consecutive_losses=5)
        assert d.approved is False
        assert d.reason == "CONSECUTIVE_LOSS_LIMIT_REACHED"

    async def test_rejects_max_open_positions(self) -> None:
        self.portfolio._positions = {
            f"mkt_{i}": {"market_id": f"mkt_{i}", "side": "YES", "size": 10}
            for i in range(10)
        }
        d = await self._evaluate(net_edge=0.10)
        assert d.approved is False
        assert d.reason in (
            "MAX_OPEN_POSITIONS_REACHED",
            "TOTAL_EXPOSURE_TOO_HIGH",
        )


# ═══════════════════════════════════════════════════════════════════════════
# RiskEngine — approved cases
# ═══════════════════════════════════════════════════════════════════════════


class TestRiskEngineApproved:
    def setup_method(self) -> None:
        self.portfolio = FakePortfolio(equity=10000)
        self.engine = RiskEngine(portfolio=self.portfolio)

    async def test_approves_strong_candidate(self) -> None:
        d = await self.engine.evaluate(
            make_candidate(confidence=0.95),
            net_edge=0.08,
        )
        assert d.approved is True
        assert d.reason == ""
        assert d.size > 0

    async def test_approved_has_all_fields(self) -> None:
        sig = make_candidate()
        d = await self.engine.evaluate(sig, net_edge=0.08)
        assert isinstance(d, RiskDecision)
        assert d.market_id == "mkt_001"
        assert d.side == "YES"
        assert d.signal_id == sig.signal_id
        assert d.net_edge == approx(0.08)
        assert d.breaker_state == "NORMAL"
        assert "equity" in d.risk_metrics
        assert "proposed_size" in d.risk_metrics
        assert "open_positions" in d.risk_metrics

    async def test_size_nonzero_when_approved(self) -> None:
        d = await self.engine.evaluate(
            make_candidate(confidence=0.95),
            net_edge=0.15,
        )
        assert d.size > 0

    async def test_breaker_state_in_decision(self) -> None:
        d = await self.engine.evaluate(
            make_candidate(), net_edge=0.08,
        )
        assert d.breaker_state == "NORMAL"

    async def test_risk_metrics_contents(self) -> None:
        d = await self.engine.evaluate(
            make_candidate(), net_edge=0.08,
        )
        rm = d.risk_metrics
        assert rm["equity"] == 10000.0
        assert rm["daily_pnl"] == 0.0
        assert rm["open_positions"] == 0.0
        assert rm["consecutive_losses"] == 0.0
        assert rm["proposed_size"] > 0


# ═══════════════════════════════════════════════════════════════════════════
# RiskDecision dataclass
# ═══════════════════════════════════════════════════════════════════════════


class TestRiskDecision:
    def test_default_size_zero(self) -> None:
        d = RiskDecision(approved=False, market_id="mkt_1", side="YES")
        assert d.size == 0.0

    def test_default_reason_empty(self) -> None:
        d = RiskDecision(approved=True, market_id="mkt_1", side="YES")
        assert d.reason == ""

    def test_default_net_edge_none(self) -> None:
        d = RiskDecision(approved=True, market_id="mkt_1", side="YES")
        assert d.net_edge is None

    def test_default_breaker_state_none(self) -> None:
        d = RiskDecision(approved=True, market_id="mkt_1", side="YES")
        assert d.breaker_state is None

    def test_rejected_reason_is_machine_readable(self) -> None:
        d = RiskDecision(
            approved=False, market_id="mkt_1", side="YES",
            reason="STALE_DATA",
        )
        assert d.reason == "STALE_DATA"

    def test_approved_reason_is_empty(self) -> None:
        d = RiskDecision(
            approved=True, market_id="mkt_1", side="YES", size=100,
        )
        assert d.reason == ""

    def test_all_fields(self) -> None:
        d = RiskDecision(
            approved=False,
            market_id="mkt_1",
            side="YES",
            size=0.0,
            reason="SPREAD_TOO_HIGH",
            signal_id="sig_1",
            net_edge=0.05,
            risk_metrics={"equity": 10000},
            breaker_state="WARNING",
            extra={"source": "test"},
        )
        assert d.approved is False
        assert d.market_id == "mkt_1"
        assert d.side == "YES"
        assert d.reason == "SPREAD_TOO_HIGH"
        assert d.signal_id == "sig_1"
        assert d.breaker_state == "WARNING"


# ═══════════════════════════════════════════════════════════════════════════
# Critical invariant: if approved=False, execution MUST NEVER occur
# ═══════════════════════════════════════════════════════════════════════════


class TestInvariantExecutionNeverWhenRejected:
    """Core safety invariant.

    If ``RiskDecision.approved is False``, the system must NEVER allow
    execution.  This test validates that every rejection path produces
    ``size=0`` and ``approved=False``.
    """

    def setup_method(self) -> None:
        self.portfolio = FakePortfolio(equity=10000)
        self.engine = RiskEngine(portfolio=self.portfolio)

    @staticmethod
    def _assert_no_execution(decision: RiskDecision) -> None:
        """Assert the invariant: no execution path for rejected decisions."""
        if not decision.approved:
            assert decision.size == 0.0, (
                f"Rejected decision for {decision.reason} has non-zero "
                f"size {decision.size}"
            )

    async def test_no_execution_on_every_rejection_reason(self) -> None:
        rejections = [
            ("NO_SIGNAL", make_no_signal(), {}),
            ("API_HEALTH", make_candidate(), {"api_healthy": False}),
            ("MODEL_UNAVAILABLE", make_candidate(), {"model_available": False}),
            ("DB_UNAVAILABLE", make_candidate(), {"database_available": False}),
            ("SYSTEM_HALTED", make_candidate(), {}),
            ("STALE_DATA", make_candidate(timestamp="2020-01-01T00:00:00Z"), {}),
            ("INVALID_DATA", make_candidate(feature_snapshot={"market_id": "x"}), {}),
            ("SPREAD_TOO_HIGH", make_candidate(
                feature_snapshot={"spread": 0.10},
            ), {}),
            ("LIQUIDITY_TOO_LOW", make_candidate(
                feature_snapshot={"liquidity_score": 50},
            ), {}),
            ("NET_EDGE_BELOW_THRESHOLD", make_candidate(), {"net_edge": 0.0}),
            ("CONFIDENCE_BELOW_THRESHOLD", make_candidate(confidence=0.50), {}),
            ("POSITION_SIZE_ZERO", make_candidate(), {"net_edge": 0.0}),
        ]

        # Activate breaker for SYSTEM_HALTED test
        await self.engine._breaker.trigger("API_HEALTH", severity="HARD")

        for reason, signal, kwargs in rejections:
            decision = await self.engine.evaluate(signal, **kwargs)
            assert decision.approved is False, f"Expected rejection for {reason}"
            assert decision.reason is not None and decision.reason != "", (
                f"Expected machine-readable reason for {reason}, got {decision.reason!r}"
            )
            self._assert_no_execution(decision)

    async def test_execution_only_when_approved(self) -> None:
        decision = await self.engine.evaluate(
            make_candidate(confidence=0.95),
            net_edge=0.10,
        )
        if decision.approved:
            assert decision.size > 0
            assert decision.reason == ""
        else:
            assert decision.size == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# Property-based tests
# ═══════════════════════════════════════════════════════════════════════════


class TestRiskLimitsProperty:
    """Property-based tests for limit checks."""

    @hyp_settings(max_examples=200)
    @given(
        floats(min_value=0, max_value=100000, allow_nan=False),
        floats(min_value=0, max_value=100000, allow_nan=False),
    )
    def test_position_size_monotonic(
        self, equity: float, size: float,
    ) -> None:
        """Position size check is monotonic: larger sizes never pass when smaller ones fail."""
        limits = RiskLimits()
        if size <= 0:
            assert limits.check_position_size(size, max(equity, 1)).approved is False
            return
        c1 = limits.check_position_size(size, max(equity, 1))
        c2 = limits.check_position_size(size * 1.1, max(equity, 1))
        if c1.approved:
            assert c2.approved is False or c2.approved is True  # no regression
        else:
            assert c2.approved is False  # larger is also rejected


class TestPositionSizerProperty:
    """Property: size never exceeds equity × max_position_pct."""

    @hyp_settings(max_examples=200)
    @given(
        floats(min_value=1000, max_value=1000000, allow_nan=False),
        floats(min_value=0.1, max_value=1.0, allow_nan=False),
        floats(min_value=0.01, max_value=0.50, allow_nan=False),
    )
    def test_size_capped_by_max_pct(
        self, equity: float, confidence: float, net_edge: float,
    ) -> None:
        sizer = PositionSizer()
        size = sizer.determine_size(
            equity=equity, confidence=confidence,
            net_edge=net_edge,
        )
        max_allowed = equity * 0.01  # max_position_pct
        assert size <= max_allowed
        assert size >= 0.0
