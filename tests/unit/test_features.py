"""Unit tests for feature engineering modules."""

import pytest

from app.features.orderbook import OrderBookFeatures


class TestOrderBookFeatures:
    """Tests for order-book feature calculations."""

    def setup_method(self):
        self.features = OrderBookFeatures()

    def test_midpoint(self):
        assert self.features.midpoint(0.45, 0.55) == 0.50

    def test_spread(self):
        assert self.features.spread(0.45, 0.55) == pytest.approx(0.10)

    def test_relative_spread(self):
        result = self.features.relative_spread(0.45, 0.55)
        assert abs(result - 0.20) < 1e-6

    def test_obi_positive(self):
        obi = self.features.order_book_imbalance(10000, 8000)
        assert abs(obi - 0.1111) < 0.001

    def test_obi_negative(self):
        obi = self.features.order_book_imbalance(8000, 10000)
        assert abs(obi - (-0.1111)) < 0.001

    def test_obi_zero_depth(self):
        assert self.features.order_book_imbalance(0, 0) == 0.0
