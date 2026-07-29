"""Comprehensive integration tests for the database and repository layer."""

import pytest

from app.storage.db import Database, DatabaseConnectionError
from app.storage.models import (
    Market,
    MarketSnapshot,
    Order,
    Position,
    RiskEvent,
    Signal,
)
from app.storage.repositories import (
    MarketRepository,
    OrderRepository,
    PositionRepository,
    RiskEventRepository,
    SignalRepository,
    SnapshotRepository,
)

# ====================================================================
# Database initialisation & connection
# ====================================================================


@pytest.mark.asyncio
async def test_connect_and_init_schema(isolated_db: Database):
    """Verify that init_schema creates all expected tables."""
    conn = isolated_db.conn
    cursor = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = {row["name"] for row in await cursor.fetchall()}
    expected = {
        "markets",
        "market_snapshots",
        "signals",
        "orders",
        "positions",
        "risk_events",
        "circuit_breaker_state",
        "_schema_version",
    }
    assert expected.issubset(tables), f"Missing tables: {expected - tables}"


@pytest.mark.asyncio
async def test_connect_and_init_schema_creates_indexes(isolated_db: Database):
    """Verify that indexes are created."""
    conn = isolated_db.conn
    cursor = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
    )
    indexes = {row["name"] for row in await cursor.fetchall()}
    assert "idx_snapshots_market_ts" in indexes
    assert "idx_signals_market_ts" in indexes
    assert "idx_orders_market_status" in indexes
    assert "idx_risk_events_ts" in indexes


@pytest.mark.asyncio
async def test_database_health_check(isolated_db: Database):
    """Verify the health check returns True for a connected db."""
    assert await isolated_db.health() is True


@pytest.mark.asyncio
async def test_database_health_check_fails_when_closed():
    """Verify health check returns False when not connected."""
    d = Database(db_path=":memory:")
    assert await d.health() is False


@pytest.mark.asyncio
async def test_close_and_reopen(isolated_db: Database):
    """Verify the database can be closed and re-opened."""
    await isolated_db.close()
    assert isolated_db.is_connected is False
    await isolated_db.connect()
    assert isolated_db.is_connected is True
    ok = await isolated_db.health()
    assert ok is True


@pytest.mark.asyncio
async def test_conn_raises_when_not_connected():
    """Verify accessing conn before connect raises."""
    d = Database(db_path=":memory:")
    with pytest.raises(DatabaseConnectionError):
        _ = d.conn


# ====================================================================
# MarketRepository
# ====================================================================


class TestMarketRepository:
    @pytest.mark.asyncio
    async def test_insert_and_get(self, isolated_db: Database):
        repo = MarketRepository(isolated_db)
        m = Market(market_id="m1", question="Q1")
        await repo.upsert(m)
        fetched = await repo.get("m1")
        assert fetched is not None
        assert fetched.market_id == "m1"
        assert fetched.question == "Q1"

    @pytest.mark.asyncio
    async def test_upsert_updates_existing(self, isolated_db: Database):
        repo = MarketRepository(isolated_db)
        await repo.upsert(Market(market_id="m1", question="Original"))
        await repo.upsert(Market(market_id="m1", question="Updated"))
        fetched = await repo.get("m1")
        assert fetched is not None
        assert fetched.question == "Updated"

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(self, isolated_db: Database):
        repo = MarketRepository(isolated_db)
        assert await repo.get("nonexistent") is None

    @pytest.mark.asyncio
    async def test_list_all(self, isolated_db: Database):
        repo = MarketRepository(isolated_db)
        await repo.upsert(Market(market_id="m1", question="Q1"))
        await repo.upsert(Market(market_id="m2", question="Q2"))
        all_markets = await repo.list_all()
        assert len(all_markets) == 2
        ids = {m.market_id for m in all_markets}
        assert ids == {"m1", "m2"}

    @pytest.mark.asyncio
    async def test_delete(self, isolated_db: Database):
        repo = MarketRepository(isolated_db)
        await repo.upsert(Market(market_id="m1", question="Q1"))
        assert await repo.delete("m1") is True
        assert await repo.get("m1") is None
        assert await repo.delete("m1") is False

    @pytest.mark.asyncio
    async def test_count(self, isolated_db: Database):
        repo = MarketRepository(isolated_db)
        assert await repo.count() == 0
        await repo.upsert(Market(market_id="m1", question="Q1"))
        assert await repo.count() == 1


# ====================================================================
# SnapshotRepository
# ====================================================================


