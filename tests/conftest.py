"""Shared test fixtures and configuration."""

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio

from app.monitoring.health import health_status
from app.storage.db import Database, db
from app.storage.models import Market, MarketSnapshot, Order, Position, RiskEvent, Signal


@pytest_asyncio.fixture
async def test_db() -> AsyncGenerator[Database, None]:
    """Create a temporary in-memory SQLite database for testing.

    Swaps the global ``db._conn`` so that singleton-based code paths
    are also exercised.
    """
    test_db_instance = Database(db_path=":memory:")
    await test_db_instance.connect()
    await test_db_instance.init_schema()
    original_conn = db._conn
    db._conn = test_db_instance._conn
    yield test_db_instance
    await test_db_instance.close()
    db._conn = original_conn


@pytest_asyncio.fixture
async def isolated_db() -> AsyncGenerator[Database, None]:
    """Create a fresh in-memory database *without* touching the global.

    Use this for tests that should be fully isolated from the global
    ``db`` singleton.
    """
    d = Database(db_path=":memory:")
    await d.connect()
    await d.init_schema()
    yield d
    await d.close()


# ── Factored helpers ────────────────────────────────────────────────


def make_market(
    market_id: str = "mkt_001",
    question: str = "Will event X happen?",
    **overrides: Any,
) -> Market:
    """Build a Market instance with sensible defaults."""
    params: dict[str, Any] = dict(
        market_id=market_id,
        question=question,
        condition_id=f"cond_{market_id}",
        status="active",
        resolution_time="2026-12-31T23:59:59Z",
        liquidity=50000.0,
    )
    params.update(overrides)
    return Market(**params)


def make_snapshot(
    market_id: str = "mkt_001",
    bid: float = 0.45,
    ask: float = 0.55,
    **overrides: Any,
) -> MarketSnapshot:
    """Build a MarketSnapshot with sensible defaults."""
    params: dict[str, Any] = dict(
        market_id=market_id,
        bid=bid,
        ask=ask,
        midpoint=(bid + ask) / 2,
        spread=ask - bid,
        bid_depth=10000.0,
        ask_depth=8000.0,
        volume=50000.0,
        time_to_resolution=86400.0,
    )
    params.update(overrides)
    return MarketSnapshot(**params)


def make_signal(
    signal_id: str = "sig_001",
    market_id: str = "mkt_001",
    **overrides: Any,
) -> Signal:
    """Build a Signal with sensible defaults."""
    params: dict[str, Any] = dict(
        signal_id=signal_id,
        market_id=market_id,
        strategy="microstructure",
        side="YES",
        model_probability=0.55,
        implied_probability=0.50,
        gross_edge=0.05,
        estimated_cost=0.01,
        net_edge=0.04,
        confidence=0.80,
        decision="PAPER_BUY",
    )
    params.update(overrides)
    return Signal(**params)


def make_order(
    order_id: str = "ord_001",
    market_id: str = "mkt_001",
    **overrides: Any,
) -> Order:
    """Build an Order with sensible defaults."""
    params: dict[str, Any] = dict(
        order_id=order_id,
        market_id=market_id,
        side="YES",
        requested_price=0.50,
        requested_size=10.0,
        status="CREATED",
    )
    params.update(overrides)
    return Order(**params)


def make_position(
    position_id: str = "pos_001",
    market_id: str = "mkt_001",
    **overrides: Any,
) -> Position:
    """Build a Position with sensible defaults."""
    params: dict[str, Any] = dict(
        position_id=position_id,
        market_id=market_id,
        side="YES",
        size=10.0,
        average_entry=0.50,
        current_price=0.55,
        realised_pnl=0.0,
        unrealised_pnl=0.50,
    )
    params.update(overrides)
    return Position(**params)


def make_risk_event(
    event_id: str = "evt_001",
    **overrides: Any,
) -> RiskEvent:
    """Build a RiskEvent with sensible defaults."""
    params: dict[str, Any] = dict(
        event_id=event_id,
        event_type="DAILY_LOSS",
        severity="HIGH",
        details="Daily loss limit exceeded",
    )
    params.update(overrides)
    return RiskEvent(**params)


@pytest.fixture
def sample_snapshot() -> dict[str, Any]:
    """Return a plain-dict snapshot (legacy compat)."""
    return {
        "market_id": "test_market_001",
        "bid": 0.45,
        "ask": 0.55,
        "midpoint": 0.50,
        "spread": 0.10,
        "bid_depth": 10000.0,
        "ask_depth": 8000.0,
        "volume": 50000.0,
        "time_to_resolution": 86400.0,
    }


@pytest.fixture
def healthy_health_status():
    """Set all health checks to healthy before a test."""
    health_status.checks.clear()
    health_status.set_healthy("database")
    health_status.set_healthy("data_freshness")
    health_status.set_healthy("api")
    health_status.set_healthy("model_availability")
    yield
    health_status.checks.clear()
