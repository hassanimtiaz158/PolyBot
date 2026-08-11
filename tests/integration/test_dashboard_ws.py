"""Tests for the real-time dashboard WebSocket feed.

Covers the ``/ws/dashboard`` endpoint (handshake, auth, heartbeat,
fan-out, disconnect cleanup) and the read-only change detector
(``DashboardBroadcaster``) that decides *when* to publish.

The broadcaster never writes to the database and never serialises
secrets; both guarantees are asserted here.  Endpoint tests drive
delivery through ``TestClient.portal`` so the poller task (which would
race the subscription) is stopped before the client connects.
"""

import asyncio
import json
import time
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api.app import create_app
from app.api.routes import dashboard_ws as dashboard_ws_route
from app.api.websocket_broadcast import (
    CIRCUIT_BREAKER,
    HEALTH_UPDATE,
    MARKET_UPDATE,
    ORDER_UPDATE,
    PNL_UPDATE,
    POSITION_UPDATE,
    RISK_UPDATE,
    SIGNAL_UPDATE,
    DashboardBroadcaster,
)
from app.config.settings import settings
from app.storage.db import Database
from app.storage.models import Market, Order, Position
from app.storage.repositories import MarketRepository, OrderRepository, PositionRepository
from tests.integration.test_api import seed_data

ALL_EVENT_TYPES = {
    MARKET_UPDATE,
    SIGNAL_UPDATE,
    POSITION_UPDATE,
    ORDER_UPDATE,
    PNL_UPDATE,
    RISK_UPDATE,
    HEALTH_UPDATE,
    CIRCUIT_BREAKER,
}

SECRET_KEY = "SUPER-SECRET-KEY-1234"


@pytest_asyncio.fixture
async def seeded_db() -> AsyncGenerator[Database, None]:
    """Fresh in-memory database populated with ``seed_data``."""
    db = Database(db_path=":memory:")
    await db.connect()
    await db.init_schema()
    await seed_data(db)
    yield db
    await db.close()


