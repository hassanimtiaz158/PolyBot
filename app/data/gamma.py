"""Polymarket Gamma API adapter (read-only market metadata).

See https://docs.polymarket.com/api-reference/introduction
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.data.retry import parse_retry_after

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────

GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
DEFAULT_TIMEOUT = 15.0  # seconds
MAX_RETRIES = 3
RETRY_BACKOFF = 1.0  # seconds (doubled each attempt)
# Rate limits from docs: /markets is 300 req / 10s, general 4000 / 10s
REQUEST_DELAY = 0.05  # 50 ms between requests (≈ 20/sec, well under limits)


class GammaAdapter:
    """Adapter for the read-only Polymarket Gamma API.

    No authentication required. Provides market discovery and metadata.

    Usage::

        adapter = GammaAdapter()
        markets = await adapter.list_markets(closed=False, limit=10)
        market = await adapter.get_market("703257")
    """

    def __init__(self) -> None:
        self._base_url = GAMMA_BASE_URL
        self._client: httpx.AsyncClient | None = None
        self._last_request_time: float = 0.0

    # ── Public API ──────────────────────────────────────────────────

    async def list_markets(self, **filters: Any) -> list[dict[str, Any]]:
        """Fetch a list of markets matching the given filters.

        Available filters: ``closed``, ``limit``, ``offset``, ``tag_id``,
        ``active``, ``liquidity_num_min``, ``volume_num_min``,
        ``start_date_min``, ``end_date_min``, ``cyom``, etc.

        Returns raw JSON-decoded market dicts (one per market).
        """
        params = {k: v for k, v in filters.items() if v is not None}
        data = await self._request("GET", "/markets", params=params)
        if data is None:
            return []
        if isinstance(data, list):
            return data
        logger.warning("Unexpected /markets response type: %s", type(data))
        return []

    async def list_markets_keyset(self, **filters: Any) -> dict[str, Any]:
        """Fetch markets using keyset (cursor) pagination.

        Returns ``{"markets": [...], "next_cursor": str | None}``.
        Pass ``after_cursor`` from a previous response as ``after_cursor``
        filter to get the next page.
        """
        params = {k: v for k, v in filters.items() if v is not None}
        data = await self._request("GET", "/markets/keyset", params=params)
        if data is None:
            return {"markets": [], "next_cursor": None}
        if isinstance(data, dict):
            return {
                "markets": data.get("markets", []),
                "next_cursor": data.get("next_cursor"),
            }
        logger.warning("Unexpected /markets/keyset response type: %s", type(data))
        return {"markets": [], "next_cursor": None}

    async def get_market(self, market_id: str) -> dict[str, Any] | None:
        """Fetch a single market by its numeric ID or condition ID.

        Returns the raw market dict, or ``None`` if not found.
        """
        data = await self._request("GET", f"/markets/{market_id}")
        if data is None:
            return None
        if isinstance(data, dict):
            return data
        logger.warning("Unexpected /markets/{id} response type: %s", type(data))
        return None

    async def get_market_by_slug(self, slug: str) -> dict[str, Any] | None:
        """Fetch a single market by its URL slug."""
        data = await self._request("GET", f"/markets/slug/{slug}")
        if data is None:
            return None
        if isinstance(data, dict):
            return data
        logger.warning("Unexpected /markets/slug/{slug} response type: %s", type(data))
        return None

    async def health(self) -> bool:
        """Quick connectivity check — hit the Gamma API root."""
        try:
            result = await self._request("GET", "/markets", params={"limit": 1})
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
    ) -> Any:
        """Make an HTTP request with retry, backoff, and rate-limit respect."""
        url = f"{self._base_url}{path}"
        client = await self._get_client()

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                await self._rate_limit_delay()
                response = await client.request(
                    method,
                    url,
                    params=params,
                    timeout=DEFAULT_TIMEOUT,
                )
                self._last_request_time = asyncio.get_running_loop().time()

                if response.status_code == 429:
                    retry_after = parse_retry_after(response)
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
                if exc.response.status_code == 404:
                    return None
                logger.error("HTTP error %s on %s: %s", exc.response.status_code, path, exc)
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
        """Ensure we don't exceed a polite request rate."""
        now = asyncio.get_running_loop().time()
        elapsed = now - self._last_request_time
        if elapsed < REQUEST_DELAY:
            await asyncio.sleep(REQUEST_DELAY - elapsed)
