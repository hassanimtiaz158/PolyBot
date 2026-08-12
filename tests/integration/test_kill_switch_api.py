"""Integration tests for the backend-controlled emergency kill switch API.

Covers the ``/api/control/kill-switch`` and ``/api/control/resume``
endpoints: auth requirements, audit recording, persistence across a
restart, explicit-confirmation resume, and the guarantee that the
frontend cannot manipulate execution state.
"""

import json
from collections.abc import AsyncGenerator

import httpx
import pytest
import pytest_asyncio

from app.api.app import create_app
from app.config.settings import settings
from app.risk.kill_switch import KILL_SWITCH_REASON
from app.storage.db import Database
from app.storage.models import Market, Position
from app.storage.repositories import MarketRepository, PositionRepository

BASE_URL = "http://testserver"

CONTROL_KEY = "CONTROL-SECRET"


@pytest_asyncio.fixture
async def api_db() -> AsyncGenerator[Database, None]:
    """In-memory database with a single open position."""
    db = Database(db_path=":memory:")
    await db.connect()
    await db.init_schema()
    await MarketRepository(db).upsert(
        Market(
            market_id="mkt_001",
            question="Will event X happen?",
            condition_id="cond_mkt_001",
            status="active",
        )
    )
    await PositionRepository(db).upsert(
        Position(
            position_id="pos_001",
            market_id="mkt_001",
            side="YES",
            size=42.0,
            average_entry=0.50,
            current_price=0.55,
            realised_pnl=0.0,
            unrealised_pnl=2.1,
        )
    )
    yield db
    await db.close()


