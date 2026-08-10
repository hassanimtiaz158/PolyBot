"""Unit tests for PositionSizer.

Covers fixed_risk_size, determine_size, constraint capping,
fail-closed behaviour, no-Martingale invariant, and custom pct.
"""

from typing import Any

import pytest

from app.risk.position_sizing import PositionSizer


def approx(val: float, rel: float = 1e-6) -> Any:
    return pytest.approx(val, rel=rel, abs=1e-12)


DEFAULT_PCT = 0.01


# ═══════════════════════════════════════════════════════════════════════════
# fixed_risk_size
# ═══════════════════════════════════════════════════════════════════════════


class TestFixedRiskSize:
    def setup_method(self) -> None:
        self.sizer = PositionSizer()

    def test_full_confidence(self) -> None:
        assert self.sizer.fixed_risk_size(10_000, 1.0) == approx(100.0)

    def test_half_confidence(self) -> None:
        assert self.sizer.fixed_risk_size(10_000, 0.5) == approx(50.0)

    def test_low_confidence(self) -> None:
        assert self.sizer.fixed_risk_size(10_000, 0.1) == approx(10.0)

    def test_small_equity(self) -> None:
        assert self.sizer.fixed_risk_size(500, 1.0) == approx(5.0)

    def test_large_equity(self) -> None:
        assert self.sizer.fixed_risk_size(1_000_000, 0.8) == approx(8_000.0)

    def test_zero_equity_returns_zero(self) -> None:
        assert self.sizer.fixed_risk_size(0, 0.8) == 0.0

    def test_negative_equity_returns_zero(self) -> None:
        assert self.sizer.fixed_risk_size(-1_000, 0.8) == 0.0

    def test_zero_confidence_returns_zero(self) -> None:
        assert self.sizer.fixed_risk_size(10_000, 0.0) == 0.0

    def test_negative_confidence_returns_zero(self) -> None:
        assert self.sizer.fixed_risk_size(10_000, -0.1) == 0.0

    def test_both_zero_returns_zero(self) -> None:
        assert self.sizer.fixed_risk_size(0, 0) == 0.0

    def test_both_negative_returns_zero(self) -> None:
        assert self.sizer.fixed_risk_size(-100, -0.5) == 0.0

    def test_computed_value(self) -> None:
        equity, confidence = 25_000, 0.75
        expected = equity * DEFAULT_PCT * confidence
        assert self.sizer.fixed_risk_size(equity, confidence) == approx(expected)


# ═══════════════════════════════════════════════════════════════════════════
# determine_size — fail-closed
# ═══════════════════════════════════════════════════════════════════════════


class TestDetermineSizeFailClosed:
    def setup_method(self) -> None:
        self.sizer = PositionSizer()

    def test_zero_net_edge_returns_zero(self) -> None:
        assert self.sizer.determine_size(
            equity=10_000, confidence=1.0, net_edge=0.0,
        ) == 0.0

    def test_negative_net_edge_returns_zero(self) -> None:
        assert self.sizer.determine_size(
            equity=10_000, confidence=1.0, net_edge=-0.05,
        ) == 0.0

    def test_zero_confidence_returns_zero(self) -> None:
        assert self.sizer.determine_size(
            equity=10_000, confidence=0.0, net_edge=0.05,
        ) == 0.0

    def test_negative_confidence_returns_zero(self) -> None:
        assert self.sizer.determine_size(
            equity=10_000, confidence=-0.1, net_edge=0.05,
        ) == 0.0

    def test_both_zero_returns_zero(self) -> None:
        assert self.sizer.determine_size(
            equity=10_000, confidence=0.0, net_edge=0.0,
        ) == 0.0

    def test_zero_equity_returns_zero(self) -> None:
        assert self.sizer.determine_size(
            equity=0, confidence=1.0, net_edge=0.05,
        ) == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# determine_size — constraint capping
# ═══════════════════════════════════════════════════════════════════════════


class TestDetermineSizeConstraints:
    def setup_method(self) -> None:
        self.sizer = PositionSizer()

    def test_nominal_unconstrained(self) -> None:
        size = self.sizer.determine_size(
            equity=10_000, confidence=1.0, net_edge=0.05,
        )
        assert size == approx(100.0)

    def test_liquidity_caps_below_fixed_risk(self) -> None:
        size = self.sizer.determine_size(
            equity=10_000, confidence=1.0, net_edge=0.05, liquidity=50,
        )
        assert size == approx(50.0)

    def test_risk_limit_caps_below_fixed_risk(self) -> None:
        size = self.sizer.determine_size(
            equity=10_000, confidence=1.0, net_edge=0.05, risk_limit=30,
        )
        assert size == approx(30.0)

    def test_liquidity_and_risk_limit_both_cap(self) -> None:
        size = self.sizer.determine_size(
            equity=10_000, confidence=1.0, net_edge=0.05,
            liquidity=50, risk_limit=40,
        )
        assert size == approx(40.0)

    def test_returns_minimum_of_all_constraints(self) -> None:
        size = self.sizer.determine_size(
            equity=10_000, confidence=0.8, net_edge=0.05,
            liquidity=60, risk_limit=90,
        )
        fixed = 10_000 * DEFAULT_PCT * 0.8
        assert size == approx(min(fixed, 60, 90))

    def test_very_high_confidence_still_capped(self) -> None:
        size = self.sizer.determine_size(
            equity=10_000, confidence=1.0, net_edge=1.0,
        )
        assert size == approx(10_000 * DEFAULT_PCT)

    def test_default_liquidity_is_infinite(self) -> None:
        size = self.sizer.determine_size(
            equity=10_000, confidence=1.0, net_edge=0.05,
        )
        assert size == approx(100.0)

    def test_zero_liquidity_returns_zero(self) -> None:
        assert self.sizer.determine_size(
            equity=10_000, confidence=1.0, net_edge=0.05, liquidity=0,
        ) == 0.0

    def test_zero_risk_limit_returns_zero(self) -> None:
        assert self.sizer.determine_size(
            equity=10_000, confidence=1.0, net_edge=0.05, risk_limit=0,
        ) == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# No Martingale invariant
