"""Unit tests for portfolio-level risk: correlated exposure, concentration.

Covers the correlation registry (event/direction/resolution mapping,
fail-closed conflicts), portfolio-level limits, the tracker's exposure
queries, and RiskEngine integration that prefers NO TRADE over breaking
a portfolio-level limit.
"""

from typing import Any
from datetime import UTC, datetime

import pytest

from app.config import settings
from app.portfolio.tracker import PortfolioTracker
from app.risk.correlation import (
    CorrelationGroup,
    CorrelationRegistry,
    PortfolioRiskLimits,
)
from app.risk.engine import RiskEngine
from app.strategies.base import Signal, StrategyDecision

# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


def make_candidate(
    market_id: str = "mkt_001",
    side: str = "YES",
    strategy: str = "test",
    **extra: Any,
) -> Signal:
    feat = {
        "market_id": market_id,
        "midpoint": 0.50,
        "spread": 0.02,
        "bid": 0.49,
        "ask": 0.51,
        "liquidity_score": 5000.0,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    feat.update(extra.pop("feature_snapshot", {}))
    return Signal(
        strategy=strategy,
        market_id=market_id,
        side=side,
        decision=StrategyDecision.CANDIDATE,
        model_probability=0.60,
        implied_probability=0.50,
        confidence=0.85,
        feature_snapshot=feat,
        **extra,
    )


class FakePortfolio(PortfolioTracker):
    def __init__(self, equity: float = 10000.0) -> None:
        super().__init__()
        self._equity = equity


def add_position(
    portfolio: PortfolioTracker,
    market_id: str,
    size: float,
    side: str = "YES",
    strategy: str = "test",
) -> None:
    price = 0.55 if side == "YES" else 0.45
    portfolio.add_trade(
        market_id=market_id, side=side, size=size, price=price, strategy=strategy,
    )


# ═══════════════════════════════════════════════════════════════════════════
# CorrelationRegistry
# ═══════════════════════════════════════════════════════════════════════════


class TestCorrelationRegistry:
    def test_unregistered_market_is_its_own_event(self) -> None:
        reg = CorrelationRegistry()
        assert reg.event_for("mkt_a") == "mkt_a"
        assert reg.direction_for("mkt_a") == 1.0
        assert reg.resolution_time_for("mkt_a") is None
        assert reg.markets_in_event("mkt_a") == {}
        assert reg.event_count() == 0
        assert reg.market_count() == 0

    def test_register_market_creates_event_on_demand(self) -> None:
        reg = CorrelationRegistry()
        reg.register_market("mkt_a", "election", direction=1.0)
        reg.register_market("mkt_b", "election", direction=-1.0)
        assert reg.event_for("mkt_a") == "election"
        assert reg.event_for("mkt_b") == "election"
        assert reg.direction_for("mkt_a") == 1.0
        assert reg.direction_for("mkt_b") == -1.0
        assert reg.markets_in_event("election") == {"mkt_a": 1.0, "mkt_b": -1.0}
        assert reg.event_count() == 1
        assert reg.market_count() == 2

    def test_register_event_with_resolution_time(self) -> None:
        reg = CorrelationRegistry()
        reg.register_event("election", resolution_time=2000.0)
        reg.register_market("mkt_a", "election", resolution_time=2000.0)
        reg.register_market("mkt_b", "election", resolution_time=2000.0)
        assert reg.resolution_time_for("mkt_a") == 2000.0
        assert reg.markets_with_resolution(2000.0) == {"mkt_a", "mkt_b"}
        assert reg.markets_with_resolution(3000.0) == set()

    def test_register_event_after_markets_inherits_resolution(self) -> None:
        reg = CorrelationRegistry()
        reg.register_market("mkt_a", "election")
        reg.register_event("election", resolution_time=2000.0)
        assert reg.resolution_time_for("mkt_a") == 2000.0

    def test_rejects_conflicting_event(self) -> None:
        reg = CorrelationRegistry()
        reg.register_market("mkt_a", "election")
        with pytest.raises(ValueError):
            reg.register_market("mkt_a", "other_event")

    def test_rejects_conflicting_direction(self) -> None:
        reg = CorrelationRegistry()
        reg.register_market("mkt_a", "election", direction=1.0)
        with pytest.raises(ValueError):
            reg.register_market("mkt_a", "election", direction=-1.0)

    def test_rejects_conflicting_resolution_time(self) -> None:
        reg = CorrelationRegistry()
        reg.register_market("mkt_a", "election", resolution_time=1000.0)
        with pytest.raises(ValueError):
            reg.register_market("mkt_a", "election", resolution_time=2000.0)

    def test_rejects_invalid_direction(self) -> None:
        reg = CorrelationRegistry()
        with pytest.raises(ValueError):
            reg.register_market("mkt_a", "election", direction=0.5)

    def test_duplicate_registration_is_idempotent(self) -> None:
        reg = CorrelationRegistry()
        reg.register_market("mkt_a", "election", direction=1.0)
        reg.register_market("mkt_a", "election", direction=1.0)
        assert reg.market_count() == 1
        assert reg.markets_in_event("election") == {"mkt_a": 1.0}

    def test_events_returns_copy(self) -> None:
        reg = CorrelationRegistry()
        reg.register_market("mkt_a", "election")
        snapshot = reg.events()
        assert isinstance(snapshot["election"], CorrelationGroup)
        snapshot["election"].markets["hacked"] = 1.0
        assert "hacked" not in reg.markets_in_event("election")


# ═══════════════════════════════════════════════════════════════════════════
# PortfolioRiskLimits
# ═══════════════════════════════════════════════════════════════════════════


class TestPortfolioRiskLimits:
    def test_event_exposure_within_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "max_event_exposure_pct", 0.03)
        assert PortfolioRiskLimits.check_event_exposure(250.0, 10000.0) == ""
        assert PortfolioRiskLimits.check_event_exposure(300.0, 10000.0) == ""

    def test_event_exposure_over_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "max_event_exposure_pct", 0.03)
        assert PortfolioRiskLimits.check_event_exposure(301.0, 10000.0) == (
            "EVENT_EXPOSURE_TOO_HIGH"
        )

    def test_strategy_exposure_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "max_strategy_exposure_pct", 0.04)
        assert PortfolioRiskLimits.check_strategy_exposure(399.0, 10000.0) == ""
        assert PortfolioRiskLimits.check_strategy_exposure(401.0, 10000.0) == (
            "STRATEGY_EXPOSURE_TOO_HIGH"
        )

    def test_directional_exposure_uses_absolute_value(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "max_directional_exposure_pct", 0.03)
        assert PortfolioRiskLimits.check_directional_exposure(300.0, 10000.0) == ""
        assert PortfolioRiskLimits.check_directional_exposure(-301.0, 10000.0) == (
            "DIRECTIONAL_EXPOSURE_TOO_HIGH"
        )
        assert PortfolioRiskLimits.check_directional_exposure(301.0, 10000.0) == (
            "DIRECTIONAL_EXPOSURE_TOO_HIGH"
        )

    def test_resolution_concentration_limit(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "max_resolution_exposure_pct", 0.05)
        assert PortfolioRiskLimits.check_resolution_concentration(500.0, 10000.0) == ""
        assert PortfolioRiskLimits.check_resolution_concentration(501.0, 10000.0) == (
            "RESOLUTION_CONCENTRATION_TOO_HIGH"
        )

    def test_zero_equity_never_approves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "max_event_exposure_pct", 0.03)
        assert PortfolioRiskLimits.check_event_exposure(0.01, 0.0) == (
            "EVENT_EXPOSURE_TOO_HIGH"
        )


