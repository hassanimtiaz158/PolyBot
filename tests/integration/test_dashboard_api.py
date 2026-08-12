"""Integration tests for the dashboard API endpoints.

Covers every ``/api/dashboard/*`` endpoint: overview, equity, signals,
markets, positions, orders, performance, risk, health, and audit.
Asserts schema fields, derived aggregates, pagination, filters,
validation errors, and the guarantee that no secrets are exposed.
"""

import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio

from app.api.app import create_app
from app.config.settings import settings
from app.modes.state import OperatingMode
from app.storage.db import Database
from app.storage.models import Market, MarketSnapshot, Order, Position, RiskEvent
from app.storage.repositories import (
    MarketRepository,
    OrderRepository,
    PositionRepository,
    RiskEventRepository,
    SnapshotRepository,
)
from tests.integration.test_api import BASE_URL, seed_data


async def _seed_snapshots(db: Database, *, spread: float = 0.02) -> None:
    """Insert recent, within-limit snapshots for spread/freshness."""
    repo = SnapshotRepository(db)
    now = datetime.now(UTC).isoformat()
    for market_id in ("mkt_001", "mkt_002"):
        await repo.insert(
            MarketSnapshot(
                market_id=market_id,
                timestamp=now,
                bid=0.50 - spread / 2,
                ask=0.50 + spread / 2,
                midpoint=0.50,
                spread=spread,
                bid_depth=10000.0,
                ask_depth=8000.0,
                volume=50000.0,
                time_to_resolution=86400.0,
            )
        )


async def _seed_winning_orders(db: Database) -> None:
    """Add winning filled orders to exercise the equity curve."""
    repo = OrderRepository(db)
    await repo.insert(
        Order(
            order_id="ord_010",
            market_id="mkt_001",
            side="YES",
            status="FILLED",
            requested_price=0.40,
            requested_size=100.0,
            filled_size=100.0,
            average_fill=0.40,
            submitted_at="2026-08-05T10:00:00Z",
            completed_at="2026-08-05T10:01:00Z",
        )
    )
    await repo.insert(
        Order(
            order_id="ord_011",
            market_id="mkt_002",
            side="NO",
            status="FILLED",
            requested_price=0.60,
            requested_size=50.0,
            filled_size=50.0,
            average_fill=0.60,
            submitted_at="2026-08-06T10:00:00Z",
            completed_at="2026-08-06T10:01:00Z",
        )
    )


@pytest_asyncio.fixture
async def api_db() -> AsyncGenerator[Database, None]:
    """In-memory database with base seed plus dashboard-specific rows."""
    db = Database(db_path=":memory:")
    await db.connect()
    await db.init_schema()
    await seed_data(db)
    await _seed_snapshots(db)
    await _seed_winning_orders(db)
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


async def _client(db: Database) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_app(database=db))
    return httpx.AsyncClient(transport=transport, base_url=BASE_URL)


SECRET_MARKERS = (
    "SUPER-SECRET-KEY",
    "SUPER-SECRET-SECRET",
    "PRIVATE-KEY",
    "wallet-private-key",
)


# ====================================================================
# /api/dashboard/overview
# ====================================================================