# ═══════════════════════════════════════════════════════════════════════════


class TestNoMartingale:
    """Position size never increases after a loss — equity shrinks,
    confidence can only stay same or drop, so size must not grow."""

    def setup_method(self) -> None:
        self.sizer = PositionSizer()

    def test_size_never_increases_after_equity_loss(self) -> None:
        size_before = self.sizer.determine_size(
            equity=10_000, confidence=0.8, net_edge=0.05,
        )
        size_after = self.sizer.determine_size(
            equity=9_000, confidence=0.7, net_edge=0.04,
        )
        assert size_after <= size_before

    def test_same_equity_same_confidence_same_size(self) -> None:
        s1 = self.sizer.determine_size(
            equity=10_000, confidence=0.8, net_edge=0.05,
        )
        s2 = self.sizer.determine_size(
            equity=10_000, confidence=0.8, net_edge=0.05,
        )
        assert s1 == s2

    def test_lower_equity_lower_confidence_smaller_or_equal(self) -> None:
        sizes = []
        for eq, conf, edge in [
            (10_000, 1.0, 0.10),
            (9_500, 0.9, 0.08),
            (9_000, 0.8, 0.06),
            (8_000, 0.6, 0.04),
        ]:
            sizes.append(
                self.sizer.determine_size(equity=eq, confidence=conf, net_edge=edge)
            )
        for i in range(1, len(sizes)):
            assert sizes[i] <= sizes[i - 1]

    def test_no_kelly_aggressive_sizing(self) -> None:
        """With only fixed-risk, size never exceeds equity * max_position_pct."""
        size = self.sizer.determine_size(
            equity=10_000, confidence=1.0, net_edge=0.50,
        )
        assert size <= 10_000 * DEFAULT_PCT

    def test_high_edge_does_not_increase_size_beyond_pct(self) -> None:
        size = self.sizer.determine_size(
            equity=10_000, confidence=1.0, net_edge=1.0,
        )
        assert size == approx(10_000 * DEFAULT_PCT)


# ═══════════════════════════════════════════════════════════════════════════
# Custom max_position_pct
# ═══════════════════════════════════════════════════════════════════════════


class TestCustomMaxPositionPct:
    def test_custom_pct_in_fixed_risk(self) -> None:
        sizer = PositionSizer(max_position_pct=0.02)
        assert sizer.fixed_risk_size(10_000, 1.0) == approx(200.0)

    def test_custom_pct_in_determine_size(self) -> None:
        sizer = PositionSizer(max_position_pct=0.05)
        size = sizer.determine_size(
            equity=10_000, confidence=1.0, net_edge=0.10,
        )
        assert size == approx(500.0)

    def test_custom_pct_respects_liquidity_cap(self) -> None:
        sizer = PositionSizer(max_position_pct=0.05)
        size = sizer.determine_size(
            equity=10_000, confidence=1.0, net_edge=0.10, liquidity=100,
        )
        assert size == approx(100.0)

    def test_custom_pct_respects_risk_limit(self) -> None:
        sizer = PositionSizer(max_position_pct=0.05)
        size = sizer.determine_size(
            equity=10_000, confidence=1.0, net_edge=0.10, risk_limit=150,
        )
        assert size == approx(150.0)

    def test_very_small_pct(self) -> None:
        sizer = PositionSizer(max_position_pct=0.001)
        assert sizer.fixed_risk_size(10_000, 1.0) == approx(10.0)

    def test_large_pct(self) -> None:
        sizer = PositionSizer(max_position_pct=0.50)
        assert sizer.fixed_risk_size(10_000, 1.0) == approx(5_000.0)

    def test_zero_pct_returns_zero(self) -> None:
        sizer = PositionSizer(max_position_pct=0.0)
        assert sizer.fixed_risk_size(10_000, 1.0) == 0.0

    def test_custom_pct_independent_across_instances(self) -> None:
        s1 = PositionSizer(max_position_pct=0.01)
        s2 = PositionSizer(max_position_pct=0.03)
        assert s1.fixed_risk_size(10_000, 1.0) == approx(100.0)
        assert s2.fixed_risk_size(10_000, 1.0) == approx(300.0)

    def test_combined_custom_pct_with_all_constraints(self) -> None:
        sizer = PositionSizer(max_position_pct=0.03)
        size = sizer.determine_size(
            equity=10_000, confidence=0.5, net_edge=0.10,
            liquidity=200, risk_limit=100,
        )
        fixed = 10_000 * 0.03 * 0.5
        assert size == approx(min(fixed, 200, 100))