# ═══════════════════════════════════════════════════════════════════════════
# PortfolioTracker exposure queries
# ═══════════════════════════════════════════════════════════════════════════


class TestTrackerExposure:
    def test_strategy_exposure_attributes_positions(self) -> None:
        pf = PortfolioTracker()
        add_position(pf, "mkt_a", 100, strategy="alpha")
        add_position(pf, "mkt_b", 50, strategy="beta")
        add_position(pf, "mkt_c", 30, strategy="")
        assert pf.strategy_exposure("alpha") == 100
        assert pf.strategy_exposure("beta") == 50
        assert pf.strategy_exposure("") == 30
        assert pf.strategy_exposure("gamma") == 0.0

    def test_strategy_exposure_survives_close_and_reopen(self) -> None:
        pf = PortfolioTracker()
        add_position(pf, "mkt_a", 100, strategy="alpha")
        pf.close_position("mkt_a", price=0.55)
        assert pf.strategy_exposure("alpha") == 0.0
        add_position(pf, "mkt_a", 75, strategy="alpha")
        assert pf.strategy_exposure("alpha") == 75

    def test_flip_position_preserves_strategy(self) -> None:
        pf = PortfolioTracker()
        add_position(pf, "mkt_a", 100, strategy="alpha")
        pf.add_trade("mkt_a", "NO", 150, price=0.45, strategy="alpha")
        assert pf.strategy_exposure("alpha") == 50

    def test_exposure_for_sums_across_markets(self) -> None:
        pf = PortfolioTracker()
        add_position(pf, "mkt_a", 100)
        add_position(pf, "mkt_b", 50)
        assert pf.exposure_for(["mkt_a", "mkt_b"]) == 150
        assert pf.exposure_for(["mkt_a", "mkt_b", "mkt_missing"]) == 150

    def test_exposure_for_deduplicates_duplicate_ids(self) -> None:
        pf = PortfolioTracker()
        add_position(pf, "mkt_a", 100)
        assert pf.exposure_for(["mkt_a", "mkt_a", "mkt_a"]) == 100

    def test_directional_exposure_sign_math(self) -> None:
        pf = PortfolioTracker()
        add_position(pf, "mkt_a", 100, side="YES")  # +1 market, YES -> +100
        add_position(pf, "mkt_b", 100, side="NO")  # +1 market, NO -> -100
        add_position(pf, "mkt_c", 100, side="YES")  # -1 market, YES -> -100
        directions = {"mkt_a": 1.0, "mkt_b": 1.0, "mkt_c": -1.0}
        assert pf.directional_exposure(directions) == -100.0

    def test_directional_hedge_cancels(self) -> None:
        pf = PortfolioTracker()
        add_position(pf, "mkt_a", 100, side="YES")
        add_position(pf, "mkt_b", 100, side="YES")
        directions = {"mkt_a": 1.0, "mkt_b": -1.0}
        assert pf.directional_exposure(directions) == 0.0

    def test_directional_exposure_ignores_unknown_markets(self) -> None:
        pf = PortfolioTracker()
        assert pf.directional_exposure({"mkt_missing": 1.0}) == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# RiskEngine integration — NO TRADE over breaking portfolio-level limits