class TestOverview:
    @pytest.mark.asyncio
    async def test_returns_headline_fields(self, client: httpx.AsyncClient):
        resp = await client.get("/api/dashboard/overview")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {
            "account_balance",
            "available_balance",
            "today_pnl",
            "total_pnl",
            "realized_pnl",
            "unrealized_pnl",
            "max_drawdown",
            "total_exposure",
            "open_positions",
            "active_signals",
            "bot_mode",
            "circuit_breaker",
        }

    @pytest.mark.asyncio
    async def test_derives_balance_from_equity_and_pnl(
        self, client: httpx.AsyncClient
    ):
        body = (await client.get("/api/dashboard/overview")).json()
        assert body["account_balance"] == settings.initial_equity + 4.75
        assert body["available_balance"] == body["account_balance"] - 8.25
        assert body["total_pnl"] == 4.75
        assert body["realized_pnl"] == 4.0
        assert body["unrealized_pnl"] == 0.75

    @pytest.mark.asyncio
    async def test_exposure_is_sum_of_size_times_price(
        self, client: httpx.AsyncClient
    ):
        body = (await client.get("/api/dashboard/overview")).json()
        assert body["total_exposure"] == 10.0 * 0.55 + 5.0 * 0.55

    @pytest.mark.asyncio
    async def test_counts_and_status(self, client: httpx.AsyncClient):
        body = (await client.get("/api/dashboard/overview")).json()
        assert body["open_positions"] == 2
        assert body["active_signals"] == 2
        assert body["today_pnl"] == 0.0
        assert body["max_drawdown"] == 0.0
        assert body["bot_mode"] in {m.value for m in OperatingMode}
        assert body["circuit_breaker"] is None

    @pytest.mark.asyncio
    async def test_does_not_expose_secrets(
        self, client: httpx.AsyncClient, monkeypatch
    ):
        monkeypatch.setattr(settings, "poly_api_key", SECRET_MARKERS[0])
        monkeypatch.setattr(settings, "poly_secret", SECRET_MARKERS[1])
        resp = await client.get("/api/dashboard/overview")
        assert resp.status_code == 200
        for marker in SECRET_MARKERS:
            assert marker not in resp.text


# ====================================================================
# /api/dashboard/equity
# ====================================================================


class TestEquity:
    @pytest.mark.asyncio
    async def test_reconstructs_equity_from_filled_orders(
        self, client: httpx.AsyncClient
    ):
        resp = await client.get("/api/dashboard/equity")
        assert resp.status_code == 200
        points = resp.json()["points"]
        assert len(points) == 5
        assert points[0]["equity"] == settings.initial_equity
        assert points[-1]["equity"] == settings.initial_equity + 15.0
        assert all(point["timestamp"] for point in points)

    @pytest.mark.asyncio
    async def test_empty_when_no_filled_orders(self):
        db = Database(db_path=":memory:")
        await db.connect()
        await db.init_schema()
        async with await _client(db) as test_client:
            resp = await test_client.get("/api/dashboard/equity")
            assert resp.status_code == 200
            assert resp.json()["points"] == []
        await db.close()


# ====================================================================
# /api/dashboard/{signals, markets, positions, orders}
# ====================================================================


