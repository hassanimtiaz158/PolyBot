"""Mock-only tests for the Polymarket CLOB V2 execution adapter.

No real orders are ever submitted.  All external calls are mocked.

Order signing/submission is delegated to Polymarket's official
``py-clob-client`` SDK (``ClobClient``), so tests that exercise a
submission/cancel/status/reconcile round-trip mock ``adapter._client``
(a ``ClobClient``) directly rather than an httpx transport.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from py_clob_client.clob_types import ApiCreds

from app.execution.polymarket import (
    AccountState,
    ClobCredentials,
    PolymarketExecution,
    SafetyViolation,
    _check_account_reconciled,
    _check_api_health,
    _check_circuit_breaker,
    _check_data_freshness,
    _check_idempotency,
    _check_kill_switch,
    _check_live_trading_enabled,
    _check_position_limits,
)
from app.monitoring.health import health_status
from app.risk.circuit_breaker import CircuitBreaker

# -- Fixtures -----------------------------------------------------------


@pytest.fixture
def creds() -> ClobCredentials:
    return ClobCredentials(
        api_key="test-api-key",
        api_secret="dGVzdC1zZWNyZXQ=",  # base64("test-secret")
        api_passphrase="test-passphrase",
        address="0x1234567890abcdef1234567890abcdef12345678",
    )


@pytest.fixture
def healthy_health():
    """Set all health checks to healthy, restore after test."""
    health_status.set_healthy("data_freshness")
    health_status.set_healthy("api")
    yield
    health_status.set_healthy("data_freshness")
    health_status.set_healthy("api")


@pytest.fixture
def breaker_ok() -> CircuitBreaker:
    return CircuitBreaker(persist=False)


@pytest.fixture
def breaker_halted() -> CircuitBreaker:
    b = CircuitBreaker(persist=False)
    b._state = "HALTED"
    b._reasons = ["DAILY_LOSS"]
    return b


@pytest.fixture(autouse=True)
def _clean_pending():
    """Ensure no test pollution from idempotency tracking."""
    yield


@pytest.fixture(autouse=True)
def _restore_env():
    """Restore POLY_KILL_SWITCH after test."""
    original = os.environ.get("POLY_KILL_SWITCH")
    yield
    if original is None:
        os.environ.pop("POLY_KILL_SWITCH", None)
    else:
        os.environ["POLY_KILL_SWITCH"] = original


@pytest.fixture
def portfolio_mock():
    mock = MagicMock()
    mock.equity = 10000.0
    mock.total_exposure.return_value = 0.0
    mock.market_exposure.return_value = 0.0
    return mock


# -- Safety gate tests --------------------------------------------------


class TestSafetyGates:
    @patch("app.execution.polymarket.settings")
    def test_live_trading_disabled_blocks(self, mock_settings):
        mock_settings.live_trading_enabled = False
        with pytest.raises(SafetyViolation, match="Live trading is disabled"):
            _check_live_trading_enabled()

    @patch("app.execution.polymarket.settings")
    def test_live_trading_enabled_passes(self, mock_settings):
        mock_settings.live_trading_enabled = True
        _check_live_trading_enabled()

    def test_kill_switch_active_blocks(self):
        os.environ["POLY_KILL_SWITCH"] = "1"
        with pytest.raises(SafetyViolation, match="Kill switch"):
            _check_kill_switch()

    def test_kill_switch_false_passes(self):
        os.environ["POLY_KILL_SWITCH"] = "false"
        _check_kill_switch()

    def test_kill_switch_empty_passes(self):
        os.environ.pop("POLY_KILL_SWITCH", None)
        _check_kill_switch()

    def test_circuit_breaker_halted_blocks(self, breaker_halted):
        with pytest.raises(SafetyViolation, match="HALTED"):
            _check_circuit_breaker(breaker_halted)

    def test_circuit_breaker_normal_passes(self, breaker_ok):
        _check_circuit_breaker(breaker_ok)

    def test_data_freshness_unhealthy_blocks(self):
        health_status.set_unhealthy("data_freshness")
        with pytest.raises(SafetyViolation, match="Data freshness"):
            _check_data_freshness()

    def test_data_freshness_healthy_passes(self):
        health_status.set_healthy("data_freshness")
        _check_data_freshness()

    def test_api_health_unhealthy_blocks(self):
        health_status.set_unhealthy("api")
        with pytest.raises(SafetyViolation, match="API health"):
            _check_api_health()

    def test_api_health_healthy_passes(self):
        health_status.set_healthy("api")
        _check_api_health()

    def test_account_not_reconciled_blocks(self):
        with pytest.raises(SafetyViolation, match="not reconciled"):
            _check_account_reconciled(None)

    def test_account_invalid_blocks(self):
        acct = AccountState(address="0xabc", api_key="k", is_valid=False)
        with pytest.raises(SafetyViolation, match="not reconciled"):
            _check_account_reconciled(acct)

    def test_account_valid_passes(self):
        acct = AccountState(address="0xabc", api_key="k", is_valid=True)
        _check_account_reconciled(acct)

    def test_position_limits_exceeded(self, portfolio_mock):
        portfolio_mock.equity = 1000.0
        with pytest.raises(SafetyViolation, match="exceeds max position"):
            _check_position_limits(portfolio_mock, "m1", 100, 0.5)

    def test_position_limits_total_exposure(self, portfolio_mock):
        portfolio_mock.equity = 1000.0
        portfolio_mock.total_exposure.return_value = 55.0
        with pytest.raises(SafetyViolation, match="Total exposure"):
            _check_position_limits(portfolio_mock, "m1", 10, 0.5)

    def test_position_limits_market_exposure(self, portfolio_mock):
        portfolio_mock.equity = 1000.0
        portfolio_mock.market_exposure.return_value = 25.0
        with pytest.raises(SafetyViolation, match="Market exposure"):
            _check_position_limits(portfolio_mock, "m1", 10, 0.5)

    def test_duplicate_order_blocked(self):
        pending = {"ord1": {"status": "LIVE"}}
        with pytest.raises(SafetyViolation, match="Duplicate order"):
            _check_idempotency("ord1", pending)

    def test_new_order_passes(self):
        _check_idempotency("ord2", {"ord1": {"status": "LIVE"}})

    def test_no_portfolio_skips_limits(self):
        _check_position_limits(None, "m1", 1000, 0.99)


# -- Adapter initialization tests ---------------------------------------


class TestInit:
    def test_default_state(self, creds):
        adapter = PolymarketExecution(credentials=creds)
        assert adapter._credentials is creds
        assert adapter._account_state is None
        assert not adapter._reconciled
        assert adapter._pending_orders == {}

    # Well-known Hardhat/Anvil test account #0 private key -- public,
    # never funded, used only to exercise address derivation in tests.
    _TEST_PRIVATE_KEY = (
        "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
    )

    def test_loads_credentials_from_settings(self, creds):
        with patch("app.execution.polymarket.settings") as mock_settings:
            mock_settings.poly_private_key = self._TEST_PRIVATE_KEY
            mock_settings.poly_funder_address = None
            mock_settings.poly_api_key = creds.api_key
            mock_settings.poly_secret = creds.api_secret
            mock_settings.poly_passphrase = creds.api_passphrase
            mock_settings.poly_signature_type = 0
            mock_settings.live_trading_enabled = False
            adapter = PolymarketExecution()
            assert adapter._credentials is not None
            assert adapter._client is not None

    def test_missing_private_key_returns_none(self):
        with patch("app.execution.polymarket.settings") as mock_settings:
            mock_settings.poly_private_key = None
            mock_settings.poly_funder_address = None
            mock_settings.poly_api_key = None
            mock_settings.poly_secret = None
            mock_settings.poly_passphrase = None
            result = PolymarketExecution._load_credentials()
            assert result is None

    def test_missing_credentials_returns_none(self):
        with patch("app.execution.polymarket.settings") as mock_settings:
            mock_settings.poly_private_key = None
            mock_settings.poly_api_key = None
            mock_settings.poly_secret = None
            mock_settings.poly_passphrase = None
            result = PolymarketExecution._load_credentials()
            assert result is None


# -- Submit tests --------------------------------------------------------


class TestSubmit:
    @pytest.mark.asyncio
    async def test_missing_order_id_rejected(self, creds, healthy_health):
        adapter = PolymarketExecution(credentials=creds)
        result = await adapter.submit({
            "order_id": "",
            "market_id": "m1",
            "side": "YES",
            "size": 10,
            "price": 0.5,
            "token_id": "tok1",
        })
        assert result["status"] == "REJECTED"
        assert "Missing order_id" in result["error"]

    @pytest.mark.asyncio
    async def test_invalid_side_rejected(self, creds, healthy_health):
        adapter = PolymarketExecution(credentials=creds)
        result = await adapter.submit({
            "order_id": "ord1",
            "market_id": "m1",
            "side": "MAYBE",
            "size": 10,
            "price": 0.5,
            "token_id": "tok1",
        })
        assert result["status"] == "REJECTED"
        assert "Invalid side" in result["error"]

    @pytest.mark.asyncio
    async def test_zero_size_rejected(self, creds, healthy_health):
        adapter = PolymarketExecution(credentials=creds)
        result = await adapter.submit({
            "order_id": "ord1",
            "market_id": "m1",
            "side": "YES",
            "size": 0,
            "price": 0.5,
            "token_id": "tok1",
        })
        assert result["status"] == "REJECTED"
        assert "Invalid size" in result["error"]

    @pytest.mark.asyncio
    async def test_price_out_of_range_rejected(self, creds, healthy_health):
        adapter = PolymarketExecution(credentials=creds)
        result = await adapter.submit({
            "order_id": "ord1",
            "market_id": "m1",
            "side": "YES",
            "size": 10,
            "price": 1.5,
            "token_id": "tok1",
        })
        assert result["status"] == "REJECTED"
        assert "Invalid price" in result["error"]

    @pytest.mark.asyncio
    async def test_missing_token_id_rejected(self, creds, healthy_health):
        adapter = PolymarketExecution(credentials=creds)
        result = await adapter.submit({
            "order_id": "ord1",
            "market_id": "m1",
            "side": "YES",
            "size": 10,
            "price": 0.5,
            "token_id": "",
        })
        assert result["status"] == "REJECTED"
        assert "Missing token_id" in result["error"]

    @pytest.mark.asyncio
    async def test_kill_switch_blocks(self, creds, healthy_health):
        os.environ["POLY_KILL_SWITCH"] = "1"
        adapter = PolymarketExecution(credentials=creds)
        adapter._account_state = AccountState(
            address=creds.address, api_key=creds.api_key, is_valid=True
        )
        with patch("app.execution.polymarket.settings") as mock_settings:
            mock_settings.live_trading_enabled = True
            result = await adapter.submit({
                "order_id": "ord1",
                "market_id": "m1",
                "side": "YES",
                "size": 10,
                "price": 0.5,
                "token_id": "tok1",
            })
        assert result["status"] == "REJECTED"
        assert "Kill switch" in result["error"]

    @pytest.mark.asyncio
    async def test_circuit_breaker_halted_blocks(self, creds, healthy_health, breaker_halted):
        adapter = PolymarketExecution(credentials=creds, breaker=breaker_halted)
        adapter._account_state = AccountState(
            address=creds.address, api_key=creds.api_key, is_valid=True
        )
        with patch("app.execution.polymarket.settings") as mock_settings:
            mock_settings.live_trading_enabled = True
            result = await adapter.submit({
                "order_id": "ord1",
                "market_id": "m1",
                "side": "YES",
                "size": 10,
                "price": 0.5,
                "token_id": "tok1",
            })
        assert result["status"] == "REJECTED"
        assert "HALTED" in result["error"]

    @pytest.mark.asyncio
    async def test_data_stale_blocks(self, creds, breaker_ok):
        health_status.set_unhealthy("data_freshness")
        adapter = PolymarketExecution(credentials=creds, breaker=breaker_ok)
        adapter._account_state = AccountState(
            address=creds.address, api_key=creds.api_key, is_valid=True
        )
        with patch("app.execution.polymarket.settings") as mock_settings:
            mock_settings.live_trading_enabled = True
            result = await adapter.submit({
                "order_id": "ord1",
                "market_id": "m1",
                "side": "YES",
                "size": 10,
                "price": 0.5,
                "token_id": "tok1",
            })
        assert result["status"] == "REJECTED"
        assert "Data freshness" in result["error"]

    @pytest.mark.asyncio
    async def test_account_not_reconciled_blocks(self, creds, healthy_health, breaker_ok):
        adapter = PolymarketExecution(credentials=creds, breaker=breaker_ok)
        with patch("app.execution.polymarket.settings") as mock_settings:
            mock_settings.live_trading_enabled = True
            result = await adapter.submit({
                "order_id": "ord1",
                "market_id": "m1",
                "side": "YES",
                "size": 10,
                "price": 0.5,
                "token_id": "tok1",
            })
        assert result["status"] == "REJECTED"
        assert "not reconciled" in result["error"]

    @pytest.mark.asyncio
    async def test_live_trading_disabled_blocks(self, creds, healthy_health, breaker_ok):
        with patch("app.execution.polymarket.settings") as mock_settings:
            mock_settings.live_trading_enabled = False
            adapter = PolymarketExecution(credentials=creds, breaker=breaker_ok)
            result = await adapter.submit({
                "order_id": "ord1",
                "market_id": "m1",
                "side": "YES",
                "size": 10,
                "price": 0.5,
                "token_id": "tok1",
            })
            assert result["status"] == "REJECTED"
            assert "Live trading is disabled" in result["error"]

    @pytest.mark.asyncio
    async def test_no_credentials_blocks(self, healthy_health, breaker_ok):
        adapter = PolymarketExecution(credentials=None, breaker=breaker_ok)
        adapter._account_state = AccountState(
            address="", api_key="", is_valid=True
        )
        with patch("app.execution.polymarket.settings") as mock_settings:
            mock_settings.live_trading_enabled = True
            result = await adapter.submit({
                "order_id": "ord1",
                "market_id": "m1",
                "side": "YES",
                "size": 10,
                "price": 0.5,
                "token_id": "tok1",
            })
            assert result["status"] == "REJECTED"
            assert "credentials" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_duplicate_order_blocked(self, creds, healthy_health, breaker_ok):
        adapter = PolymarketExecution(credentials=creds, breaker=breaker_ok)
        adapter._account_state = AccountState(
            address=creds.address, api_key=creds.api_key, is_valid=True
        )
        adapter._pending_orders["ord1"] = {"status": "LIVE"}

        with patch("app.execution.polymarket.settings") as mock_settings:
            mock_settings.live_trading_enabled = True
            result = await adapter.submit({
                "order_id": "ord1",
                "market_id": "m1",
                "side": "YES",
                "size": 10,
                "price": 0.5,
                "token_id": "tok1",
            })
            assert result["status"] == "REJECTED"
            assert "Duplicate" in result["error"]

    @pytest.mark.asyncio
    async def test_successful_submission(self, creds, healthy_health, breaker_ok):
        adapter = PolymarketExecution(credentials=creds, breaker=breaker_ok)
        adapter._account_state = AccountState(
            address=creds.address, api_key=creds.api_key, is_valid=True
        )

        mock_client = MagicMock()
        mock_client.create_order.return_value = MagicMock()  # signed order
        mock_client.post_order.return_value = {
            "success": True,
            "orderID": "0xabc123",
            "status": "matched",
            "makingAmount": "10000000",
            "takingAmount": "5000000",
            "transactionsHashes": ["0xtx1"],
            "tradeIDs": ["trade1"],
            "errorMsg": "",
        }
        adapter._client = mock_client

        with patch("app.execution.polymarket.settings") as mock_settings:
            mock_settings.live_trading_enabled = True
            mock_settings.max_position_pct = 0.01
            mock_settings.max_total_exposure_pct = 0.05
            mock_settings.max_market_exposure_pct = 0.02

            result = await adapter.submit({
                "order_id": "ord1",
                "market_id": "m1",
                "side": "YES",
                "size": 10,
                "price": 0.5,
                "token_id": "tok1",
            })

        assert result["status"] == "FILLED"
        assert result["order_id"] == "0xabc123"
        assert result["filled_size"] == 10.0
        assert result["error"] is None
        mock_client.create_order.assert_called_once()
        mock_client.post_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_http_error_rejected(self, creds, healthy_health, breaker_ok):
        adapter = PolymarketExecution(credentials=creds, breaker=breaker_ok)
        adapter._account_state = AccountState(
            address=creds.address, api_key=creds.api_key, is_valid=True
        )

        mock_client = MagicMock()
        mock_client.create_order.return_value = MagicMock()
        mock_client.post_order.return_value = {
            "success": False,
            "orderID": "",
            "status": "failed",
            "errorMsg": "Insufficient balance",
        }
        adapter._client = mock_client

        with patch("app.execution.polymarket.settings") as mock_settings:
            mock_settings.live_trading_enabled = True
            mock_settings.max_position_pct = 0.1
            mock_settings.max_total_exposure_pct = 0.5
            mock_settings.max_market_exposure_pct = 0.2

            result = await adapter.submit({
                "order_id": "ord1",
                "market_id": "m1",
                "side": "YES",
                "size": 10,
                "price": 0.5,
                "token_id": "tok1",
            })

        assert result["status"] == "REJECTED"
        assert "Insufficient balance" in result["error"]


# -- Cancel tests --------------------------------------------------------


class TestCancel:
    @pytest.mark.asyncio
    async def test_cancel_empty_id_returns_false(self, creds):
        adapter = PolymarketExecution(credentials=creds)
        assert await adapter.cancel("") is False

    @pytest.mark.asyncio
    async def test_cancel_kill_switch_blocks(self, creds):
        os.environ["POLY_KILL_SWITCH"] = "1"
        adapter = PolymarketExecution(credentials=creds)
        assert await adapter.cancel("ord1") is False

    @pytest.mark.asyncio
    async def test_cancel_no_credentials_returns_false(self):
        adapter = PolymarketExecution(credentials=None)
        assert await adapter.cancel("ord1") is False

    @pytest.mark.asyncio
    async def test_cancel_success(self, creds):
        adapter = PolymarketExecution(credentials=creds)
        mock_client = MagicMock()
        mock_client.cancel.return_value = {}
        adapter._client = mock_client

        with patch("app.execution.polymarket.settings") as mock_settings:
            mock_settings.live_trading_enabled = True
            result = await adapter.cancel("ord1")

        assert result is True
        mock_client.cancel.assert_called_once_with("ord1")

    @pytest.mark.asyncio
    async def test_cancel_updates_pending(self, creds):
        adapter = PolymarketExecution(credentials=creds)
        adapter._pending_orders["ord1"] = {"status": "LIVE"}
        mock_client = MagicMock()
        mock_client.cancel.return_value = {}
        adapter._client = mock_client

        with patch("app.execution.polymarket.settings") as mock_settings:
            mock_settings.live_trading_enabled = True
            await adapter.cancel("ord1")

        assert adapter._pending_orders["ord1"]["status"] == "CANCELLED"


# -- Status tests --------------------------------------------------------


class TestStatus:
    @pytest.mark.asyncio
    async def test_status_empty_id_returns_not_found(self, creds):
        adapter = PolymarketExecution(credentials=creds)
        result = await adapter.status("")
        assert result["status"] == "NOT_FOUND"

    @pytest.mark.asyncio
    async def test_status_returns_pending(self, creds):
        adapter = PolymarketExecution(credentials=creds)
        adapter._pending_orders["ord1"] = {"status": "LIVE"}
        result = await adapter.status("ord1")
        assert result["status"] == "LIVE"

    @pytest.mark.asyncio
    async def test_status_no_credentials_returns_unknown(self):
        adapter = PolymarketExecution(credentials=None)
        result = await adapter.status("ord1")
        assert result["status"] == "UNKNOWN"


# -- Reconciliation tests -----------------------------------------------


class TestReconciliation:
    @pytest.mark.asyncio
    async def test_reconcile_no_credentials_fails(self):
        adapter = PolymarketExecution(credentials=None)
        result = await adapter.reconcile()
        assert result.success is False
        assert "not configured" in result.error

    @pytest.mark.asyncio
    async def test_reconcile_success(self, creds):
        adapter = PolymarketExecution(credentials=creds)

        mock_client = MagicMock()
        mock_client.creds = ApiCreds(
            api_key=creds.api_key,
            api_secret=creds.api_secret,
            api_passphrase=creds.api_passphrase,
        )
        mock_client.get_orders.return_value = [
            {"orderID": "o1", "status": "live"},
            {"orderID": "o2", "status": "matched"},
            {"orderID": "o3", "status": "cancelled"},
        ]
        adapter._client = mock_client

        result = await adapter.reconcile()

        assert result.success is True
        assert result.orders_found == 3
        assert adapter._account_state is not None
        assert adapter._account_state.is_valid is True
        assert len(adapter._account_state.open_order_ids) == 2
        assert adapter._reconciled is True

    @pytest.mark.asyncio
    async def test_reconcile_http_error_fails(self, creds):
        adapter = PolymarketExecution(credentials=creds)

        mock_client = MagicMock()
        mock_client.creds = ApiCreds(
            api_key=creds.api_key,
            api_secret=creds.api_secret,
            api_passphrase=creds.api_passphrase,
        )
        mock_client.get_orders.side_effect = Exception("Connection refused")
        adapter._client = mock_client

        result = await adapter.reconcile()

        assert result.success is False
        assert adapter._account_state.is_valid is False


# -- Full safety gate integration tests ---------------------------------


class TestFullSafetyGates:
    @pytest.mark.asyncio
    async def test_all_gates_pass_for_valid_order(
        self, creds, healthy_health, breaker_ok, portfolio_mock
    ):
        adapter = PolymarketExecution(
            credentials=creds, breaker=breaker_ok, portfolio=portfolio_mock
        )
        adapter._account_state = AccountState(
            address=creds.address, api_key=creds.api_key, is_valid=True
        )

        mock_client = MagicMock()
        mock_client.create_order.return_value = MagicMock()
        mock_client.post_order.return_value = {
            "success": True,
            "orderID": "0xorder1",
            "status": "live",
            "makingAmount": "10000000",
            "takingAmount": "5000000",
            "errorMsg": "",
        }
        adapter._client = mock_client

        with patch("app.execution.polymarket.settings") as mock_settings:
            mock_settings.live_trading_enabled = True
            mock_settings.max_position_pct = 0.1
            mock_settings.max_total_exposure_pct = 0.5
            mock_settings.max_market_exposure_pct = 0.2
            result = await adapter.submit({
                "order_id": "ord1",
                "market_id": "m1",
                "side": "YES",
                "size": 10,
                "price": 0.5,
                "token_id": "tok1",
            })

        assert result["status"] == "LIVE"
        assert result["error"] is None
        assert "0xorder1" in adapter._pending_orders

    @pytest.mark.asyncio
    async def test_position_limits_enforced_at_submit(self, creds, healthy_health, breaker_ok):
        portfolio = MagicMock()
        portfolio.equity = 100.0
        portfolio.total_exposure.return_value = 0.0
        portfolio.market_exposure.return_value = 0.0

        adapter = PolymarketExecution(
            credentials=creds, breaker=breaker_ok, portfolio=portfolio
        )
        adapter._account_state = AccountState(
            address=creds.address, api_key=creds.api_key, is_valid=True
        )

        with patch("app.execution.polymarket.settings") as mock_settings:
            mock_settings.live_trading_enabled = True
            mock_settings.max_position_pct = 0.01
            mock_settings.max_total_exposure_pct = 0.05
            mock_settings.max_market_exposure_pct = 0.02

            result = await adapter.submit({
                "order_id": "ord1",
                "market_id": "m1",
                "side": "YES",
                "size": 100,
                "price": 0.5,
                "token_id": "tok1",
            })

        assert result["status"] == "REJECTED"
        assert "exceeds max position" in result["error"]