class TestSnapshotRepository:
    @pytest.mark.asyncio
    async def test_insert_and_get(self, isolated_db: Database):
        mrepo = MarketRepository(isolated_db)
        await mrepo.upsert(Market(market_id="m1", question="Q1"))
        repo = SnapshotRepository(isolated_db)
        snap = MarketSnapshot(market_id="m1", bid=0.45, ask=0.55)
        snap_id = await repo.insert(snap)
        assert snap_id is not None and snap_id > 0
        fetched = await repo.get(snap_id)
        assert fetched is not None
        assert fetched.bid == 0.45

    @pytest.mark.asyncio
    async def test_list_by_market(self, isolated_db: Database):
        mrepo = MarketRepository(isolated_db)
        await mrepo.upsert(Market(market_id="m1", question="Q1"))
        repo = SnapshotRepository(isolated_db)
        for i in range(3):
            await repo.insert(MarketSnapshot(market_id="m1", bid=0.40 + i * 0.05, ask=0.60))
        snaps = await repo.list_by_market("m1", limit=2)
        assert len(snaps) == 2

    @pytest.mark.asyncio
    async def test_count_by_market(self, isolated_db: Database):
        mrepo = MarketRepository(isolated_db)
        await mrepo.upsert(Market(market_id="m1", question="Q1"))
        repo = SnapshotRepository(isolated_db)
        await repo.insert(MarketSnapshot(market_id="m1", bid=0.45, ask=0.55))
        assert await repo.count_by_market("m1") == 1

    @pytest.mark.asyncio
    async def test_delete_older_than(self, isolated_db: Database):
        mrepo = MarketRepository(isolated_db)
        await mrepo.upsert(Market(market_id="m1", question="Q1"))
        repo = SnapshotRepository(isolated_db)
        await repo.insert(
            MarketSnapshot(market_id="m1", bid=0.45, ask=0.55, timestamp="2024-01-01T00:00:00Z")
        )
        await repo.insert(
            MarketSnapshot(market_id="m1", bid=0.50, ask=0.60, timestamp="2025-01-01T00:00:00Z")
        )
        deleted = await repo.delete_older_than("m1", "2024-06-01T00:00:00Z")
        assert deleted == 1


# ====================================================================
# SignalRepository
# ====================================================================


class TestSignalRepository:
    @pytest.mark.asyncio
    async def test_insert_and_get(self, isolated_db: Database):
        mrepo = MarketRepository(isolated_db)
        await mrepo.upsert(Market(market_id="m1", question="Q1"))
        repo = SignalRepository(isolated_db)
        s = Signal(signal_id="s1", market_id="m1", strategy="ms", side="YES", decision="BUY")
        await repo.insert(s)
        fetched = await repo.get("s1")
        assert fetched is not None
        assert fetched.strategy == "ms"
        assert fetched.decision == "BUY"

    @pytest.mark.asyncio
    async def test_duplicate_signal_is_idempotent(self, isolated_db: Database):
        mrepo = MarketRepository(isolated_db)
        await mrepo.upsert(Market(market_id="m1", question="Q1"))
        repo = SignalRepository(isolated_db)
        s = Signal(signal_id="s1", market_id="m1", strategy="ms", side="YES", decision="BUY")
        await repo.insert(s)
        await repo.insert(s)  # second insert should not raise
        assert await repo.count() == 1

    @pytest.mark.asyncio
    async def test_list_recent(self, isolated_db: Database):
        mrepo = MarketRepository(isolated_db)
        await mrepo.upsert(Market(market_id="m1", question="Q1"))
        repo = SignalRepository(isolated_db)
        for i in range(5):
            sig = Signal(
                signal_id=f"s{i}", market_id="m1",
                strategy="ms", side="YES", decision="HOLD",
            )
            await repo.insert(sig)
        recent = await repo.list_recent(limit=3)
        assert len(recent) == 3

    @pytest.mark.asyncio
    async def test_list_recent_filtered_by_market(self, isolated_db: Database):
        mrepo = MarketRepository(isolated_db)
        await mrepo.upsert(Market(market_id="m1", question="Q1"))
        await mrepo.upsert(Market(market_id="m2", question="Q2"))
        repo = SignalRepository(isolated_db)
        for mid in ("m1", "m2"):
            await repo.insert(
                Signal(
                    signal_id=f"s_{mid}", market_id=mid,
                    strategy="ms", side="YES", decision="BUY",
                )
            )
        filtered = await repo.list_recent(market_id="m1")
        assert len(filtered) == 1
        assert filtered[0].market_id == "m1"