class TestDashboardLists:
    @pytest.mark.asyncio
    async def test_signals_list_and_filter(self, client: httpx.AsyncClient):
        resp = await client.get("/api/dashboard/signals")
        assert resp.status_code == 200
        assert resp.json()["pagination"]["total"] == 3

        by_decision = await client.get(
            "/api/dashboard/signals", params={"decision": "CANDIDATE"}
        )
        assert by_decision.json()["pagination"]["total"] == 2

    @pytest.mark.asyncio
    async def test_signals_filter_by_strategy(self, client: httpx.AsyncClient):
        resp = await client.get(
            "/api/dashboard/signals", params={"strategy": "microstructure"}
        )
        body = resp.json()
        assert body["pagination"]["total"] == 2
        assert all(item["strategy"] == "microstructure" for item in body["items"])

    @pytest.mark.asyncio
    async def test_signals_filter_by_market(self, client: httpx.AsyncClient):
        resp = await client.get(
            "/api/dashboard/signals", params={"market_id": "mkt_001"}
        )
        body = resp.json()
        assert body["pagination"]["total"] == 2
        assert all(item["market_id"] == "mkt_001" for item in body["items"])

    @pytest.mark.asyncio
    async def test_signals_filter_by_min_edge(self, client: httpx.AsyncClient):
        resp = await client.get(
            "/api/dashboard/signals", params={"min_edge": 0.04}
        )
        body = resp.json()
        assert body["pagination"]["total"] == 3
        assert all(item["net_edge"] >= 0.04 for item in body["items"])

    @pytest.mark.asyncio
    async def test_signals_filter_by_min_confidence(self, client: httpx.AsyncClient):
        resp = await client.get(
            "/api/dashboard/signals", params={"min_confidence": 0.8}
        )
        body = resp.json()
        assert body["pagination"]["total"] == 3
        assert all(item["confidence"] >= 0.8 for item in body["items"])

    @pytest.mark.asyncio
    async def test_signals_sort_by_net_edge(self, client: httpx.AsyncClient):
        resp = await client.get(
            "/api/dashboard/signals",
            params={"sort_by": "net_edge", "sort_order": "desc"},
        )
        body = resp.json()
        edges = [item["net_edge"] for item in body["items"]]
        assert edges == sorted(edges, reverse=True)

    @pytest.mark.asyncio
    async def test_signals_combined_filters(self, client: httpx.AsyncClient):
        resp = await client.get(
            "/api/dashboard/signals",
            params={
                "strategy": "microstructure",
                "decision": "CANDIDATE",
                "min_edge": 0.03,
            },
        )
        body = resp.json()
        assert body["pagination"]["total"] == 1
        assert body["items"][0]["signal_id"] == "sig_001"

    @pytest.mark.asyncio
    async def test_markets_list_and_pagination(self, client: httpx.AsyncClient):
        resp = await client.get("/api/dashboard/markets", params={"limit": 2})
        body = resp.json()
        assert len(body["items"]) == 2
        assert body["pagination"]["total"] == 3
        assert body["pagination"]["has_more"] is True

    @pytest.mark.asyncio
    async def test_positions_defaults_to_open_only(self, client: httpx.AsyncClient):
        resp = await client.get("/api/dashboard/positions")
        body = resp.json()
        assert body["pagination"]["total"] == 2
        assert all(item["size"] > 0 for item in body["items"])

    @pytest.mark.asyncio
    async def test_positions_filter_by_market(self, client: httpx.AsyncClient):
        resp = await client.get(
            "/api/dashboard/positions", params={"market_id": "mkt_001"}
        )
        body = resp.json()
        assert body["pagination"]["total"] == 1
        assert body["items"][0]["position_id"] == "pos_001"

    @pytest.mark.asyncio
    async def test_positions_include_closed_with_open_only_false(
        self, client: httpx.AsyncClient
    ):
        resp = await client.get(
            "/api/dashboard/positions",
            params={"open_only": False, "market_id": "mkt_003"},
        )
        body = resp.json()
        assert body["pagination"]["total"] == 1
        assert body["items"][0]["position_id"] == "pos_003"

    @pytest.mark.asyncio
    async def test_orders_list_and_status_filter(self, client: httpx.AsyncClient):
        resp = await client.get("/api/dashboard/orders")
        assert resp.json()["pagination"]["total"] == 6

        by_status = await client.get(
            "/api/dashboard/orders", params={"status": "FILLED"}
        )
        body = by_status.json()
        assert body["pagination"]["total"] == 3
        assert all(item["status"] == "FILLED" for item in body["items"])

    @pytest.mark.asyncio
    async def test_orders_filter_by_market(self, client: httpx.AsyncClient):
        resp = await client.get(
            "/api/dashboard/orders", params={"market_id": "mkt_002"}
        )
        body = resp.json()
        assert body["pagination"]["total"] == 3
        assert all(item["market_id"] == "mkt_002" for item in body["items"])

    @pytest.mark.asyncio
    async def test_audit_filter_by_market(self, client: httpx.AsyncClient):
        db = Database(db_path=":memory:")
        await db.connect()
        await db.init_schema()
        await seed_data(db)
        await RiskEventRepository(db).insert(
            RiskEvent(
                event_id="evt_003",
                event_type="RISK_REJECTED",
                severity="MEDIUM",
                market_id="mkt_001",
                timestamp="2026-08-01T08:00:00Z",
            )
        )
        async with await _client(db) as test_client:
            body = (
                await test_client.get(
                    "/api/dashboard/audit", params={"market_id": "mkt_001"}
                )
            ).json()
            assert body["pagination"]["total"] == 1
            assert body["items"][0]["event_type"] == "RISK_REJECTED"
        await db.close()

    @pytest.mark.asyncio
    async def test_validation_errors(self, client: httpx.AsyncClient):
        for path in (
            "/api/dashboard/signals",
            "/api/dashboard/markets",
            "/api/dashboard/positions",
            "/api/dashboard/orders",
            "/api/dashboard/audit",
        ):
            resp = await client.get(path, params={"limit": 0})
            assert resp.status_code == 422
            assert resp.json()["detail"] == "validation_error"


