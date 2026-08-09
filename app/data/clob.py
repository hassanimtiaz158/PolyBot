"""Polymarket CLOB API adapter (order-book and price data).

See https://docs.polymarket.com/api-reference/market-data/get-order-book

Public CLOB endpoints (``/book``, ``/books``, ``/price``) do **not** require
authentication — only order placement needs API keys.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────

CLOB_BASE_URL = "https://clob.polymarket.com"
DEFAULT_TIMEOUT = 10.0  # seconds
MAX_RETRIES = 3
RETRY_BACKOFF = 1.0
# Conservative rate limit — docs suggest ~100 req/min for trading endpoints,
# but public reads are typically less restricted.
REQUEST_DELAY = 0.1  # 100 ms → ~10 req/s


class ClobAdapter:
    """Adapter for the Polymarket CLOB API (read-only public endpoints).

    Provides order-book levels, best bid/ask, and price data.  Does **not**
    place orders or require authentication.

    Usage::

        adapter = ClobAdapter()
        book = await adapter.get_order_book("0xabc...")
        price = await adapter.get_price("0xabc...")
    """

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._last_request_time: float = 0.0

    # ── Public API ──────────────────────────────────────────────────

    async def get_order_book(self, token_id: str) -> dict[str, Any]:
        """Fetch the current order book for a single outcome token.

        Returns the raw ``OrderBookSummary`` dict (bids, asks, market,
        timestamp, hash, etc.) or an empty dict on failure.
        """
        data = await self._request("GET", "/book", params={"token_id": token_id})
        if isinstance(data, dict):
            return data
        logger.warning("Unexpected /book response for token %s: %s", token_id, type(data))
        return {}

    async def get_order_books(self, token_ids: list[str]) -> list[dict[str, Any]]:
        """Fetch order books for multiple tokens in a single request.

        Maximum 500 token IDs per request (enforced by API).
        """
        if not token_ids:
            return []
        payload: list[dict[str, str]] = [{"token_id": tid} for tid in token_ids]
        data = await self._request("POST", "/books", json=payload)
        if isinstance(data, list):
            return data
        logger.warning("Unexpected /books response: %s", type(data))
        return []

    async def get_price(self, token_id: str) -> dict[str, Any]:
        """Fetch the best price (midpoint) for a single outcome token.

        Returns a dict like ``{"price": "0.45", "timestamp": "1234567890"}``
        or an empty dict on failure.
        """
        data = await self._request("GET", "/price", params={"token_id": token_id})
        if isinstance(data, dict):
            return data
        logger.warning("Unexpected /price response for token %s: %s", token_id, type(data))
        return {}

    async def get_recent_trades(self, token_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """Fetch recent trades for a token. (Mimicked; no public endpoint.)"""
        logger.debug("ClobAdapter.get_recent_trades called for %s (limit=%d)", token_id, limit)
        return []

    async def health(self) -> bool:
        """Quick connectivity check — fetch a known-invalid token's book.

        A 404 on a bogus token id is acceptable — it means the API is
        reachable.
        """
        try:
            result = await self._request("GET", "/book", params={"token_id": "0xhealthcheck"})
            return result is not None
        except Exception:
            return False

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── Internal HTTP helpers ───────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json: list[dict[str, str]] | None = None,
    ) -> Any:
        url = f"{CLOB_BASE_URL}{path}"
        client = await self._get_client()

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                await self._rate_limit_delay()
                response = await client.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    timeout=DEFAULT_TIMEOUT,
                )
                self._last_request_time = asyncio.get_event_loop().time()

                if response.status_code == 429:
                    retry_after = _parse_retry_after(response)
                    logger.warning(
                        "Rate limited on %s, retrying after %.1fs (attempt %d/%d)",
                        path,
                        retry_after,
                        attempt,
                        MAX_RETRIES,
                    )
                    await asyncio.sleep(retry_after)
                    continue

                if 500 <= response.status_code < 600:
                    logger.warning(
                        "Server error %d on %s (attempt %d/%d)",
                        response.status_code,
                        path,
                        attempt,
                        MAX_RETRIES,
                    )
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(RETRY_BACKOFF * (2 ** (attempt - 1)))
                        continue
                    return None

                if response.status_code in (400, 404):
                    return None

                response.raise_for_status()
                return response.json()

            except httpx.TimeoutException:
                logger.warning(
                    "Timeout on %s (attempt %d/%d)", path, attempt, MAX_RETRIES
                )
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_BACKOFF * (2 ** (attempt - 1)))
                    continue
                return None

            except httpx.HTTPStatusError as exc:
                logger.error(
                    "HTTP error %s on %s: %s", exc.response.status_code, path, exc
                )
                return None

            except httpx.RequestError as exc:
                logger.warning(
                    "Request failed on %s (attempt %d/%d): %s",
                    path,
                    attempt,
                    MAX_RETRIES,
                    exc,
                )
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_BACKOFF * (2 ** (attempt - 1)))
                    continue
                return None

        return None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"User-Agent": "PolyQuantBot/0.1.0"},
            )
        return self._client

    async def _rate_limit_delay(self) -> None:
        loop = asyncio.get_event_loop()
        now = loop.time()
        elapsed = now - self._last_request_time
        if elapsed < REQUEST_DELAY:
            await asyncio.sleep(REQUEST_DELAY - elapsed)


def _parse_retry_after(response: httpx.Response) -> float:
    val = response.headers.get("Retry-After")
    if val is not None:
        try:
            return float(val)
        except ValueError:
            pass
    return 5.0
