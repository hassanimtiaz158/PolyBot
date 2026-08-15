"""Order-book feature calculations: midpoint, spread, OBI, depth, freshness."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.storage.models import MarketSnapshot


class OrderBookFeatures:
    """Computes order-book-derived features from snapshot data.

    All static methods can be used standalone.  The ``compute()`` method
    returns a complete feature dict with explicit timestamp.
    """

    # ── Standalone calculators ─────────────────────────────────────

    @staticmethod
    def midpoint(bid: float | None, ask: float | None) -> float | None:
        """Midpoint = (bid + ask) / 2.  Returns None if either is missing."""
        if bid is None or ask is None:
            return None
        return (bid + ask) / 2.0

    @staticmethod
    def absolute_spread(bid: float | None, ask: float | None) -> float | None:
        """Absolute spread = ask - bid.  Returns None if either is missing."""
        if bid is None or ask is None:
            return None
        return ask - bid

    @staticmethod
    def relative_spread(bid: float | None, ask: float | None) -> float | None:
        """Relative spread = (ask - bid) / midpoint.  Returns None on missing/zero."""
        mid = OrderBookFeatures.midpoint(bid, ask)
        if mid is None or mid == 0.0:
            return None
        return (ask - bid) / mid  # type: ignore[operator]

    @staticmethod
    def order_book_imbalance(
        bid_depth: float | None, ask_depth: float | None
    ) -> float | None:
        """OBI = (bid_depth - ask_depth) / (bid_depth + ask_depth).

        Returns 0.0 when both sides are zero (balanced).  Returns None
        when either depth is missing.
        """
        if bid_depth is None or ask_depth is None:
            return None
        total = bid_depth + ask_depth
        if total == 0:
            return 0.0
        return (bid_depth - ask_depth) / total

    @staticmethod
    def top_n_depth(
        bids: list[dict[str, Any]] | None,
        asks: list[dict[str, Any]] | None,
        n: int = 5,
    ) -> dict[str, float | None]:
        """Sum the size of the top *n* levels on each side.

        Expects each level dict to have a ``"size"`` key (string or number).
        Returns ``{"top_n_bid_depth": ..., "top_n_ask_depth": ...}``.
        """
        def _sum_sizes(levels: list[dict[str, Any]] | None, count: int) -> float | None:
            if levels is None:
                return None
            total = 0.0
            for level in levels[:count]:
                try:
                    val = level.get("size", 0)
                    total += float(val)
                except (ValueError, TypeError):
                    pass
            return total

        return {
            "top_n_bid_depth": _sum_sizes(bids, n),
            "top_n_ask_depth": _sum_sizes(asks, n),
        }

    @staticmethod
    def data_freshness(
        timestamp_str: str | None, max_age_seconds: int = 5
    ) -> bool:
        """``True`` if the timestamp is recent enough (not stale)."""
        if timestamp_str is None:
            return False
        try:
            ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
            age = (datetime.now(UTC) - ts).total_seconds()
            return age <= max_age_seconds
        except (ValueError, TypeError):
            return False

    # ── Batch compute ──────────────────────────────────────────────

    def compute(
        self,
        snapshot: MarketSnapshot | dict[str, Any],
        bids: list[dict[str, Any]] | None = None,
        asks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Compute all order-book features from a snapshot (and optional raw levels).

        Returns a dict with keys:
        ``midpoint, absolute_spread, relative_spread, bid_depth, ask_depth,
        obi, top_n_bid_depth, top_n_ask_depth, data_fresh, timestamp``.
        """
        s = _unwrap(snapshot)

        bid = s.bid
        ask = s.ask
        bd = s.bid_depth
        ad = s.ask_depth

        mid = self.midpoint(bid, ask)
        abs_spr = self.absolute_spread(bid, ask)
        rel_spr = self.relative_spread(bid, ask)
        obi = self.order_book_imbalance(bd, ad)
        top_n = self.top_n_depth(bids, asks)
        fresh = self.data_freshness(s.timestamp)

        ts = s.timestamp or datetime.now(UTC).isoformat()
        return {
            "midpoint": mid,
            "absolute_spread": abs_spr,
            "relative_spread": rel_spr,
            "bid_depth": bd,
            "ask_depth": ad,
            "obi": obi,
            "top_n_bid_depth": top_n["top_n_bid_depth"],
            "top_n_ask_depth": top_n["top_n_ask_depth"],
            "data_fresh": fresh,
            "timestamp": ts,
        }


def _unwrap(
    snapshot: MarketSnapshot | dict[str, Any],
) -> MarketSnapshot:
    if isinstance(snapshot, MarketSnapshot):
        return snapshot
    return MarketSnapshot.from_row(snapshot)