# ====================================================================
# OrderRepository
# ====================================================================


class TestOrderRepository:
    @pytest.mark.asyncio
    async def test_insert_and_get(self, isolated_db: Database):
        mrepo = MarketRepository(isolated_db)
        await mrepo.upsert(Market(market_id="m1", question="Q1"))
        repo = OrderRepository(isolated_db)
        o = Order(order_id="ord1", market_id="m1", side="YES", status="CREATED")
        await repo.insert(o)
        fetched = await repo.get("ord1")
        assert fetched is not None
        assert fetched.status == "CREATED"

    @pytest.mark.asyncio
    async def test_update_status(self, isolated_db: Database):
        mrepo = MarketRepository(isolated_db)
        await mrepo.upsert(Market(market_id="m1", question="Q1"))
        repo = OrderRepository(isolated_db)
        await repo.insert(Order(order_id="ord1", market_id="m1", side="YES", status="CREATED"))
        await repo.update_status(
            Order(order_id="ord1", market_id="m1", side="YES", status="FILLED", filled_size=10.0,
                  average_fill=0.50)
        )
        fetched = await repo.get("ord1")
        assert fetched is not None
        assert fetched.status == "FILLED"
        assert fetched.filled_size == 10.0

    @pytest.mark.asyncio
    async def test_list_by_market(self, isolated_db: Database):
        mrepo = MarketRepository(isolated_db)
        await mrepo.upsert(Market(market_id="m1", question="Q1"))
        await mrepo.upsert(Market(market_id="m2", question="Q2"))
        repo = OrderRepository(isolated_db)
        await repo.insert(Order(order_id="o1", market_id="m1", side="YES", status="CREATED"))
        await repo.insert(Order(order_id="o2", market_id="m2", side="NO", status="CREATED"))
        m1_orders = await repo.list_by_market("m1")
        assert len(m1_orders) == 1

    @pytest.mark.asyncio
    async def test_list_open(self, isolated_db: Database):
        mrepo = MarketRepository(isolated_db)
        await mrepo.upsert(Market(market_id="m1", question="Q1"))
        repo = OrderRepository(isolated_db)
        await repo.insert(Order(order_id="o1", market_id="m1", side="YES", status="CREATED"))
        await repo.insert(Order(order_id="o2", market_id="m1", side="YES", status="FILLED"))
        open_orders = await repo.list_open()
        assert len(open_orders) == 1
        assert open_orders[0].order_id == "o1"


# ====================================================================
# PositionRepository
# ====================================================================


class TestPositionRepository:
    @pytest.mark.asyncio
    async def test_upsert_and_get(self, isolated_db: Database):
        mrepo = MarketRepository(isolated_db)
        await mrepo.upsert(Market(market_id="m1", question="Q1"))
        repo = PositionRepository(isolated_db)
        p = Position(position_id="p1", market_id="m1", side="YES", size=10.0)
        await repo.upsert(p)
        fetched = await repo.get("p1")
        assert fetched is not None
        assert fetched.size == 10.0

    @pytest.mark.asyncio
    async def test_upsert_updates(self, isolated_db: Database):
        mrepo = MarketRepository(isolated_db)
        await mrepo.upsert(Market(market_id="m1", question="Q1"))
        repo = PositionRepository(isolated_db)
        await repo.upsert(Position(position_id="p1", market_id="m1", side="YES", size=10.0))
        await repo.upsert(Position(position_id="p1", market_id="m1", side="YES", size=20.0))
        fetched = await repo.get("p1")
        assert fetched is not None
        assert fetched.size == 20.0

    @pytest.mark.asyncio
    async def test_list_open(self, isolated_db: Database):
        mrepo = MarketRepository(isolated_db)
        await mrepo.upsert(Market(market_id="m1", question="Q1"))
        repo = PositionRepository(isolated_db)
        await repo.upsert(Position(position_id="p1", market_id="m1", side="YES", size=10.0))
        await repo.upsert(Position(position_id="p2", market_id="m1", side="NO", size=0.0))
        open_pos = await repo.list_open()
        assert len(open_pos) == 1
        assert open_pos[0].position_id == "p1"

    @pytest.mark.asyncio
    async def test_total_exposure(self, isolated_db: Database):
        mrepo = MarketRepository(isolated_db)
        await mrepo.upsert(Market(market_id="m1", question="Q1"))
        repo = PositionRepository(isolated_db)
        await repo.upsert(Position(position_id="p1", market_id="m1", side="YES", size=10.0))
        await repo.upsert(Position(position_id="p2", market_id="m1", side="NO", size=-5.0))
        exposure = await repo.total_exposure()
        assert exposure == 15.0


