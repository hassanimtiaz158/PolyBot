"""Unit tests for expected value and cost estimation."""

from typing import Any

import pytest

from app.ev.costs import (
    DEFAULT_FEE_RATE,
    FEE_RATES_BY_CATEGORY,
    CostEstimator,
)
from app.ev.expected_value import EVResult, ExpectedValueEngine

# ── Helpers ──────────────────────────────────────────────────────────────


def approx(val: float, rel: float = 1e-6) -> Any:
    return pytest.approx(val, rel=rel, abs=1e-12)


# ── CostEstimator ────────────────────────────────────────────────────────


class TestSpreadCost:
    def setup_method(self) -> None:
        self.est = CostEstimator()

    def test_spread_cost_half_spread(self) -> None:
        cost = self.est.spread_cost(0.02, 0.50)
        assert cost == approx(0.01)

    def test_spread_cost_zero_price(self) -> None:
        assert self.est.spread_cost(0.02, 0.0) == 0.0

    def test_spread_cost_zero_spread(self) -> None:
        assert self.est.spread_cost(0.0, 0.50) == 0.0

    def test_spread_cost_negative(self) -> None:
        assert self.est.spread_cost(-0.01, 0.50) == 0.0

    def test_spread_cost_wide_spread(self) -> None:
        cost = self.est.spread_cost(0.10, 0.50)
        assert cost == approx(0.05)


class TestSlippageCost:
    def setup_method(self) -> None:
        self.est = CostEstimator()

    def test_no_slippage_when_size_within_depth(self) -> None:
        cost = self.est.slippage_cost(100, 500, 0.02, 0.50)
        assert cost == 0.0

    def test_slippage_when_size_exceeds_depth(self) -> None:
        cost = self.est.slippage_cost(1000, 500, 0.02, 0.50)
        expected = (1000 / 500 - 1.0) * 0.02 / 2.0
        assert cost == approx(expected)

    def test_slippage_capped_at_three_percent(self) -> None:
        cost = self.est.slippage_cost(100_000, 100, 0.05, 0.50)
        assert cost == approx(0.03)

    def test_slippage_zero_depth(self) -> None:
        assert self.est.slippage_cost(100, 0, 0.02, 0.50) == 0.0

    def test_slippage_zero_price(self) -> None:
        assert self.est.slippage_cost(100, 500, 0.02, 0.0) == 0.0


class TestFeeCost:
    def setup_method(self) -> None:
        self.est = CostEstimator()

    def test_fee_at_midpoint(self) -> None:
        cost = self.est.fee_cost(0.50, 0.05)
        assert cost == approx(0.05 * 0.50 * 0.50)

    def test_fee_low_price(self) -> None:
        cost = self.est.fee_cost(0.10, 0.05)
        assert cost == approx(0.05 * 0.10 * 0.90)

    def test_fee_high_price(self) -> None:
        cost = self.est.fee_cost(0.90, 0.05)
        assert cost == approx(0.05 * 0.90 * 0.10)

    def test_fee_extreme_price(self) -> None:
        assert self.est.fee_cost(0.01, 0.05) == approx(0.05 * 0.01 * 0.99)
        assert self.est.fee_cost(0.99, 0.05) == approx(0.05 * 0.99 * 0.01)

    def test_fee_zero_rate(self) -> None:
        assert self.est.fee_cost(0.50, 0.0) == 0.0

    def test_fee_crypto_rate(self) -> None:
        cost = self.est.fee_cost(0.50, 0.07)
        assert cost == approx(0.07 * 0.50 * 0.50)

    def test_fee_symmetric(self) -> None:
        assert self.est.fee_cost(0.30, 0.05) == approx(
            self.est.fee_cost(0.70, 0.05)
        )

    def test_default_fee_rate_defined(self) -> None:
        assert DEFAULT_FEE_RATE == 0.05

    def test_fee_rates_by_category(self) -> None:
        assert FEE_RATES_BY_CATEGORY["crypto"] == 0.07
        assert FEE_RATES_BY_CATEGORY["politics"] == 0.04
        assert FEE_RATES_BY_CATEGORY["geopolitics"] == 0.0
        assert FEE_RATES_BY_CATEGORY["sports"] == 0.05


