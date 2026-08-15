"""Integration tests for the read-only FastAPI backend.

Covers every endpoint, pagination, filters, validation errors, error
handling, and the guarantee that no secrets are exposed.
"""

import json
from collections.abc import AsyncGenerator

import httpx
import pytest
import pytest_asyncio

from app.api.app import create_app
from app.config.settings import settings
from app.storage.db import Database
from app.storage.models import Market, Order, Position, RiskEvent, Signal
from app.storage.repositories import (
    MarketRepository,
    OrderRepository,
    PositionRepository,
    RiskEventRepository,
    SignalRepository,
)

BASE_URL = "http://testserver"


async def seed_data(db: Database) -> None:
    """Populate a test database with markets, signals, orders, positions,
    and risk events."""
    market_repo = MarketRepository(db)
    for market_id, question, status in [
        ("mkt_001", "Will event A happen?", "active"),
        ("mkt_002", "Will event B happen?", "active"),
        ("mkt_003", "Will event C happen?", "closed"),
    ]:
        await market_repo.upsert(
            Market(
                market_id=market_id,
                question=question,
                status=status,
                liquidity=50000.0,
            )
        )

    signal_repo = SignalRepository(db)
    for signal_id, market_id, strategy, decision, ts in [
        ("sig_001", "mkt_001", "microstructure", "CANDIDATE", "2026-08-01T10:00:00Z"),
        ("sig_002", "mkt_001", "microstructure", "NO_SIGNAL", "2026-08-01T09:00:00Z"),
        ("sig_003", "mkt_002", "probability", "CANDIDATE", "2026-08-01T08:00:00Z"),
    ]:
        await signal_repo.insert(
            Signal(
                signal_id=signal_id,
                market_id=market_id,
                strategy=strategy,
                side="YES",
                decision=decision,
                model_probability=0.55,
                implied_probability=0.50,
                gross_edge=0.05,
                net_edge=0.04,
                confidence=0.8,
                rejection_reason=(
                    "NET_EDGE_TOO_LOW" if decision == "NO_SIGNAL" else None
                ),
                timestamp=ts,
            )
        )

    order_repo = OrderRepository(db)
    for order_id, market_id, status, ts in [
        ("ord_001", "mkt_001", "CREATED", "2026-08-01T10:00:00Z"),
        ("ord_002", "mkt_001", "FILLED", "2026-08-01T09:00:00Z"),
        ("ord_003", "mkt_002", "CANCELLED", "2026-08-01T08:00:00Z"),
        ("ord_004", "mkt_002", "PARTIALLY_FILLED", "2026-08-01T07:00:00Z"),
    ]:
        await order_repo.insert(
            Order(
                order_id=order_id,
                market_id=market_id,
                side="YES",
                requested_price=0.50,
                requested_size=10.0,
                status=status,
                filled_size=10.0 if status in ("FILLED", "PARTIALLY_FILLED") else None,
                submitted_at=ts,
            )
        )

    position_repo = PositionRepository(db)
    for position_id, market_id, side, size, realised, unrealised in [
        ("pos_001", "mkt_001", "YES", 10.0, 1.5, 0.5),
        ("pos_002", "mkt_002", "NO", 5.0, 2.5, 0.25),
        ("pos_003", "mkt_003", "YES", 0.0, 0.0, 0.0),
    ]:
        await position_repo.upsert(
            Position(
                position_id=position_id,
                market_id=market_id,
                side=side,
                size=size,
                average_entry=0.50,
                current_price=0.55,
                realised_pnl=realised,
                unrealised_pnl=unrealised,
            )
        )

    event_repo = RiskEventRepository(db)
    for event_id, event_type, severity, ts in [
        ("evt_001", "DAILY_LOSS", "HIGH", "2026-08-01T10:00:00Z"),
        ("evt_002", "API_FAILURE", "LOW", "2026-08-01T09:00:00Z"),
    ]:
        await event_repo.insert(
            RiskEvent(
                event_id=event_id,
                event_type=event_type,
                severity=severity,
                details=f"details for {event_type}",
                timestamp=ts,
            )
        )


@pytest_asyncio.fixture
async def api_db() -> AsyncGenerator[Database, None]:
    """In-memory database with seeded data for API tests."""
    db = Database(db_path=":memory:")
    await db.connect()
    await db.init_schema()
    await seed_data(db)
    yield db
    await db.close()