# ====================================================================
# RiskEventRepository
# ====================================================================


class TestRiskEventRepository:
    @pytest.mark.asyncio
    async def test_insert_and_get(self, isolated_db: Database):
        repo = RiskEventRepository(isolated_db)
        e = RiskEvent(event_id="e1", event_type="DAILY_LOSS", severity="HIGH")
        await repo.insert(e)
        fetched = await repo.get("e1")
        assert fetched is not None
        assert fetched.event_type == "DAILY_LOSS"

    @pytest.mark.asyncio
    async def test_duplicate_event_idempotent(self, isolated_db: Database):
        repo = RiskEventRepository(isolated_db)
        e = RiskEvent(event_id="e1", event_type="DAILY_LOSS", severity="HIGH")
        await repo.insert(e)
        await repo.insert(e)
        assert await repo.count() == 1

    @pytest.mark.asyncio
    async def test_list_recent(self, isolated_db: Database):
        repo = RiskEventRepository(isolated_db)
        for i in range(5):
            await repo.insert(
                RiskEvent(event_id=f"e{i}", event_type="INFO", severity="LOW")
            )
        recent = await repo.list_recent(limit=3)
        assert len(recent) == 3

    @pytest.mark.asyncio
    async def test_list_recent_filtered(self, isolated_db: Database):
        repo = RiskEventRepository(isolated_db)
        await repo.insert(RiskEvent(event_id="e1", event_type="DAILY_LOSS", severity="HIGH"))
        await repo.insert(RiskEvent(event_id="e2", event_type="API_ERROR", severity="MEDIUM"))
        filtered = await repo.list_recent(event_type="DAILY_LOSS")
        assert len(filtered) == 1


# ====================================================================
# Transaction safety
# ====================================================================


class TestTransactions:
    @pytest.mark.asyncio
    async def test_transaction_rollback_on_error(self, isolated_db: Database):
        """Verify that an exception inside a transaction rolls back."""
        mrepo = MarketRepository(isolated_db)
        await mrepo.upsert(Market(market_id="m1", question="Q1"))
        try:
            async with isolated_db.transaction():
                await isolated_db.conn.execute(
                    "UPDATE markets SET question = ? WHERE market_id = ?",
                    ("Updated", "m1"),
                )
                raise ValueError("simulated failure")
        except ValueError:
            pass
        # The update should have been rolled back
        fetched = await mrepo.get("m1")
        assert fetched is not None
        assert fetched.question == "Q1"

    @pytest.mark.asyncio
    async def test_transaction_commit_success(self, isolated_db: Database):
        """Verify changes are committed after a successful transaction."""
        mrepo = MarketRepository(isolated_db)
        await mrepo.upsert(Market(market_id="m1", question="Q1"))
        async with isolated_db.transaction():
            await isolated_db.conn.execute(
                "UPDATE markets SET question = ? WHERE market_id = ?",
                ("Committed", "m1"),
            )
        fetched = await mrepo.get("m1")
        assert fetched is not None
        assert fetched.question == "Committed"


# ====================================================================
# Invalid / edge-case data
# ====================================================================


class TestInvalidData:
    @pytest.mark.asyncio
    async def test_insert_market_with_empty_question(self, isolated_db: Database):
        """An empty string is acceptable; NULL is not (NOT NULL column)."""
        repo = MarketRepository(isolated_db)
        with pytest.raises(Exception):
            await repo.upsert(Market(market_id="bad", question=None))  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_foreign_key_violation_on_snapshot(self, isolated_db: Database):
        """Inserting a snapshot for a non-existent market should fail."""
        repo = SnapshotRepository(isolated_db)
        snap = MarketSnapshot(market_id="nonexistent", bid=0.45, ask=0.55)
        with pytest.raises(Exception):
            await repo.insert(snap)

    @pytest.mark.asyncio
    async def test_duplicate_market_is_updated_not_duplicated(self, isolated_db: Database):
        """Upsert on same PK should update, not create a second row."""
        repo = MarketRepository(isolated_db)
        await repo.upsert(Market(market_id="m1", question="Q1"))
        await repo.upsert(Market(market_id="m1", question="Q2"))
        assert await repo.count() == 1