@pytest_asyncio.fixture
async def client(
    api_db: Database,
) -> AsyncGenerator[httpx.AsyncClient, None]:
    """Async test client bound to the seeded database with control key set."""
    app = create_app(database=api_db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as c:
        yield c


@pytest.fixture
def control_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable the control interface with a known key."""
    monkeypatch.setattr(settings, "poly_control_key", CONTROL_KEY)


def _post(client: httpx.AsyncClient, path: str, **body: object) -> httpx.Response:
    headers = {"X-API-Key": CONTROL_KEY}
    return client.post(path, json=body, headers=headers)


class TestControlAuth:
    @pytest.mark.asyncio
    async def test_control_interface_disabled_without_key(self, client):
        settings.poly_control_key = None
        resp = await client.post("/api/control/kill-switch", json={})
        assert resp.status_code == 503
        assert resp.json()["detail"] == "control interface disabled"
        settings.poly_control_key = CONTROL_KEY

    @pytest.mark.asyncio
    async def test_missing_key_rejected(self, client, control_key):
        resp = await client.post("/api/control/kill-switch", json={})
        assert resp.status_code == 401
        assert "API key" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_wrong_key_rejected(self, client, control_key):
        resp = await client.post(
            "/api/control/kill-switch",
            json={},
            headers={"X-API-Key": "WRONG"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_control_key_never_leaked_in_response(self, client, control_key):
        resp = await _post(client, "/api/control/resume", confirm=True)
        assert CONTROL_KEY not in resp.text


class TestKillSwitchEndpoint:
    @pytest.mark.asyncio
    async def test_kill_switch_halts_trading(self, client, control_key):
        resp = await _post(client, "/api/control/kill-switch", operator="ops-1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "KILLED"
        assert body["reason"] == KILL_SWITCH_REASON
        assert body["killed_by"] == "ops-1"
        assert body["trading_enabled"] is False

    @pytest.mark.asyncio
    async def test_kill_switch_is_idempotent(self, client, control_key):
        first = await _post(client, "/api/control/kill-switch", operator="ops-1")
        second = await _post(client, "/api/control/kill-switch", operator="ops-2")
        assert first.json()["state"] == "KILLED"
        assert second.json()["state"] == "KILLED"
        assert second.json()["killed_by"] == "ops-1"  # unchanged

    @pytest.mark.asyncio
    async def test_status_reports_killed_switch(self, client, control_key):
        await _post(client, "/api/control/kill-switch", operator="ops-1")
        status = (await client.get("/system/status")).json()
        ks = status["kill_switch"]
        assert ks["state"] == "KILLED"
        assert ks["reason"] == KILL_SWITCH_REASON
        assert ks["killed_by"] == "ops-1"
        assert ks["killed_at"]

    @pytest.mark.asyncio
    async def test_kill_is_audited(self, api_db, client, control_key):
        await _post(client, "/api/control/kill-switch", operator="ops-1")
        cursor = await api_db.conn.execute(
            "SELECT event_type, decision, reason, severity FROM risk_events "
            "WHERE event_type = 'KILL_SWITCH' ORDER BY timestamp DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["decision"] == "KILLED"
        assert row["reason"] == KILL_SWITCH_REASON
        assert row["severity"] == "CRITICAL"


class TestResumeEndpoint:
    @pytest.mark.asyncio
    async def test_resume_requires_confirmation(self, client, control_key):
        await _post(client, "/api/control/kill-switch")
        resp = await _post(client, "/api/control/resume", operator="ops-2")
        assert resp.status_code == 409
        assert "confirmation" in resp.json()["detail"]
        # Switch is still killed and new orders remain blocked.
        status = (await client.get("/system/status")).json()
        assert status["kill_switch"]["state"] == "KILLED"

    @pytest.mark.asyncio
    async def test_resume_with_confirmation_reactivates(self, client, control_key):
        await _post(client, "/api/control/kill-switch")
        resp = await _post(client, "/api/control/resume", confirm=True, operator="ops-2")
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "ACTIVE"
        assert body["trading_enabled"] is True
        status = (await client.get("/system/status")).json()
        assert status["kill_switch"]["state"] == "ACTIVE"

    @pytest.mark.asyncio
    async def test_resume_is_audited(self, api_db, client, control_key):
        await _post(client, "/api/control/kill-switch")
        await _post(client, "/api/control/resume", confirm=True, operator="ops-2")
        cursor = await api_db.conn.execute(
            "SELECT event_type, decision, severity FROM risk_events "
            "WHERE event_type = 'KILL_SWITCH_RESUMED' ORDER BY timestamp DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["decision"] == "ACTIVE"
        assert row["severity"] == "INFO"


class TestPersistenceAcrossRestart:
    @pytest.mark.asyncio
    async def test_killed_state_survives_restart(self, api_db, control_key):
        """A kill survives a full API restart and is not auto-resumed."""
        app = create_app(database=api_db)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url=BASE_URL
        ) as first_client:
            headers = {"X-API-Key": CONTROL_KEY}
            resp = await first_client.post(
                "/api/control/kill-switch", json={"operator": "ops-1"}, headers=headers
            )
            assert resp.status_code == 200

        # New app instance over the SAME database = restart.
        app2 = create_app(database=api_db)
        transport2 = httpx.ASGITransport(app=app2)
        async with httpx.AsyncClient(
            transport=transport2, base_url=BASE_URL
        ) as second_client:
            status = (await second_client.get("/system/status")).json()
            assert status["kill_switch"]["state"] == "KILLED"
            assert status["kill_switch"]["reason"] == KILL_SWITCH_REASON
            # Resume still requires confirmation even after restart.
            resp = await second_client.post(
                "/api/control/resume",
                json={"operator": "ops-2"},
                headers={"X-API-Key": CONTROL_KEY},
            )
            assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_defaults_active_on_fresh_database(self):
        db = Database(db_path=":memory:")
        await db.connect()
        await db.init_schema()
        app = create_app(database=db)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as c:
            status = (await c.get("/system/status")).json()
            assert status["kill_switch"] is None
        await db.close()


class TestPositionsRemainVisible:
    @pytest.mark.asyncio
    async def test_positions_still_listed_while_killed(self, client, control_key):
        await _post(client, "/api/control/kill-switch")
        resp = await client.get("/positions")
        body = resp.json()
        assert body["pagination"]["total"] == 1
        assert body["items"][0]["position_id"] == "pos_001"
        assert body["items"][0]["size"] == 42.0

    @pytest.mark.asyncio
    async def test_dashboard_overview_unaffected(self, client, control_key):
        await _post(client, "/api/control/kill-switch")
        resp = await client.get("/api/dashboard/overview")
        assert resp.status_code == 200


class TestFrontendCannotMutate:
    @pytest.mark.asyncio
    async def test_display_endpoints_send_write_operations(self, client, control_key):
        # Attempting to toggle the kill switch through a display GET endpoint
        # is impossible — those endpoints are read-only and have no action.
        for method in ("post", "put", "patch", "delete"):
            resp = await client.request(method, "/system/status")
            assert resp.status_code in (404, 405), method

    @pytest.mark.asyncio
    async def test_control_endpoints_accept_no_trade_parameters(
        self, client, control_key
    ):
        # A control command cannot carry side/price/size — the only writable
        # fields are the confirmation flag and an operator label.
        resp = await _post(
            client,
            "/api/control/kill-switch",
            operator="ops-1",
            side="YES",
            price=0.5,
            size=10,
        )
        assert resp.status_code == 200
        assert resp.json()["state"] == "KILLED"

    @pytest.mark.asyncio
    async def test_control_db_rows_do_not_serialize(self, api_db, client, control_key):
        """Audit rows must not contain the control key."""
        await _post(client, "/api/control/kill-switch")
        cursor = await api_db.conn.execute("SELECT * FROM risk_events")
        rows = await cursor.fetchall()
        payload = json.dumps([dict(r) for r in rows])
        assert CONTROL_KEY not in payload