@pytest_asyncio.fixture
async def client(api_db: Database) -> AsyncGenerator[httpx.AsyncClient, None]:
    """Async test client bound to the seeded database."""
    app = create_app(database=api_db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url=BASE_URL
    ) as test_client:
        yield test_client


async def make_client(db: Database) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_app(database=db))
    return httpx.AsyncClient(transport=transport, base_url=BASE_URL)


# ====================================================================
# /health
# ====================================================================


class TestHealth:
    @pytest.mark.asyncio
    async def test_reports_expected_checks(self, client: httpx.AsyncClient):
        resp = await client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body["checks"]) == {
            "database",
            "data_freshness",
            "api",
            "model_availability",
        }
        assert body["checks"]["database"]["healthy"] is True
        assert body["checks"]["data_freshness"]["healthy"] is False
        assert body["checks"]["model_availability"]["healthy"] is False
        assert body["healthy"] is False
        assert body["timestamp"]

    @pytest.mark.asyncio
    async def test_does_not_expose_secrets(
        self, client: httpx.AsyncClient, monkeypatch
    ):
        monkeypatch.setattr(settings, "poly_api_key", "SUPER-SECRET-KEY")
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert "SUPER-SECRET-KEY" not in resp.text


# ====================================================================
# /system/status
# ====================================================================


class TestSystemStatus:
    @pytest.mark.asyncio
    async def test_reports_mode_and_database(self, client: httpx.AsyncClient):
        resp = await client.get("/system/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["mode"] in ("RESEARCH", "PAPER", "HALTED")
        assert isinstance(body["trading_enabled"], bool)
        assert isinstance(body["live_enabled"], bool)
        assert body["database_connected"] is True
        assert body["schema_version"] == 3
        assert body["version"]
        assert body["timestamp"]

    @pytest.mark.asyncio
    async def test_reads_persisted_circuit_breaker_state(
        self, api_db: Database, client: httpx.AsyncClient
    ):
        payload = json.dumps({
            "state": "HALTED",
            "reasons": ["DAILY_LOSS"],
            "triggered_at": "2026-08-01T10:00:00Z",
        })
        await api_db.conn.execute(
            "INSERT OR REPLACE INTO circuit_breaker_state "
            "(key, value, updated_at) VALUES ('circuit_breaker', ?, ?)",
            (payload, "2026-08-01T10:00:00Z"),
        )
        await api_db.conn.commit()
        resp = await client.get("/system/status")
        body = resp.json()
        assert body["circuit_breaker"]["state"] == "HALTED"
        assert body["circuit_breaker"]["reasons"] == ["DAILY_LOSS"]

    @pytest.mark.asyncio
    async def test_no_breaker_state_when_nothing_persisted(
        self, client: httpx.AsyncClient
    ):
        body = (await client.get("/system/status")).json()
        assert body["circuit_breaker"] is None

    @pytest.mark.asyncio
    async def test_does_not_expose_secrets(
        self, client: httpx.AsyncClient, monkeypatch
    ):
        monkeypatch.setattr(settings, "poly_api_key", "SUPER-SECRET-KEY")
        monkeypatch.setattr(settings, "poly_secret", "SUPER-SECRET-SECRET")
        resp = await client.get("/system/status")
        assert resp.status_code == 200
        assert "SUPER-SECRET-KEY" not in resp.text
        assert "SUPER-SECRET-SECRET" not in resp.text


# ====================================================================
# /markets
# ====================================================================


class TestMarkets:
    @pytest.mark.asyncio
    async def test_lists_all_markets(self, client: httpx.AsyncClient):
        resp = await client.get("/markets")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["items"]) == 3
        assert body["pagination"]["total"] == 3
        assert body["pagination"]["has_more"] is False
        questions = {item["question"] for item in body["items"]}
        assert questions == {
            "Will event A happen?",
            "Will event B happen?",
            "Will event C happen?",
        }

    @pytest.mark.asyncio
    async def test_pagination(self, client: httpx.AsyncClient):
        resp = await client.get("/markets", params={"limit": 2})
        body = resp.json()
        assert len(body["items"]) == 2
        assert body["pagination"]["total"] == 3
        assert body["pagination"]["has_more"] is True

        resp = await client.get("/markets", params={"limit": 2, "offset": 2})
        body = resp.json()
        assert len(body["items"]) == 1
        assert body["pagination"]["has_more"] is False

    @pytest.mark.asyncio
    async def test_filters_by_status(self, client: httpx.AsyncClient):
        resp = await client.get("/markets", params={"status": "active"})
        assert resp.json()["pagination"]["total"] == 2
        resp = await client.get("/markets", params={"status": "closed"})
        assert resp.json()["pagination"]["total"] == 1

    @pytest.mark.asyncio
    async def test_validation_errors(self, client: httpx.AsyncClient):
        for params in ({"limit": 0}, {"limit": 101}, {"offset": -1}):
            resp = await client.get("/markets", params=params)
            assert resp.status_code == 422
            assert resp.json()["detail"] == "validation_error"