class TestPartialFillRisk:
    def setup_method(self) -> None:
        self.est = CostEstimator()

    def test_no_risk_when_size_within_depth(self) -> None:
        assert self.est.partial_fill_risk(100, 500) == 0.0

    def test_risk_when_size_exceeds_depth(self) -> None:
        risk = self.est.partial_fill_risk(600, 500)
        assert risk == approx(0.2 * (600 / 500 - 1.0))

    def test_risk_capped_at_ten_percent(self) -> None:
        risk = self.est.partial_fill_risk(10_000, 100)
        assert risk == approx(0.10)

    def test_risk_zero_depth(self) -> None:
        assert self.est.partial_fill_risk(100, 0) == 0.0


class TestUncertaintyPenalty:
    def setup_method(self) -> None:
        self.est = CostEstimator()

    def test_high_confidence_low_penalty(self) -> None:
        assert self.est.uncertainty_penalty(0.95) == approx(0.01 * 0.05)

    def test_low_confidence_high_penalty(self) -> None:
        assert self.est.uncertainty_penalty(0.50) == approx(0.01 * 0.50)

    def test_perfect_confidence_no_penalty(self) -> None:
        assert self.est.uncertainty_penalty(1.0) == approx(0.0)

    def test_zero_confidence_max_penalty(self) -> None:
        assert self.est.uncertainty_penalty(0.0) == approx(0.01)

    def test_custom_base_penalty(self) -> None:
        assert self.est.uncertainty_penalty(0.80, 0.02) == approx(0.02 * 0.20)


# ── TotalCost ────────────────────────────────────────────────────────────


class TestTotalCost:
    def setup_method(self) -> None:
        self.est = CostEstimator()

    def test_total_cost_nominal(self) -> None:
        total = self.est.total_cost(
            spread=0.02, price=0.50, size=100, depth=500,
            confidence=0.85,
        )
        expected = (
            0.01          # spread
            + 0.0         # slippage (size <= depth)
            + 0.05 * 0.50 * 0.50  # fee
            + 0.0         # partial fill (size <= depth)
            + 0.01 * 0.15  # uncertainty
        )
        assert total == approx(expected)

    def test_total_cost_large_order(self) -> None:
        total = self.est.total_cost(
            spread=0.02, price=0.50, size=2000, depth=500,
            confidence=0.85,
        )
        expected = (
            0.01          # spread
            + min((2000/500 - 1) * 0.02 / 2.0, 0.03)  # slippage
            + 0.05 * 0.50 * 0.50            # fee
            + min(0.2 * (2000/500 - 1), 0.10)  # partial fill
            + 0.01 * 0.15                   # uncertainty
        )
        assert total == approx(expected)

    def test_total_cost_zero_fee(self) -> None:
        total_no_fee = self.est.total_cost(
            spread=0.02, price=0.50, size=100, depth=500,
            confidence=0.85, fee_rate=0.0,
        )
        total_with_fee = self.est.total_cost(
            spread=0.02, price=0.50, size=100, depth=500,
            confidence=0.85, fee_rate=0.05,
        )
        assert total_no_fee < total_with_fee


class TestFeeRatesByCategory:
    def test_all_categories_defined(self) -> None:
        expected = {"crypto", "sports", "finance", "politics",
                    "economics", "culture", "weather", "other",
                    "mentions", "tech", "geopolitics"}
        assert set(FEE_RATES_BY_CATEGORY) == expected

    def test_geopolitics_is_free(self) -> None:
        assert FEE_RATES_BY_CATEGORY["geopolitics"] == 0.0

    def test_crypto_highest(self) -> None:
        assert FEE_RATES_BY_CATEGORY["crypto"] == max(
            v for k, v in FEE_RATES_BY_CATEGORY.items() if k != "geopolitics"
        )


# ── ExpectedValueEngine ──────────────────────────────────────────────────