# ═══════════════════════════════════════════════════════════════════════════


class TestRiskEngineCorrelation:
    def setup_method(self) -> None:
        self.portfolio = FakePortfolio(equity=10000)
        self.registry = CorrelationRegistry()
        self.engine = RiskEngine(
            portfolio=self.portfolio, registry=self.registry,
        )

    async def test_approves_without_registry(self) -> None:
        d = await self.engine.evaluate(
            make_candidate(), net_edge=0.08,
        )
        assert d.approved is True
        assert d.reason == ""

    async def test_approves_isolated_market(self) -> None:
        self.registry.register_market("mkt_a", "election")
        add_position(self.portfolio, "mkt_a", 100)
        d = await self.engine.evaluate(
            make_candidate(market_id="mkt_a"), net_edge=0.08,
        )
        assert d.approved is True

    async def test_rejects_correlated_event_concentration(self) -> None:
        self.registry.register_market("mkt_a", "election")
        self.registry.register_market("mkt_b", "election")
        self.registry.register_market("mkt_c", "election")
        add_position(self.portfolio, "mkt_a", 200)
        add_position(self.portfolio, "mkt_b", 200)
        # Event exposure = 400 + proposed ~85 > 300 (3% of 10000)
        d = await self.engine.evaluate(
            make_candidate(market_id="mkt_c"), net_edge=0.08,
        )
        assert d.approved is False
        assert d.reason == "EVENT_EXPOSURE_TOO_HIGH"
        assert d.size == 0.0

    async def test_independent_markets_do_not_sum_across_events(self) -> None:
        # Same sizes but different events -> each event bucket is small.
        # Positions carry no strategy label so only the event buckets
        # (per-market, small) and the total cap are exercised.
        add_position(self.portfolio, "mkt_a", 200, strategy="")
        add_position(self.portfolio, "mkt_b", 200, strategy="")
        d = await self.engine.evaluate(
            make_candidate(market_id="mkt_c"), net_edge=0.08,
        )
        assert d.approved is True

    async def test_rejects_strategy_concentration(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "max_event_exposure_pct", 0.99)
        monkeypatch.setattr(settings, "max_market_exposure_pct", 0.99)
        monkeypatch.setattr(settings, "max_total_exposure_pct", 0.99)
        add_position(self.portfolio, "mkt_a", 250, strategy="alpha")
        add_position(self.portfolio, "mkt_b", 250, strategy="alpha")
        d = await self.engine.evaluate(
            make_candidate(strategy="alpha"), net_edge=0.08,
        )
        assert d.approved is False
        assert d.reason == "STRATEGY_EXPOSURE_TOO_HIGH"

    async def test_rejects_directional_concentration(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "max_event_exposure_pct", 0.99)
        monkeypatch.setattr(settings, "max_strategy_exposure_pct", 0.99)
        monkeypatch.setattr(settings, "max_market_exposure_pct", 0.99)
        self.registry.register_market("mkt_a", "election")
        add_position(self.portfolio, "mkt_a", 400)
        d = await self.engine.evaluate(
            make_candidate(market_id="mkt_a"), net_edge=0.08,
        )
        assert d.approved is False
        assert d.reason == "DIRECTIONAL_EXPOSURE_TOO_HIGH"

    async def test_hedged_event_is_approved(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "max_event_exposure_pct", 0.99)
        monkeypatch.setattr(settings, "max_strategy_exposure_pct", 0.99)
        monkeypatch.setattr(settings, "max_market_exposure_pct", 0.99)
        self.registry.register_market("mkt_a", "election", direction=1.0)
        self.registry.register_market("mkt_b", "election", direction=-1.0)
        add_position(self.portfolio, "mkt_a", 200, side="YES")
        add_position(self.portfolio, "mkt_b", 200, side="YES")
        # +200 and -200 -> net directional ~0, never exceeds the cap.
        d = await self.engine.evaluate(
            make_candidate(market_id="mkt_a"), net_edge=0.08,
        )
        assert d.approved is True

    async def test_same_direction_rejected_where_hedge_approved(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "max_event_exposure_pct", 0.99)
        monkeypatch.setattr(settings, "max_strategy_exposure_pct", 0.99)
        monkeypatch.setattr(settings, "max_market_exposure_pct", 0.99)
        self.registry.register_market("mkt_a", "election", direction=1.0)
        self.registry.register_market("mkt_b", "election", direction=1.0)
        add_position(self.portfolio, "mkt_a", 200, side="YES")
        add_position(self.portfolio, "mkt_b", 200, side="YES")
        d = await self.engine.evaluate(
            make_candidate(market_id="mkt_a"), net_edge=0.08,
        )
        assert d.approved is False
        assert d.reason == "DIRECTIONAL_EXPOSURE_TOO_HIGH"

    async def test_rejects_resolution_concentration(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "max_event_exposure_pct", 0.99)
        monkeypatch.setattr(settings, "max_total_exposure_pct", 0.99)
        monkeypatch.setattr(settings, "max_strategy_exposure_pct", 0.99)
        monkeypatch.setattr(settings, "max_directional_exposure_pct", 0.99)
        self.registry.register_market(
            "mkt_a", "event_one", resolution_time=2000.0,
        )
        self.registry.register_market(
            "mkt_b", "event_two", resolution_time=2000.0,
        )
        self.registry.register_market(
            "mkt_c", "event_three", resolution_time=2000.0,
        )
        add_position(self.portfolio, "mkt_a", 300)
        add_position(self.portfolio, "mkt_b", 300)
        d = await self.engine.evaluate(
            make_candidate(market_id="mkt_c"), net_edge=0.08,
        )
        assert d.approved is False
        assert d.reason == "RESOLUTION_CONCENTRATION_TOO_HIGH"

    async def test_same_resolution_time_under_limit_approved(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "max_event_exposure_pct", 0.99)
        self.registry.register_market(
            "mkt_a", "event_one", resolution_time=2000.0,
        )
        self.registry.register_market(
            "mkt_b", "event_two", resolution_time=2000.0,
        )
        add_position(self.portfolio, "mkt_a", 100)
        d = await self.engine.evaluate(
            make_candidate(market_id="mkt_b"), net_edge=0.08,
        )
        assert d.approved is True

    async def test_correlation_metrics_in_approved_decision(self) -> None:
        self.registry.register_market("mkt_a", "election")
        add_position(self.portfolio, "mkt_a", 100)
        d = await self.engine.evaluate(
            make_candidate(market_id="mkt_a"), net_edge=0.08,
        )
        assert d.approved is True
        rm = d.risk_metrics
        assert rm["event_exposure"] > 100
        assert rm["strategy_exposure"] > 100
        assert rm["directional_exposure"] > 100
        assert rm["resolution_exposure"] == 0.0
