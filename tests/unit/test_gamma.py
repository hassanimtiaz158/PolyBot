"""Tests for the Gamma API adapter."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.data.gamma import GammaAdapter


@pytest.fixture
def adapter() -> GammaAdapter:
    return GammaAdapter()


@pytest.fixture
def mock_client() -> AsyncMock:
    """Return a mock that replaces ``httpx.AsyncClient``."""
    client = AsyncMock(spec=httpx.AsyncClient)
    client.request.return_value = _mock_response(200, [])
    return client


def _mock_response(status: int, json_data: Any) -> AsyncMock:
    resp = AsyncMock(spec=httpx.Response)
    resp.status_code = status
    resp.json.return_value = json_data
    resp.headers = {}
    if status >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"{status} error", request=MagicMock(), response=resp
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


class TestListMarkets:
    async def test_normal_response(self, adapter: GammaAdapter) -> None:
        markets = [{"id": "1", "question": "Q1?"}, {"id": "2", "question": "Q2?"}]
        with patch.object(adapter, "_request", AsyncMock(return_value=markets)):
            result = await adapter.list_markets(closed=False, limit=10)
        assert len(result) == 2
        assert result[0]["id"] == "1"

    async def test_empty_response(self, adapter: GammaAdapter) -> None:
        with patch.object(adapter, "_request", AsyncMock(return_value=[])):
            result = await adapter.list_markets()
        assert result == []

    async def test_none_response(self, adapter: GammaAdapter) -> None:
        with patch.object(adapter, "_request", AsyncMock(return_value=None)):
            result = await adapter.list_markets()
        assert result == []

    async def test_non_list_response(self, adapter: GammaAdapter) -> None:
        with patch.object(adapter, "_request", AsyncMock(return_value="unexpected")):
            result = await adapter.list_markets()
        assert result == []

    async def test_filters_passed_to_request(self, adapter: GammaAdapter) -> None:
        mock = AsyncMock(return_value=[])
        with (
            patch.object(adapter, "_request", mock) as req,
        ):
            await adapter.list_markets(closed=True, tag_id=42, limit=5)
        req.assert_called_once_with(
            "GET", "/markets", params={"closed": True, "tag_id": 42, "limit": 5}
        )


class TestListMarketsKeyset:
    async def test_normal_response(self, adapter: GammaAdapter) -> None:
        raw = {
            "markets": [{"id": "1"}],
            "next_cursor": "cursor123",
        }
        with patch.object(adapter, "_request", AsyncMock(return_value=raw)):
            result = await adapter.list_markets_keyset(tag_id=74)
        assert len(result["markets"]) == 1
        assert result["next_cursor"] == "cursor123"

    async def test_none_response(self, adapter: GammaAdapter) -> None:
        with patch.object(adapter, "_request", AsyncMock(return_value=None)):
            result = await adapter.list_markets_keyset()
        assert result == {"markets": [], "next_cursor": None}


class TestGetMarket:
    async def test_found(self, adapter: GammaAdapter) -> None:
        market = {"id": "703257", "question": "Will X happen?"}
        with patch.object(adapter, "_request", AsyncMock(return_value=market)):
            result = await adapter.get_market("703257")
        assert result is not None
        assert result["question"] == "Will X happen?"

    async def test_not_found(self, adapter: GammaAdapter) -> None:
        with patch.object(adapter, "_request", AsyncMock(return_value=None)):
            result = await adapter.get_market("999999")
        assert result is None


class TestGetMarketBySlug:
    async def test_found(self, adapter: GammaAdapter) -> None:
        market = {"id": "1", "slug": "test-slug"}
        with patch.object(adapter, "_request", AsyncMock(return_value=market)):
            result = await adapter.get_market_by_slug("test-slug")
        assert result is not None
        assert result["slug"] == "test-slug"

    async def test_not_found(self, adapter: GammaAdapter) -> None:
        with patch.object(adapter, "_request", AsyncMock(return_value=None)):
            result = await adapter.get_market_by_slug("nonexistent")
        assert result is None


class TestHealth:
    async def test_healthy(self, adapter: GammaAdapter) -> None:
        with patch.object(adapter, "_request", AsyncMock(return_value=[{"id": "1"}])):
            healthy = await adapter.health()
        assert healthy is True

    async def test_unhealthy(self, adapter: GammaAdapter) -> None:
        with patch.object(adapter, "_request", AsyncMock(return_value=None)):
            healthy = await adapter.health()
        assert healthy is False

    async def test_exception(self, adapter: GammaAdapter) -> None:
        with patch.object(adapter, "_request", AsyncMock(side_effect=RuntimeError("fail"))):
            healthy = await adapter.health()
        assert healthy is False


class TestRequestRetryAndError:
    """Test the _request method's error-handling logic by mocking the client."""

    async def test_rate_limit_retry(self, adapter: GammaAdapter) -> None:
        """429 should retry and eventually succeed."""
        client = AsyncMock(spec=httpx.AsyncClient)
        resp_429 = _mock_response(429, {"error": "rate limit"})
        resp_429.headers = {"Retry-After": "0.01"}
        resp_200 = _mock_response(200, [{"id": "1"}])
        client.request = AsyncMock(side_effect=[resp_429, resp_200])

        adapter._client = client
        result = await adapter._request("GET", "/markets", params={"limit": 1})
        assert result == [{"id": "1"}]
        assert client.request.call_count == 2

    async def test_server_error_retry(self, adapter: GammaAdapter) -> None:
        """5xx should retry and eventually succeed."""
        client = AsyncMock(spec=httpx.AsyncClient)
        resp_500 = _mock_response(500, {"error": "internal"})
        resp_200 = _mock_response(200, [{"id": "1"}])
        client.request = AsyncMock(side_effect=[resp_500, resp_200])

        adapter._client = client
        result = await adapter._request("GET", "/markets")
        assert result == [{"id": "1"}]

    async def test_all_retries_exhausted(self, adapter: GammaAdapter) -> None:
        """After max retries of 5xx, should return None."""
        client = AsyncMock(spec=httpx.AsyncClient)
        resp_500 = _mock_response(500, {"error": "internal"})
        client.request = AsyncMock(return_value=resp_500)

        adapter._client = client
        result = await adapter._request("GET", "/markets")
        assert result is None

    async def test_timeout_retry(self, adapter: GammaAdapter) -> None:
        """Timeout should retry and eventually succeed."""
        client = AsyncMock(spec=httpx.AsyncClient)
        client.request = AsyncMock(
            side_effect=[httpx.TimeoutException("timeout"), _mock_response(200, [])]
        )

        adapter._client = client
        result = await adapter._request("GET", "/markets")
        assert result == []

    async def test_all_timeouts_exhausted(self, adapter: GammaAdapter) -> None:
        """All attempts time out → return None."""
        client = AsyncMock(spec=httpx.AsyncClient)
        client.request = AsyncMock(side_effect=httpx.TimeoutException("timeout"))

        adapter._client = client
        result = await adapter._request("GET", "/markets")
        assert result is None

    async def test_404_returns_none(self, adapter: GammaAdapter) -> None:
        client = AsyncMock(spec=httpx.AsyncClient)
        resp_404 = _mock_response(404, {"error": "not found"})
        client.request = AsyncMock(return_value=resp_404)

        adapter._client = client
        result = await adapter._request("GET", "/markets/999")
        assert result is None

    async def test_400_returns_none(self, adapter: GammaAdapter) -> None:
        client = AsyncMock(spec=httpx.AsyncClient)
        resp_400 = _mock_response(400, {"error": "bad request"})
        client.request = AsyncMock(return_value=resp_400)

        adapter._client = client
        result = await adapter._request("GET", "/markets", params={"invalid": "x"})
        assert result is None

    async def test_connection_error_retry(self, adapter: GammaAdapter) -> None:
        """Connection errors should retry and eventually succeed."""
        client = AsyncMock(spec=httpx.AsyncClient)
        client.request = AsyncMock(
            side_effect=[
                httpx.RequestError("connection failed"),
                _mock_response(200, [{"id": "1"}]),
            ]
        )

        adapter._client = client
        result = await adapter._request("GET", "/markets")
        assert result == [{"id": "1"}]

    async def test_connection_error_all_fail(self, adapter: GammaAdapter) -> None:
        client = AsyncMock(spec=httpx.AsyncClient)
        client.request = AsyncMock(side_effect=httpx.RequestError("connection failed"))

        adapter._client = client
        result = await adapter._request("GET", "/markets")
        assert result is None


class TestClose:
    async def test_close_cleanup(self, adapter: GammaAdapter) -> None:
        client = AsyncMock(spec=httpx.AsyncClient)
        adapter._client = client
        await adapter.close()
        client.aclose.assert_awaited_once()
        assert adapter._client is None