class TestGrossEdge:
    def setup_method(self) -> None:
        self.engine = ExpectedValueEngine()

    def test_positive_edge(self) -> None:
        assert self.engine.gross_edge(0.60, 0.40) == approx(0.20)

    def test_negative_edge(self) -> None:
        assert self.engine.gross_edge(0.30, 0.70) == approx(-0.40)

    def test_zero_edge(self) -> None:
        assert self.engine.gross_edge(0.50, 0.50) == 0.0

    def test_extreme_values(self) -> None:
        assert self.engine.gross_edge(0.99, 0.01) == approx(0.98)
        assert self.engine.gross_edge(0.01, 0.99) == approx(-0.98)


class TestEvaluate:
    def setup_method(self) -> None:
        self.engine = ExpectedValueEngine()

    def test_returns_evresult(self) -> None:
        result = self.engine.evaluate(
            model_probability=0.60,
            implied_probability=0.50,
            spread=0.02,
            price=0.50,
            size=100,
            depth=500,
            confidence=0.85,
        )
        assert isinstance(result, EVResult)

    def test_evresult_has_all_fields(self) -> None:
        result = self.engine.evaluate(
            model_probability=0.60,
            implied_probability=0.50,
            spread=0.02,
            price=0.50,
            size=100,
            depth=500,
            confidence=0.85,
        )
        assert hasattr(result, "gross_edge")
        assert hasattr(result, "spread_penalty")
        assert hasattr(result, "slippage_penalty")
        assert hasattr(result, "fee_penalty")
        assert hasattr(result, "partial_fill_penalty")
        assert hasattr(result, "uncertainty_penalty")
        assert hasattr(result, "total_cost")
        assert hasattr(result, "net_edge")
        assert hasattr(result, "confidence")
        assert hasattr(result, "tradeable")
        assert hasattr(result, "reason")

    def test_gross_edge_correct(self) -> None:
        result = self.engine.evaluate(
            model_probability=0.60,
            implied_probability=0.50,
            spread=0.02, price=0.50, size=100, depth=500,
            confidence=0.85,
        )
        assert result.gross_edge == approx(0.10)

    def test_cost_breakdown_totals(self) -> None:
        result = self.engine.evaluate(
            model_probability=0.60,
            implied_probability=0.50,
            spread=0.02, price=0.50, size=100, depth=500,
            confidence=0.85,
        )
        assert result.total_cost == approx(
            result.spread_penalty
            + result.slippage_penalty
            + result.fee_penalty
            + result.partial_fill_penalty
            + result.uncertainty_penalty
        )

    def test_net_edge_is_gross_minus_cost(self) -> None:
        result = self.engine.evaluate(
            model_probability=0.60,
            implied_probability=0.50,
            spread=0.02, price=0.50, size=100, depth=500,
            confidence=0.85,
        )
        assert result.net_edge == approx(
            result.gross_edge - result.total_cost
        )

    def test_cost_breakdown_property(self) -> None:
        result = self.engine.evaluate(
            model_probability=0.60,
            implied_probability=0.50,
            spread=0.02, price=0.50, size=100, depth=500,
            confidence=0.85,
        )
        bd = result.cost_breakdown
        assert bd["total_cost"] == result.total_cost
        assert bd["spread_penalty"] == result.spread_penalty

    def test_strong_edge_is_tradeable(self) -> None:
        result = self.engine.evaluate(
            model_probability=0.65,
            implied_probability=0.50,
            spread=0.01, price=0.50, size=100, depth=1000,
            confidence=0.95, fee_rate=0.04,
        )
        assert result.tradeable is True
        assert result.reason == ""

    def test_weak_edge_is_not_tradeable(self) -> None:
        result = self.engine.evaluate(
            model_probability=0.51,
            implied_probability=0.50,
            spread=0.05, price=0.50, size=500, depth=200,
            confidence=0.60,
        )
        assert result.tradeable is False
        assert "threshold" in result.reason

    def test_tradeable_depends_on_fee_rate(self) -> None:
        args = dict(
            model_probability=0.58,
            implied_probability=0.50,
            spread=0.01, price=0.50, size=100, depth=500,
            confidence=0.90,
        )
        r_low = self.engine.evaluate(**args, fee_rate=0.0)
        r_high = self.engine.evaluate(**args, fee_rate=0.07)
        assert r_low.tradeable is True
        if r_high.tradeable:
            assert r_low.net_edge > r_high.net_edge
        else:
            assert r_low.net_edge > r_high.net_edge

    def test_zero_edge_not_tradeable_due_to_costs(self) -> None:
        result = self.engine.evaluate(
            model_probability=0.50,
            implied_probability=0.50,
            spread=0.02, price=0.50, size=100, depth=500,
            confidence=0.80,
        )
        assert result.net_edge < 0
        assert result.tradeable is False

    def test_custom_min_net_edge(self) -> None:
        engine = ExpectedValueEngine(min_net_edge=0.01)
        result = engine.evaluate(
            model_probability=0.54,
            implied_probability=0.50,
            spread=0.01, price=0.50, size=100, depth=1000,
            confidence=0.90, fee_rate=0.04,
        )
        assert result.tradeable is True

    def test_large_order_not_tradeable(self) -> None:
        result = self.engine.evaluate(
            model_probability=0.60,
            implied_probability=0.50,
            spread=0.02, price=0.50, size=10_000, depth=200,
            confidence=0.85,
        )
        assert result.tradeable is False
        assert result.slippage_penalty > 0
        assert result.partial_fill_penalty > 0

    def test_confidence_zero_incurs_max_penalty(self) -> None:
        result = self.engine.evaluate(
            model_probability=0.55,
            implied_probability=0.50,
            spread=0.02, price=0.50, size=100, depth=1000,
            confidence=0.0,
        )
        assert result.uncertainty_penalty == approx(0.01)
        assert result.net_edge < result.gross_edge

    def test_negative_gross_edge_not_tradeable(self) -> None:
        result = self.engine.evaluate(
            model_probability=0.40,
            implied_probability=0.50,
            spread=0.01, price=0.50, size=100, depth=1000,
            confidence=0.90,
        )
        assert result.net_edge < 0
        assert result.tradeable is False

    def test_geopolitics_fee_free(self) -> None:
        result = self.engine.evaluate(
            model_probability=0.55,
            implied_probability=0.50,
            spread=0.01, price=0.50, size=100, depth=1000,
            confidence=0.90, fee_rate=0.0,
        )
        assert result.fee_penalty == 0.0


