"""Tests for the portfolio tracker (app.portfolio.tracker)."""

from __future__ import annotations

from typing import Any

import pytest

from app.portfolio.tracker import PortfolioTracker


def approx(val: float, rel: float = 1e-6) -> Any:
    return pytest.approx(val, rel=rel, abs=1e-12)


class TestPortfolioTrackerInit:
    def test_default_equity(self) -> None:
        pt = PortfolioTracker()
        assert pt.equity == approx(10_000.0)
        assert pt.initial_equity == approx(10_000.0)

    def test_custom_equity(self) -> None:
        pt = PortfolioTracker(initial_equity=50_000.0)
        assert pt.equity == approx(50_000.0)

    def test_zero_equity(self) -> None:
        pt = PortfolioTracker(initial_equity=0.0)
        assert pt.equity == approx(0.0)


class TestPortfolioTrackerAddTrade:
    def test_opens_new_position(self) -> None:
        pt = PortfolioTracker()
        pt.add_trade(market_id="mkt_001", side="YES", size=100, price=0.55)

        pos = pt.position_for("mkt_001")
        assert pos is not None
        assert pos["side"] == "YES"
        assert pos["size"] == approx(100.0)
        assert pos["average_entry"] == approx(0.55)

    def test_adds_to_existing_position(self) -> None:
        pt = PortfolioTracker()
        pt.add_trade("mkt_001", "YES", 100, 0.50)
        pt.add_trade("mkt_001", "YES", 100, 0.60)

        pos = pt.position_for("mkt_001")
        assert pos is not None
        assert pos["size"] == approx(200.0)
        assert pos["average_entry"] == approx(0.55)  # (100*0.50 + 100*0.60) / 200

    def test_reduces_opposite_side(self) -> None:
        pt = PortfolioTracker()
        pt.add_trade("mkt_001", "YES", 100, 0.50)
        pt.add_trade("mkt_001", "NO", 40, 0.55)

        pos = pt.position_for("mkt_001")
        assert pos is not None
        assert pos["side"] == "YES"
        assert pos["size"] == approx(60.0)
        # Opposite-side close: effective YES exit = 1 - 0.55 = 0.45
        # Realised P&L = 40 * (0.45 - 0.50) = -2.0
        assert pos["realised_pnl"] == approx(-2.0)

    def test_closes_position_exactly(self) -> None:
        pt = PortfolioTracker()
        pt.add_trade("mkt_001", "YES", 100, 0.50)
        pt.add_trade("mkt_001", "NO", 100, 0.55)

        assert pt.position_for("mkt_001") is None
        assert pt.num_positions() == 0
        # 100 * ((1 - 0.55) - 0.50) = -5.0
        assert pt.total_realised_pnl() == approx(-5.0)

    def test_closes_with_excess(self) -> None:
        pt = PortfolioTracker()
        pt.add_trade("mkt_001", "YES", 100, 0.50)
        pt.add_trade("mkt_001", "NO", 150, 0.55)

        # 100 YES closed: effective exit = (1 - 0.55) = 0.45, P&L = -5.0
        # 50 NO position at 0.55
        pos = pt.position_for("mkt_001")
        assert pos is not None
        assert pos["side"] == "NO"
        assert pos["size"] == approx(50.0)
        assert pos["average_entry"] == approx(0.55)
        assert pos["realised_pnl"] == approx(-5.0)

    def test_fee_deducted_from_equity(self) -> None:
        pt = PortfolioTracker(initial_equity=10_000.0)
        pt.add_trade("mkt_001", "YES", 100, 0.50, fee=2.50)
        assert pt.equity == approx(9_997.50)

    def test_ignores_zero_size(self) -> None:
        pt = PortfolioTracker()
        pt.add_trade("mkt_001", "YES", 0, 0.50)
        assert pt.num_positions() == 0

    def test_ignores_zero_price(self) -> None:
        pt = PortfolioTracker()
        pt.add_trade("mkt_001", "YES", 100, 0.0)
        assert pt.num_positions() == 0


class TestPortfolioTrackerClose:
    def test_close_existing_position(self) -> None:
        pt = PortfolioTracker()
        pt.add_trade("mkt_001", "YES", 100, 0.50)
        pt.close_position("mkt_001", price=0.55)
        assert pt.position_for("mkt_001") is None
        # 100 * ((1 - 0.55) - 0.50) = -5.0
        assert pt.total_realised_pnl() == approx(-5.0)

    def test_close_nonexistent_position(self) -> None:
        pt = PortfolioTracker()
        pt.close_position("mkt_999", price=0.50)
        assert pt.num_positions() == 0


class TestPortfolioTrackerUpdatePrice:
    def test_updates_unrealised_pnl(self) -> None:
        pt = PortfolioTracker()
        pt.add_trade("mkt_001", "YES", 100, 0.50)
        pt.update_price("mkt_001", 0.60)
        pos = pt.position_for("mkt_001")
        assert pos is not None
        assert pos["current_price"] == approx(0.60)
        assert pos["unrealised_pnl"] == approx(10.0)

    def test_unrealised_pnl_no_position(self) -> None:
        pt = PortfolioTracker()
        pt.update_price("mkt_001", 0.60)
        assert pt.total_unrealised_pnl() == approx(0.0)


class TestPortfolioTrackerQuery:
    def test_total_exposure(self) -> None:
        pt = PortfolioTracker()
        pt.add_trade("mkt_001", "YES", 100, 0.50)
        pt.add_trade("mkt_002", "NO", 200, 0.40)
        assert pt.total_exposure() == approx(300.0)

    def test_market_exposure(self) -> None:
        pt = PortfolioTracker()
        pt.add_trade("mkt_001", "YES", 100, 0.50)
        assert pt.market_exposure("mkt_001") == approx(100.0)
        assert pt.market_exposure("mkt_999") == approx(0.0)

    def test_num_positions(self) -> None:
        pt = PortfolioTracker()
        assert pt.num_positions() == 0
        pt.add_trade("mkt_001", "YES", 100, 0.50)
        pt.add_trade("mkt_002", "NO", 200, 0.40)
        assert pt.num_positions() == 2

    def test_summary(self) -> None:
        pt = PortfolioTracker()
        pt.add_trade("mkt_001", "YES", 100, 0.50)
        s = pt.summary()
        assert s["equity"] == approx(10_000.0)
        assert s["total_exposure"] == approx(100.0)
        assert s["open_positions"] == 1
        assert "mkt_001" in s["positions"]


class TestPortfolioTrackerLegacyCompat:
    def test_update_position(self) -> None:
        pt = PortfolioTracker()
        pt.update_position({
            "market_id": "mkt_001",
            "side": "YES",
            "size": 50.0,
            "average_entry": 0.55,
            "current_price": 0.55,
            "realised_pnl": 0.0,
            "unrealised_pnl": 0.0,
        })
        assert pt.market_exposure("mkt_001") == approx(50.0)

    def test_unrealised_pnl_property(self) -> None:
        pt = PortfolioTracker()
        assert pt.unrealised_pnl() == approx(0.0)

    def test_realised_pnl_property(self) -> None:
        pt = PortfolioTracker()
        assert pt.realised_pnl() == approx(0.0)

    def test_equity_reflects_pnl(self) -> None:
        pt = PortfolioTracker()
        pt.add_trade("mkt_001", "YES", 100, 0.50)
        pt.close_position("mkt_001", price=0.55)
        # -5.0 realised P&L on close
        assert pt.equity == approx(9_995.0)