# ====================================================================
# /api/dashboard/positions — enrichment
# ====================================================================


class TestPositionsEnrichment:
    @pytest.mark.asyncio
    async def test_exposes_display_only_derivations(
        self, client: httpx.AsyncClient
    ):
        body = (await client.get("/api/dashboard/positions")).json()
        by_id = {item["position_id"]: item for item in body["items"]}

        # pos_001: 10 YES @ 0.50, current 0.55, unrealised 0.50.
        p = by_id["pos_001"]
        assert p["exposure"] == 10.0 * 0.55
        assert p["return_pct"] == 0.5 / (10.0 * 0.50)
        assert p["time_to_resolution"] == 86400.0  # snapshot fallback
        assert p["risk_status"] == "NORMAL"

        # pos_002: 5 NO @ 0.50, current 0.55, unrealised 0.25.
        p = by_id["pos_002"]
        assert p["exposure"] == 5.0 * 0.55
        assert p["return_pct"] == 0.25 / (5.0 * 0.50)
        assert p["risk_status"] == "NORMAL"

    @pytest.mark.asyncio
    async def test_risk_status_reflects_unrealised_loss(
        self, client: httpx.AsyncClient
    ):
        db = Database(db_path=":memory:")
        await db.connect()
        await db.init_schema()
        await seed_data(db)
        await PositionRepository(db).upsert(
            Position(
                position_id="pos_100",
                market_id="mkt_001",
                side="YES",
                size=10.0,
                average_entry=0.50,
                current_price=0.10,
                realised_pnl=0.0,
                unrealised_pnl=-4.0,
            )
        )
        async with await _client(db) as test_client:
            body = (
                await test_client.get("/api/dashboard/positions")
            ).json()
            by_id = {item["position_id"]: item for item in body["items"]}
            assert by_id["pos_100"]["risk_status"] == "CRITICAL"  # -80% of cost
        await db.close()

    @pytest.mark.asyncio
    async def test_time_to_resolution_uses_market_resolution_time(
        self, client: httpx.AsyncClient
    ):
        db = Database(db_path=":memory:")
        await db.connect()
        await db.init_schema()
        await seed_data(db)
        future = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
        await MarketRepository(db).upsert(
            Market(
                market_id="mkt_001",
                question="Will event A happen?",
                status="active",
                resolution_time=future,
            )
        )
        async with await _client(db) as test_client:
            body = (
                await test_client.get(
                    "/api/dashboard/positions", params={"market_id": "mkt_001"}
                )
            ).json()
            ttr = body["items"][0]["time_to_resolution"]
            assert 1.5 * 3600 < ttr <= 2.0 * 3600
        await db.close()

    @pytest.mark.asyncio
    async def test_return_pct_null_without_entry(self):
        db = Database(db_path=":memory:")
        await db.connect()
        await db.init_schema()
        await seed_data(db)
        await PositionRepository(db).upsert(
            Position(
                position_id="pos_101",
                market_id="mkt_001",
                side="YES",
                size=10.0,
                average_entry=0.0,
                current_price=0.55,
                realised_pnl=0.0,
                unrealised_pnl=0.5,
            )
        )
        async with await _client(db) as test_client:
            body = (
                await test_client.get("/api/dashboard/positions")
            ).json()
            by_id = {item["position_id"]: item for item in body["items"]}
            assert by_id["pos_101"]["return_pct"] is None
        await db.close()


# ====================================================================
# /api/dashboard/markets/{market_id}/snapshots
# ====================================================================