class TestNetEdge:
    def setup_method(self) -> None:
        self.engine = ExpectedValueEngine()

    def test_net_edge_matches_evaluate(self) -> None:
        net = self.engine.net_edge(
            model_probability=0.60,
            implied_probability=0.50,
            spread=0.02, price=0.50, size=100, depth=500,
            confidence=0.85,
        )
        result = self.engine.evaluate(
            model_probability=0.60,
            implied_probability=0.50,
            spread=0.02, price=0.50, size=100, depth=500,
            confidence=0.85,
        )
        assert net == approx(result.net_edge)

    def test_net_edge_negative_when_costs_exceed_gross(self) -> None:
        net = self.engine.net_edge(
            model_probability=0.51,
            implied_probability=0.50,
            spread=0.05, price=0.50, size=1000, depth=200,
            confidence=0.50,
        )
        assert net < 0


class TestIsAcceptable:
    def setup_method(self) -> None:
        self.engine = ExpectedValueEngine()

    def test_above_threshold(self) -> None:
        assert self.engine.is_acceptable(0.10) is True

    def test_at_threshold(self) -> None:
        assert self.engine.is_acceptable(0.05) is True

    def test_below_threshold(self) -> None:
        assert self.engine.is_acceptable(0.04) is False

    def test_negative(self) -> None:
        assert self.engine.is_acceptable(-0.01) is False

    def test_custom_min_net_edge(self) -> None:
        engine = ExpectedValueEngine(min_net_edge=0.02)
        assert engine.is_acceptable(0.02) is True
        assert engine.is_acceptable(0.01) is False
