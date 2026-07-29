"""Unit tests for expected value and cost estimation."""

from app.ev.costs import CostEstimator
from app.ev.expected_value import ExpectedValueEngine


class TestCostEstimator:
    def setup_method(self):
        self.estimator = CostEstimator()

    def test_spread_cost(self):
        cost = self.estimator.spread_cost(0.10, 0.50)
        assert abs(cost - 0.20) < 1e-6

    def test_spread_cost_zero_price(self):
        assert self.estimator.spread_cost(0.10, 0.0) == 0.0

    def test_slippage_cost(self):
        cost = self.estimator.slippage_cost(100, 10000)
        assert cost <= 0.05

    def test_uncertainty_penalty(self):
        penalty = self.estimator.uncertainty_penalty(0.80)
        expected = 0.01 * 0.20
        assert abs(penalty - expected) < 1e-6


class TestExpectedValueEngine:
    def setup_method(self):
        self.engine = ExpectedValueEngine()

    def test_gross_edge_positive(self):
        edge = self.engine.gross_edge(0.60, 0.40)
        assert abs(edge - 0.20) < 1e-6

    def test_gross_edge_negative(self):
        edge = self.engine.gross_edge(0.30, 0.70)
        assert abs(edge - (-0.40)) < 1e-6