class TestMarketSnapshots:
    @pytest.mark.asyncio
    async def test_returns_snapshot_rows_for_market(
        self, client: httpx.AsyncClient
    ):
        resp = await client.get("/api/dashboard/markets/mkt_001/snapshots")
        assert resp.status_code == 200
        body = resp.json()
        assert body["pagination"]["total"] == 1
        row = body["items"][0]
        assert row["market_id"] == "mkt_001"
        assert row["bid"] == 0.49
        assert row["ask"] == 0.51
        assert row["midpoint"] == 0.50
        assert row["spread"] == 0.02
        assert row["time_to_resolution"] == 86400.0

    @pytest.mark.asyncio
    async def test_orders_newest_first(self, client: httpx.AsyncClient):
        db = Database(db_path=":memory:")
        await db.connect()
        await db.init_schema()
        await seed_data(db)
        repo = SnapshotRepository(db)
        old = "2026-08-01T10:00:00Z"
        new = "2026-08-02T10:00:00Z"
        await repo.insert(
            MarketSnapshot(
                market_id="mkt_001", timestamp=old, midpoint=0.40,
                spread=0.02, time_to_resolution=1000.0,
            )
        )
        await repo.insert(
            MarketSnapshot(
                market_id="mkt_001", timestamp=new, midpoint=0.60,
                spread=0.02, time_to_resolution=2000.0,
            )
        )
        async with await _client(db) as test_client:
            body = (
                await test_client.get("/api/dashboard/markets/mkt_001/snapshots")
            ).json()
            assert body["pagination"]["total"] == 2
            assert body["items"][0]["midpoint"] == 0.60  # newest first
            assert body["items"][1]["midpoint"] == 0.40
        await db.close()

    @pytest.mark.asyncio
    async def test_empty_when_no_snapshots(self, client: httpx.AsyncClient):
        resp = await client.get("/api/dashboard/markets/mkt_003/snapshots")
        body = resp.json()
        assert body["pagination"]["total"] == 0
        assert body["items"] == []


# ====================================================================
# /api/dashboard/performance
# ====================================================================


class TestPerformance:
    @pytest.mark.asyncio
    async def test_returns_pnl_and_counts(self, client: httpx.AsyncClient):
        resp = await client.get("/api/dashboard/performance")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_realised_pnl"] == 4.0
        assert body["total_unrealised_pnl"] == 0.75
        assert body["total_pnl"] == 4.75
        assert body["open_positions"] == 2
        assert body["total_markets"] == 3
        assert body["total_signals"] == 3
        assert body["total_orders"] == 6
        assert body["filled_orders"] == 4
        assert body["timestamp"]


# ====================================================================
# /api/dashboard/risk
# ====================================================================