def _drain(queue: asyncio.Queue[str]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    while not queue.empty():
        messages.append(json.loads(queue.get_nowait()))
    return messages


# ── pub / sub unit behaviour ────────────────────────────────────────


async def test_subscribe_publish_unsubscribe():
    b = DashboardBroadcaster()
    queue = await b.subscribe()
    assert len(b._subscribers) == 1

    await b.publish(SIGNAL_UPDATE, {"items": [], "total": 0})
    message = json.loads(queue.get_nowait())
    assert message["type"] == SIGNAL_UPDATE
    assert message["data"] == {"items": [], "total": 0}
    assert "timestamp" in message

    await b.unsubscribe(queue)
    assert not b._subscribers


async def test_publish_unknown_event_type_is_forwarded():
    b = DashboardBroadcaster()
    queue = await b.subscribe()
    await b.publish("CUSTOM_EVENT", {"x": 1})
    assert json.loads(queue.get_nowait())["type"] == "CUSTOM_EVENT"


async def test_full_queue_drops_oldest_event():
    b = DashboardBroadcaster()
    queue = await b.subscribe()
    for i in range(200):
        await b.publish(SIGNAL_UPDATE, {"i": i})
    # The 201st publish must not block: drop the oldest queued event.
    await b.publish(SIGNAL_UPDATE, {"i": 200})

    messages = _drain(queue)
    assert len(messages) == 200
    assert messages[0]["data"]["i"] == 1
    assert messages[-1]["data"]["i"] == 200


# ── change detection ────────────────────────────────────────────────


async def test_baseline_publishes_every_section_then_stays_quiet(seeded_db):
    b = DashboardBroadcaster(interval=3600, database=seeded_db)
    queue = await b.subscribe()
    await b.emit_changes(seeded_db)
    assert {m["type"] for m in _drain(queue)} == ALL_EVENT_TYPES

    await b.emit_changes(seeded_db)
    assert queue.empty()


async def test_market_insert_publishes_only_market_update(seeded_db):
    b = DashboardBroadcaster(interval=3600, database=seeded_db)
    queue = await b.subscribe()
    await b.emit_changes(seeded_db)
    _drain(queue)

    await MarketRepository(seeded_db).upsert(
        Market(market_id="mkt_004", question="Will event D happen?", status="active")
    )
    await b.emit_changes(seeded_db)

    types = {m["type"] for m in _drain(queue)}
    assert types == {MARKET_UPDATE}


async def test_position_change_publishes_position_and_pnl(seeded_db):
    b = DashboardBroadcaster(interval=3600, database=seeded_db)
    queue = await b.subscribe()
    await b.emit_changes(seeded_db)
    _drain(queue)

    await PositionRepository(seeded_db).upsert(
        Position(
            position_id="pos_001",
            market_id="mkt_001",
            side="YES",
            size=10.0,
            average_entry=0.50,
            current_price=0.55,
            realised_pnl=2.5,
            unrealised_pnl=0.5,
        )
    )
    await b.emit_changes(seeded_db)

    types = {m["type"] for m in _drain(queue)}
    assert POSITION_UPDATE in types
    assert PNL_UPDATE in types
    assert MARKET_UPDATE not in types
    assert SIGNAL_UPDATE not in types


async def test_order_insert_publishes_order_and_pnl_only(seeded_db):
    b = DashboardBroadcaster(interval=3600, database=seeded_db)
    queue = await b.subscribe()
    await b.emit_changes(seeded_db)
    _drain(queue)

    await OrderRepository(seeded_db).insert(
        Order(
            order_id="ord_ws_001",
            market_id="mkt_001",
            side="YES",
            requested_price=0.50,
            requested_size=10.0,
            status="FILLED",
            filled_size=10.0,
            average_fill=0.40,
            submitted_at=datetime.now(UTC).isoformat(),
        )
    )
    await b.emit_changes(seeded_db)

    types = {m["type"] for m in _drain(queue)}
    assert ORDER_UPDATE in types
    assert PNL_UPDATE in types
    assert POSITION_UPDATE not in types
    assert MARKET_UPDATE not in types


async def test_circuit_breaker_change_publishes_breaker_risk_and_pnl(seeded_db):
    b = DashboardBroadcaster(interval=3600, database=seeded_db)
    queue = await b.subscribe()
    await b.emit_changes(seeded_db)
    _drain(queue)

    now = datetime.now(UTC).isoformat()
    await seeded_db.conn.execute(
        "INSERT OR REPLACE INTO circuit_breaker_state (key, value, updated_at) VALUES (?, ?, ?)",
        (
            "circuit_breaker",
            json.dumps(
                {
                    "state": "HALTED",
                    "reasons": ["DAILY_LOSS"],
                    "triggered_at": now,
                }
            ),
            now,
        ),
    )
    await seeded_db.conn.commit()
    await b.emit_changes(seeded_db)

    messages = _drain(queue)
    types = {m["type"] for m in messages}
    assert CIRCUIT_BREAKER in types
    assert RISK_UPDATE in types
    assert PNL_UPDATE in types
    breaker = next(m for m in messages if m["type"] == CIRCUIT_BREAKER)
    assert breaker["data"]["state"] == "HALTED"
    assert breaker["data"]["reasons"] == ["DAILY_LOSS"]
    assert MARKET_UPDATE not in types


async def test_emit_changes_never_exposes_secrets(seeded_db, monkeypatch):
    monkeypatch.setattr(settings, "poly_api_key", SECRET_KEY)
    b = DashboardBroadcaster(interval=3600, database=seeded_db)
    queue = await b.subscribe()
    await b.emit_changes(seeded_db)

    messages = _drain(queue)
    assert {m["type"] for m in messages} == ALL_EVENT_TYPES
    raw = json.dumps(messages)
    assert SECRET_KEY not in raw


# ── /ws/dashboard endpoint ──────────────────────────────────────────
# TestClient is synchronous; these run as plain sync tests so the
# pytest-asyncio loop never touches TestClient's anyio portal thread
# (which deadlocks on Windows during connection teardown).


def test_handshake_sends_connected_then_pushed_events(monkeypatch):
    monkeypatch.setattr(settings, "poly_api_key", None)
    app = create_app()
    with TestClient(app) as client:
        assert client.portal is not None
        client.portal.call(app.state.broadcaster.stop)
        with client.websocket_connect("/ws/dashboard") as ws:
            assert ws.receive_json()["type"] == "CONNECTED"
            client.portal.call(app.state.broadcaster.publish, ORDER_UPDATE, {"total": 1})
            client.portal.call(app.state.broadcaster.publish, SIGNAL_UPDATE, {"total": 2})
            assert ws.receive_json()["type"] == ORDER_UPDATE
            assert ws.receive_json()["type"] == SIGNAL_UPDATE


def test_auth_requires_query_key(monkeypatch):
    monkeypatch.setattr(settings, "poly_api_key", SECRET_KEY)
    app = create_app()
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws/dashboard"):
                pass
        assert exc_info.value.code == 4401


def test_auth_rejects_wrong_key(monkeypatch):
    monkeypatch.setattr(settings, "poly_api_key", SECRET_KEY)
    app = create_app()
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws/dashboard?apiKey=wrong"):
                pass
        assert exc_info.value.code == 4401


def test_auth_accepts_correct_key(monkeypatch):
    monkeypatch.setattr(settings, "poly_api_key", SECRET_KEY)
    app = create_app()
    with TestClient(app) as client:
        with client.websocket_connect("/ws/dashboard?apiKey=" + SECRET_KEY) as ws:
            assert ws.receive_json()["type"] == "CONNECTED"


def test_heartbeat_ping_sent_when_idle(monkeypatch):
    monkeypatch.setattr(settings, "poly_api_key", None)
    monkeypatch.setattr(dashboard_ws_route, "HEARTBEAT_SECONDS", 0.05)
    app = create_app()
    with TestClient(app) as client:
        assert client.portal is not None
        client.portal.call(app.state.broadcaster.stop)
        with client.websocket_connect("/ws/dashboard") as ws:
            assert ws.receive_json()["type"] == "CONNECTED"
            message = ws.receive_json()
            assert message["type"] == "PING"


def test_disconnect_removes_subscriber(monkeypatch):
    monkeypatch.setattr(settings, "poly_api_key", None)
    app = create_app()
    broadcaster = app.state.broadcaster
    with TestClient(app) as client:
        assert client.portal is not None
        client.portal.call(broadcaster.stop)
        with client.websocket_connect("/ws/dashboard") as ws:
            assert ws.receive_json()["type"] == "CONNECTED"
            assert len(broadcaster._subscribers) == 1

    # The endpoint must notice the closed connection and unsubscribe.
    deadline = time.monotonic() + 3.0
    while broadcaster._subscribers and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not broadcaster._subscribers
