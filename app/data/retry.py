"""Shared HTTP retry/rate-limit helpers for API adapters."""

from __future__ import annotations

import httpx


def parse_retry_after(response: httpx.Response) -> float:
    """Extract retry-after seconds from a 429 response.

    Falls back to 5 seconds when the header is missing or unparseable.
    """
    val = response.headers.get("Retry-After")
    if val is not None:
        try:
            return float(val)
        except ValueError:
            pass
    return 5.0