class TestRisk:
    @pytest.mark.asyncio
    async def test_returns_utilisation_and_status(
        self, client: httpx.AsyncClient
    ):
        resp = await client.get("/api/dashboard/risk")
        assert resp.status_code == 200
        body = resp.json()
        account = settings.initial_equity + 4.75
        assert body["daily_loss"] == 0.0
        assert body["daily_loss_limit"] == round(account * settings.max_daily_loss_pct, 6)
        assert body["exposure"] == 8.25
        assert body["exposure_limit"] == round(
            account * settings.max_total_exposure_pct, 6
        )
        assert body["consecutive_losses"] == 0
        assert body["consecutive_loss_limit"] == settings.max_consecutive_losses
        assert body["spread_status"] == "OK"
        assert body["liquidity_status"] == "OK"
        assert body["data_freshness"] in ("FRESH", "STALE")
        assert body["circuit_breaker"] is None

    @pytest.mark.asyncio
    async def test_returns_account_and_loss_control_fields(
        self, client: httpx.AsyncClient
    ):
        body = (await client.get("/api/dashboard/risk")).json()
        account = settings.initial_equity + 4.75
        exposure_limit = round(account * settings.max_total_exposure_pct, 6)
        assert body["account_balance"] == round(account, 6)
        assert body["available_balance"] == round(account - 8.25, 6)
        assert body["today_pnl"] == 0.0
        assert body["exposure_pct"] == round(8.25 / exposure_limit * 100, 4)
        assert body["daily_loss_limit"] == round(
            account * settings.max_daily_loss_pct, 6
        )
        assert body["consecutive_loss_limit"] == settings.max_consecutive_losses

    @pytest.mark.asyncio
    async def test_returns_market_risk_fields(self, client: httpx.AsyncClient):
        body = (await client.get("/api/dashboard/risk")).json()
        assert body["open_positions"] == 2
        assert body["max_open_positions"] == settings.max_open_positions
        # pos_001 (10 YES @ 0.55) is the largest single position and market.
        assert body["largest_position"] == 10.0 * 0.55
        assert body["largest_position_market"] == "mkt_001"
        assert body["largest_market_exposure"] == 10.0 * 0.55
        # Two fresh snapshots, both at spread 0.02.
        assert body["average_spread"] == 0.02
        # All seeded markets carry 50000 liquidity.
        assert body["minimum_liquidity"] == 50000.0

    @pytest.mark.asyncio
    async def test_market_risk_empty_without_positions(self):
        db = Database(db_path=":memory:")
        await db.connect()
        await db.init_schema()
        async with await _client(db) as test_client:
            body = (await test_client.get("/api/dashboard/risk")).json()
            assert body["open_positions"] == 0
            assert body["largest_position"] == 0.0
            assert body["largest_position_market"] is None
            assert body["largest_market_exposure"] == 0.0
            assert body["average_spread"] is None
            assert body["minimum_liquidity"] is None
            assert body["exposure_pct"] == 0.0
        await db.close()

    @pytest.mark.asyncio
    async def test_reports_loss_streak_from_filled_orders(self):
        db = Database(db_path=":memory:")
        await db.connect()
        await db.init_schema()
        await seed_data(db)
        now = datetime.now(UTC).isoformat()
        repo = OrderRepository(db)
        await repo.insert(
            Order(
                order_id="ord_100",
                market_id="mkt_001",
                side="YES",
                status="FILLED",
                requested_price=0.55,
                requested_size=100.0,
                filled_size=100.0,
                average_fill=0.55,
                submitted_at=now,
                completed_at=now,
            )
        )
        await repo.insert(
            Order(
                order_id="ord_101",
                market_id="mkt_002",
                side="YES",
                status="FILLED",
                requested_price=0.60,
                requested_size=100.0,
                filled_size=100.0,
                average_fill=0.60,
                submitted_at=now,
                completed_at=now,
            )
        )
        async with await _client(db) as test_client:
            body = (await test_client.get("/api/dashboard/risk")).json()
            # Two losing YES fills at 0.55/0.60: -5.0 and -10.0 today.
            assert body["consecutive_losses"] == 2
            assert body["daily_loss"] == 15.0
        await db.close()

    @pytest.mark.asyncio
    async def test_unknown_statuses_without_snapshots(self):
        db = Database(db_path=":memory:")
        await db.connect()
        await db.init_schema()
        await seed_data(db)
        async with await _client(db) as test_client:
            body = (await test_client.get("/api/dashboard/risk")).json()
            assert body["spread_status"] == "UNKNOWN"
            assert body["liquidity_status"] == "OK"
            assert body["data_freshness"] == "UNKNOWN"
        await db.close()

    @pytest.mark.asyncio
    async def test_high_spread_and_stale_freshness(self, monkeypatch):
        db = Database(db_path=":memory:")
        await db.connect()
        await db.init_schema()
        await seed_data(db)
        old = datetime.now(UTC) - timedelta(hours=1)
        repo = SnapshotRepository(db)
        await repo.insert(
            MarketSnapshot(
                market_id="mkt_001",
                timestamp=old.isoformat(),
                bid=0.45,
                ask=0.55,
                midpoint=0.50,
                spread=0.10,
                bid_depth=10000.0,
                ask_depth=8000.0,
                volume=50000.0,
                time_to_resolution=86400.0,
            )
        )
        monkeypatch.setattr(settings, "data_max_age_seconds", 60)
        async with await _client(db) as test_client:
            body = (await test_client.get("/api/dashboard/risk")).json()
            assert body["spread_status"] == "HIGH"
            assert body["data_freshness"] == "STALE"
        await db.close()

    @pytest.mark.asyncio
    async def test_fresh_status_within_window(self, monkeypatch):
        db = Database(db_path=":memory:")
        await db.connect()
        await db.init_schema()
        await seed_data(db)
        recent = (datetime.now(UTC) - timedelta(seconds=2)).isoformat()
        await SnapshotRepository(db).insert(
            MarketSnapshot(
                market_id="mkt_001",
                timestamp=recent,
                bid=0.45,
                ask=0.55,
                midpoint=0.50,
                spread=0.10,
                bid_depth=10000.0,
                ask_depth=8000.0,
                volume=50000.0,
                time_to_resolution=86400.0,
            )
        )
        monkeypatch.setattr(settings, "data_max_age_seconds", 60)
        async with await _client(db) as test_client:
            body = (await test_client.get("/api/dashboard/risk")).json()
            assert body["data_freshness"] == "FRESH"
        await db.close()

    @pytest.mark.asyncio
    async def test_reads_persisted_circuit_breaker(self, client: httpx.AsyncClient):
        payload = json.dumps({
            "state": "HALTED",
            "reasons": ["DAILY_LOSS"],
            "triggered_at": "2026-08-01T10:00:00Z",
        })
        db = Database(db_path=":memory:")
        await db.connect()
        await db.init_schema()
        await seed_data(db)
        await db.conn.execute(
            "INSERT OR REPLACE INTO circuit_breaker_state "
            "(key, value, updated_at) VALUES ('circuit_breaker', ?, ?)",
            (payload, "2026-08-01T10:00:00Z"),
        )
        await db.conn.commit()
        async with await _client(db) as test_client:
            body = (await test_client.get("/api/dashboard/risk")).json()
            assert body["circuit_breaker"]["state"] == "HALTED"
            assert body["circuit_breaker"]["reasons"] == ["DAILY_LOSS"]
        await db.close()


