"""Normalises external API data into the internal storage schema.

Each ``normalize_*`` method accepts a raw JSON-decoded dict from either the
Gamma or CLOB Polymarket API and returns a dict whose keys match the fields
of the corresponding model in ``app.storage.models``.
"""

from __future__ import annotations

import logging
from typing import Any

from app.storage.models import Market, MarketSnapshot

logger = logging.getLogger(__name__)


class DataNormalizer:
    """Converts raw Polymarket API responses into standardised internal dicts."""

    # ── Market metadata (Gamma API) ─────────────────────────────────

    @staticmethod
    def normalize_market(raw: dict[str, Any]) -> dict[str, Any]:
        """Convert a raw Gamma API market dict into a normalised Market dict.

        Expected raw keys (camelCase, per Polymarket Gamma API):
            id, question, conditionId, status, endDate, liquidity,
            volumeNum, startDate, clobTokenIds, negRisk, ...
        """
        end_date = raw.get("endDate") or raw.get("end_date")
        start_date = raw.get("startDate") or raw.get("start_date")
        return {
            "market_id": str(raw.get("id", "")),
            "question": raw.get("question", "") or "",
            "condition_id": raw.get("conditionId"),
            "status": raw.get("status"),
            "resolution_time": end_date,
            "liquidity": _to_float(raw.get("liquidityNum") or raw.get("liquidity")),
            "created_at": start_date,
            "updated_at": raw.get("updatedAt") or raw.get("updated_at"),
        }

    @staticmethod
    def normalize_markets(raw_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Batch-normalise a list of Gamma market dicts."""
        return [DataNormalizer.normalize_market(m) for m in raw_list]

    # ── Market snapshot (CLOB order-book API) ───────────────────────

    @staticmethod
    def normalize_snapshot(market_id: str, raw: dict[str, Any]) -> dict[str, Any]:
        """Convert raw CLOB order-book response into a normalised snapshot dict.

        Parameters
        ----------
        market_id:
            The unique market identifier to attach to the snapshot.
        raw:
            Raw JSON dict from ``/book`` or ``/books`` CLOB endpoint.
        """
        bids = raw.get("bids") or []
        asks = raw.get("asks") or []

        best_bid = _best_level(bids, side="bid")
        best_ask = _best_level(asks, side="ask")

        bid_depth = _total_size(bids)
        ask_depth = _total_size(asks)

        timestamp_raw = raw.get("timestamp")
        timestamp_str = _normalize_timestamp(timestamp_raw) if timestamp_raw else None

        midpoint: float | None = None
        spread: float | None = None
        if best_bid is not None and best_ask is not None:
            midpoint = (best_bid + best_ask) / 2.0
            spread = best_ask - best_bid

        volume = _to_float(raw.get("volume")) if raw.get("volume") else None

        return {
            "market_id": market_id,
            "timestamp": timestamp_str,
            "bid": best_bid,
            "ask": best_ask,
            "midpoint": midpoint,
            "spread": spread,
            "bid_depth": bid_depth,
            "ask_depth": ask_depth,
            "volume": volume,
            "time_to_resolution": None,
        }

    @staticmethod
    def normalize_snapshots(
        market_id: str, raw_list: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Batch-normalise a list of CLOB order-book responses (rare, single-market)."""
        return [DataNormalizer.normalize_snapshot(market_id, r) for r in raw_list]

    # ── Extras ──────────────────────────────────────────────────────

    @staticmethod
    def extract_token_ids(raw: dict[str, Any]) -> list[str]:
        """Extract CLOB token IDs from a Gamma market response.

        The ``clobTokenIds`` field is a JSON-encoded list of token address
        strings.
        """
        raw_ids = raw.get("clobTokenIds")
        if isinstance(raw_ids, str):
            import json

            try:
                parsed = json.loads(raw_ids)
                if isinstance(parsed, list):
                    return [str(t) for t in parsed]
                return []
            except (json.JSONDecodeError, TypeError):
                logger.warning("Failed to parse clobTokenIds: %s", raw_ids)
                return []
        if isinstance(raw_ids, list):
            return [str(t) for t in raw_ids]
        return []

    @staticmethod
    def to_market_model(normalized: dict[str, Any]) -> Market:
        """Convert a normalised dict to a ``Market`` dataclass instance."""
        return Market.from_row(normalized)

    @staticmethod
    def to_snapshot_model(normalized: dict[str, Any]) -> MarketSnapshot:
        """Convert a normalised dict to a ``MarketSnapshot`` dataclass instance."""
        return MarketSnapshot.from_row(normalized)


# ── Module-level helpers ──────────────────────────────────────────────


def _to_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _best_level(levels: list[Any], side: str) -> float | None:
    """Return the best (highest bid / lowest ask) price from an order-book level list."""
    prices: list[float] = []
    for entry in levels:
        price_str = entry.get("price") if isinstance(entry, dict) else None
        if price_str is not None:
            p = _to_float(price_str)
            if p is not None:
                prices.append(p)
    if not prices:
        return None
    return max(prices) if side == "bid" else min(prices)


def _total_size(levels: list[Any]) -> float | None:
    """Sum the size of all levels in an order-book slice."""
    total = 0.0
    for entry in levels:
        size_str = entry.get("size") if isinstance(entry, dict) else None
        if size_str is not None:
            s = _to_float(size_str)
            if s is not None:
                total += s
    return total if total > 0 else None


def _normalize_timestamp(ts: Any) -> str:
    """Convert various timestamp formats to ISO-8601 string."""
    if ts is None:
        return ""
    if isinstance(ts, str):
        if ts.isdigit() or (ts.startswith("-") and ts[1:].isdigit()):
            from datetime import UTC, datetime

            try:
                val = int(ts)
                if val > 1e12:
                    # Likely milliseconds
                    val = val // 1000
                return datetime.fromtimestamp(val, tz=UTC).isoformat()
            except (OSError, OverflowError, ValueError):
                return ts
        if "T" in ts:
            return ts.replace("Z", "+00:00")
        return ts
    if isinstance(ts, (int, float)):
        from datetime import UTC, datetime

        try:
            val = int(ts)
            if val > 1e12:
                val = val // 1000
            return datetime.fromtimestamp(val, tz=UTC).isoformat()
        except (OSError, OverflowError, ValueError):
            return str(ts)
    return str(ts)
