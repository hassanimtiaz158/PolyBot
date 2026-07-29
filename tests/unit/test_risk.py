"""Unit tests for risk limits and position sizing."""

from app.risk.limits import RiskLimits
from app.risk.position_sizing import PositionSizer


class TestRiskLimits:
    def setup_method(self):
        self.limits = RiskLimits()

    def test_position_size_within_limit(self):
        assert self.limits.check_position_size(50, 10000)

    def test_position_size_exceeds_limit(self):
        assert not self.limits.check_position_size(500, 10000)

    def test_daily_loss_within_limit(self):
        assert self.limits.check_daily_loss(-50, 10000)

    def test_daily_loss_exceeds_limit(self):
        assert not self.limits.check_daily_loss(-500, 10000)

    def test_consecutive_losses_within_limit(self):
        assert self.limits.check_consecutive_losses(3)

    def test_consecutive_losses_exceeds_limit(self):
        assert not self.limits.check_consecutive_losses(5)

    def test_open_positions_within_limit(self):
        assert self.limits.check_open_positions(5)

    def test_open_positions_exceeds_limit(self):
        assert not self.limits.check_open_positions(10)


class TestPositionSizer:
    def setup_method(self):
        self.sizer = PositionSizer()

    def test_fixed_risk_size(self):
        size = self.sizer.fixed_risk_size(10000, 1.0)
        assert abs(size - 100.0) < 1e-6

    def test_determine_size_capped_by_liquidity(self):
        size = self.sizer.determine_size(10000, 1.0, 0.10, 50, 200)
        assert size == 50