# ====================================================================
# /api/dashboard/health
# ====================================================================


class TestHealth:
    @pytest.mark.asyncio
    async def test_reports_expected_checks(self, client: httpx.AsyncClient):
        resp = await client.get("/api/dashboard/health")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body["checks"]) == {
            "database",
            "data_freshness",
            "api",
            "model_availability",
        }
        assert body["checks"]["database"]["healthy"] is True
        assert body["timestamp"]


# ====================================================================
# /api/dashboard/audit
# ====================================================================


class TestAudit:
    @pytest.mark.asyncio
    async def test_returns_events_and_filter(self, client: httpx.AsyncClient):
        resp = await client.get("/api/dashboard/audit")
        assert resp.status_code == 200
        body = resp.json()
        assert body["pagination"]["total"] == 2
        assert body["items"][0]["event_type"] == "DAILY_LOSS"

        by_severity = await client.get(
            "/api/dashboard/audit", params={"severity": "LOW"}
        )
        filtered = by_severity.json()
        assert filtered["pagination"]["total"] == 1
        assert filtered["items"][0]["event_type"] == "API_FAILURE"


# ====================================================================
# Safety
# ====================================================================


class TestSafety:
    @pytest.mark.asyncio
    async def test_no_dashboard_endpoint_exposes_secrets(
        self, client: httpx.AsyncClient, monkeypatch
    ):
        monkeypatch.setattr(settings, "poly_api_key", SECRET_MARKERS[0])
        monkeypatch.setattr(settings, "poly_secret", SECRET_MARKERS[1])
        for path in (
            "/api/dashboard/overview",
            "/api/dashboard/equity",
            "/api/dashboard/signals",
            "/api/dashboard/markets",
            "/api/dashboard/positions",
            "/api/dashboard/orders",
            "/api/dashboard/performance",
            "/api/dashboard/risk",
            "/api/dashboard/health",
            "/api/dashboard/audit",
        ):
            resp = await client.get(path)
            assert resp.status_code == 200, path
            for marker in SECRET_MARKERS:
                assert marker not in resp.text, path

    @pytest.mark.asyncio
    async def test_no_write_endpoints_under_dashboard(
        self, client: httpx.AsyncClient
    ):
        for method in ("post", "put", "patch", "delete"):
            resp = await client.request(method, "/api/dashboard/overview")
            assert resp.status_code in (404, 405), method