# ====================================================================
# /signals
# ====================================================================


class TestSignals:
    @pytest.mark.asyncio
    async def test_lists_all_signals(self, client: httpx.AsyncClient):
        resp = await client.get("/signals")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["items"]) == 3
        assert body["items"][0]["strategy"] == "microstructure"

    @pytest.mark.asyncio
    async def test_filters(self, client: httpx.AsyncClient):
        by_market = await client.get("/signals", params={"market_id": "mkt_001"})
        assert by_market.json()["pagination"]["total"] == 2

        by_strategy = await client.get(
            "/signals", params={"strategy": "probability"}
        )
        assert by_strategy.json()["pagination"]["total"] == 1

        by_decision = await client.get("/signals", params={"decision": "NO_SIGNAL"})
        body = by_decision.json()
        assert body["pagination"]["total"] == 1
        assert body["items"][0]["rejection_reason"] == "NET_EDGE_TOO_LOW"

    @pytest.mark.asyncio
    async def test_pagination(self, client: httpx.AsyncClient):
        resp = await client.get("/signals", params={"limit": 2})
        body = resp.json()
        assert len(body["items"]) == 2
        assert body["pagination"]["total"] == 3
        assert body["pagination"]["has_more"] is True

    @pytest.mark.asyncio
    async def test_validation_errors(self, client: httpx.AsyncClient):
        resp = await client.get("/signals", params={"limit": 0})
        assert resp.status_code == 422
        assert resp.json()["detail"] == "validation_error"


# ====================================================================
# /positions
# ====================================================================


class TestPositions:
    @pytest.mark.asyncio
    async def test_defaults_to_open_only(self, client: httpx.AsyncClient):
        resp = await client.get("/positions")
        assert resp.status_code == 200
        body = resp.json()
        assert body["pagination"]["total"] == 2
        assert all(item["size"] > 0 for item in body["items"])

    @pytest.mark.asyncio
    async def test_open_only_false_returns_closed(self, client: httpx.AsyncClient):
        resp = await client.get("/positions", params={"open_only": "false"})
        body = resp.json()
        assert body["pagination"]["total"] == 3

    @pytest.mark.asyncio
    async def test_side_filter(self, client: httpx.AsyncClient):
        resp = await client.get(
            "/positions", params={"side": "YES", "open_only": "false"}
        )
        body = resp.json()
        assert body["pagination"]["total"] == 2
        assert all(item["side"] == "YES" for item in body["items"])


# ====================================================================
# /orders
# ====================================================================


class TestOrders:
    @pytest.mark.asyncio
    async def test_lists_all_orders(self, client: httpx.AsyncClient):
        resp = await client.get("/orders")
        assert resp.status_code == 200
        body = resp.json()
        assert body["pagination"]["total"] == 4

    @pytest.mark.asyncio
    async def test_filters(self, client: httpx.AsyncClient):
        by_market = await client.get("/orders", params={"market_id": "mkt_001"})
        assert by_market.json()["pagination"]["total"] == 2

        by_status = await client.get("/orders", params={"status": "FILLED"})
        body = by_status.json()
        assert body["pagination"]["total"] == 1
        assert body["items"][0]["status"] == "FILLED"

    @pytest.mark.asyncio
    async def test_pagination(self, client: httpx.AsyncClient):
        resp = await client.get("/orders", params={"limit": 2})
        body = resp.json()
        assert len(body["items"]) == 2
        assert body["pagination"]["has_more"] is True


# ====================================================================
# /risk
# ====================================================================


