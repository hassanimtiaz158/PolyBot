"""Tests for the CLOB API adapter."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.data.clob import ClobAdapter


@pytest.fixture
def adapter() -> ClobAdapter:
    return ClobAdapter()


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


class TestGetOrderBook:
    async def test_normal_response(self, adapter: ClobAdapter) -> None:
        raw = {
            "market": "0xcond",
            "asset_id": "0xtoken",
            "timestamp": "1712345678",
            "bids": [{"price": "0.45", "size": "100"}],
            "asks": [{"price": "0.46", "size": "150"}],
        }
        with patch.object(adapter, "_request", AsyncMock(return_value=raw)):
            result = await adapter.get_order_book("0xtoken")
        assert result["asset_id"] == "0xtoken"
        assert result["bids"][0]["price"] == "0.45"

    async def test_empty_response(self, adapter: ClobAdapter) -> None:
        with patch.object(adapter, "_request", AsyncMock(return_value={})):
            result = await adapter.get_order_book("0xnonexistent")
        assert result == {}

    async def test_none_response(self, adapter: ClobAdapter) -> None:
        with patch.object(adapter, "_request", AsyncMock(return_value=None)):
            result = await adapter.get_order_book("0xfail")
        assert result == {}


class TestGetOrderBooks:
    async def test_normal_batch(self, adapter: ClobAdapter) -> None:
        raw_list = [
            {"asset_id": "0xa", "bids": [], "asks": []},
            {"asset_id": "0xb", "bids": [], "asks": []},
        ]
        with patch.object(adapter, "_request", AsyncMock(return_value=raw_list)):
            result = await adapter.get_order_books(["0xa", "0xb"])
        assert len(result) == 2

    async def test_empty_token_list(self, adapter: ClobAdapter) -> None:
        result = await adapter.get_order_books([])
        assert result == []

    async def test_non_list_response(self, adapter: ClobAdapter) -> None:
        with patch.object(adapter, "_request", AsyncMock(return_value="unexpected")):
            result = await adapter.get_order_books(["0xa"])
        assert result == []


class TestGetPrice:
    async def test_normal_response(self, adapter: ClobAdapter) -> None:
        raw = {"price": "0.45", "timestamp": "1712345678"}
        with patch.object(adapter, "_request", AsyncMock(return_value=raw)):
            result = await adapter.get_price("0xtoken")
        assert result["price"] == "0.45"

    async def test_empty_response(self, adapter: ClobAdapter) -> None:
        with patch.object(adapter, "_request", AsyncMock(return_value={})):
            result = await adapter.get_price("0xmissing")
        assert result == {}

    async def test_none_response(self, adapter: ClobAdapter) -> None:
        with patch.object(adapter, "_request", AsyncMock(return_value=None)):
            result = await adapter.get_price("0xfail")
        assert result == {}


class TestHealth:
    async def test_healthy(self, adapter: ClobAdapter) -> None:
        with patch.object(adapter, "_request", AsyncMock(return_value={})):
            healthy = await adapter.health()
        assert healthy is True

    async def test_unhealthy(self, adapter: ClobAdapter) -> None:
        with patch.object(adapter, "_request", AsyncMock(side_effect=RuntimeError("fail"))):
            healthy = await adapter.health()
        assert healthy is False

    async def test_exception(self, adapter: ClobAdapter) -> None:
        with patch.object(adapter, "_request", AsyncMock(side_effect=RuntimeError("fail"))):
            healthy = await adapter.health()
        assert healthy is False


class TestRequestRetryAndError:
    async def test_rate_limit_retry(self, adapter: ClobAdapter) -> None:
        client = AsyncMock(spec=httpx.AsyncClient)
        resp_429 = _mock_response(429, {"error": "rate limit"})
        resp_429.headers = {"Retry-After": "0.01"}
        resp_200 = _mock_response(200, {"bids": [], "asks": []})
        client.request = AsyncMock(side_effect=[resp_429, resp_200])

        adapter._client = client
        result = await adapter._request("GET", "/book", params={"token_id": "0x1"})
        assert isinstance(result, dict)

    async def test_server_error_retry(self, adapter: ClobAdapter) -> None:
        client = AsyncMock(spec=httpx.AsyncClient)
        resp_500 = _mock_response(500, {})
        resp_200 = _mock_response(200, {"bids": [], "asks": []})
        client.request = AsyncMock(side_effect=[resp_500, resp_200])

        adapter._client = client
        result = await adapter._request("GET", "/book", params={"token_id": "0x1"})
        assert isinstance(result, dict)

    async def test_all_retries_exhausted(self, adapter: ClobAdapter) -> None:
        client = AsyncMock(spec=httpx.AsyncClient)
        client.request = AsyncMock(return_value=_mock_response(500, {}))

        adapter._client = client
        result = await adapter._request("GET", "/book", params={"token_id": "0x1"})
        assert result is None

    async def test_timeout_retry(self, adapter: ClobAdapter) -> None:
        client = AsyncMock(spec=httpx.AsyncClient)
        client.request = AsyncMock(
            side_effect=[httpx.TimeoutException("timeout"), _mock_response(200, {})]
        )
        adapter._client = client
        result = await adapter._request("GET", "/book", params={"token_id": "0x1"})
        assert isinstance(result, dict)

    async def test_404_returns_none(self, adapter: ClobAdapter) -> None:
        client = AsyncMock(spec=httpx.AsyncClient)
        client.request = AsyncMock(return_value=_mock_response(404, {}))

        adapter._client = client
        result = await adapter._request("GET", "/book", params={"token_id": "0xnone"})
        assert result is None

    async def test_connection_error_retry(self, adapter: ClobAdapter) -> None:
        client = AsyncMock(spec=httpx.AsyncClient)
        client.request = AsyncMock(
            side_effect=[httpx.RequestError("fail"), _mock_response(200, {})]
        )
        adapter._client = client
        result = await adapter._request("GET", "/book", params={"token_id": "0x1"})
        assert isinstance(result, dict)


class TestClose:
    async def test_close_cleanup(self, adapter: ClobAdapter) -> None:
        client = AsyncMock(spec=httpx.AsyncClient)
        adapter._client = client
        await adapter.close()
        client.aclose.assert_awaited_once()
        assert adapter._client is None