class TestRisk:
    @pytest.mark.asyncio
    async def test_returns_exposure_and_limits(self, client: httpx.AsyncClient):
        resp = await client.get("/risk")
        assert resp.status_code == 200
        body = resp.json()
        assert body["exposure"]["total_exposure"] == 15.0
        assert body["exposure"]["open_positions"] == 2
        assert body["limits"]["max_position_pct"] == settings.max_position_pct
        assert body["limits"]["max_total_exposure_pct"] == settings.max_total_exposure_pct
        assert body["events"]["pagination"]["total"] == 2
        assert len(body["events"]["items"]) == 2

    @pytest.mark.asyncio
    async def test_events_pagination(self, client: httpx.AsyncClient):
        resp = await client.get("/risk", params={"limit": 1})
        body = resp.json()
        assert len(body["events"]["items"]) == 1
        assert body["events"]["pagination"]["has_more"] is True

    @pytest.mark.asyncio
    async def test_empty_database(self):
        db = Database(db_path=":memory:")
        await db.connect()
        await db.init_schema()
        async with await make_client(db) as test_client:
            resp = await test_client.get("/risk")
            body = resp.json()
            assert resp.status_code == 200
            assert body["exposure"]["total_exposure"] == 0.0
            assert body["exposure"]["open_positions"] == 0
            assert body["events"]["items"] == []
        await db.close()


# ====================================================================
# /performance
# ====================================================================


class TestPerformance:
    @pytest.mark.asyncio
    async def test_returns_pnl_and_counts(self, client: httpx.AsyncClient):
        resp = await client.get("/performance")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_realised_pnl"] == 4.0
        assert body["total_unrealised_pnl"] == 0.75
        assert body["total_pnl"] == 4.75
        assert body["open_positions"] == 2
        assert body["total_markets"] == 3
        assert body["total_signals"] == 3
        assert body["total_orders"] == 4
        assert body["filled_orders"] == 2


# ====================================================================
# /audit
# ====================================================================


class TestAudit:
    @pytest.mark.asyncio
    async def test_returns_events(self, client: httpx.AsyncClient):
        resp = await client.get("/audit")
        assert resp.status_code == 200
        body = resp.json()
        assert body["pagination"]["total"] == 2
        assert body["items"][0]["event_type"] == "DAILY_LOSS"

    @pytest.mark.asyncio
    async def test_filters(self, client: httpx.AsyncClient):
        by_type = await client.get("/audit", params={"event_type": "API_FAILURE"})
        assert by_type.json()["pagination"]["total"] == 1

        by_severity = await client.get("/audit", params={"severity": "LOW"})
        body = by_severity.json()
        assert body["pagination"]["total"] == 1
        assert body["items"][0]["event_type"] == "API_FAILURE"

    @pytest.mark.asyncio
    async def test_pagination(self, client: httpx.AsyncClient):
        resp = await client.get("/audit", params={"limit": 1})
        body = resp.json()
        assert len(body["items"]) == 1
        assert body["pagination"]["has_more"] is True


# ====================================================================
# Error handling & safety
# ====================================================================


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_unknown_route_returns_404(self, client: httpx.AsyncClient):
        resp = await client.get("/does-not-exist")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Not Found"

    @pytest.mark.asyncio
    async def test_database_unavailable_returns_503(self):
        db = Database(db_path=":memory:")
        async with await make_client(db) as test_client:
            resp = await test_client.get("/markets")
            assert resp.status_code == 503
            assert resp.json()["detail"] == "database_unavailable"

    @pytest.mark.asyncio
    async def test_unhealthy_database_reports_health_failure(self):
        db = Database(db_path=":memory:")
        async with await make_client(db) as test_client:
            resp = await test_client.get("/health")
            body = resp.json()
            assert resp.status_code == 200
            assert body["checks"]["database"]["healthy"] is False
            assert body["healthy"] is False

    @pytest.mark.asyncio
    async def test_request_id_header_is_set(self, client: httpx.AsyncClient):
        resp = await client.get("/health")
        assert resp.headers.get("X-Request-ID")

    @pytest.mark.asyncio
    async def test_no_write_endpoints_exposed(self, client: httpx.AsyncClient):
        for method in ("post", "put", "patch", "delete"):
            resp = await client.request(method, "/orders")
            assert resp.status_code in (404, 405)
        openapi = (await client.get("/openapi.json")).json()
        for path, methods in openapi["paths"].items():
            # The only writable surface is the keyed /api/control/*
            # emergency kill switch (requires POLY_CONTROL_KEY).
            if path.startswith("/api/control"):
                assert methods.keys() == {"post"}, path
            else:
                assert all(method == "get" for method in methods), path
