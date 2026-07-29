"""Price-return and momentum features over configurable lookback windows.

All calculations are strictly timestamp-based to prevent look-ahead bias.
The internal buffer stores ``(unix_timestamp, midpoint)`` pairs.
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import UTC, datetime
from typing import Any

from app.storage.models import MarketSnapshot

logger = logging.getLogger(__name__)


class MomentumFeatures:
    """Computes price returns and momentum over configurable time windows.

    Usage::

        mf = MomentumFeatures(windows_seconds=[5, 30, 300])
        mf.update("2026-01-01T00:00:00Z", 0.45)
        ret_5s = mf.return_over_window(5)
        vel = mf.velocity(60)
        features = mf.compute(snapshot)
    """

    def __init__(self, windows_seconds: list[int] | None = None) -> None:
        self.windows = windows_seconds or [1, 5, 30, 300]
        # Each entry: (unix_timestamp_seconds, midpoint)
        self._history: deque[tuple[float, float]] = deque(maxlen=2000)

    # ── Public API ─────────────────────────────────────────────────

    def update(self, timestamp: str, midpoint: float) -> None:
        """Record a new timestamped price observation."""
        try:
            ts = _to_unix(timestamp)
            self._history.append((ts, midpoint))
        except (ValueError, TypeError):
            logger.warning("Invalid timestamp in update: %s", timestamp)

    def return_over_window(self, window_seconds: int) -> float | None:
        """Return ``(current_midpoint - past_midpoint) / past_midpoint``.

        The past price is the most recent observation whose timestamp is
        at least ``window_seconds`` before the latest entry.  Returns
        ``None`` when there are fewer than 2 observations or the past
        price is zero/missing.
        """
        if len(self._history) < 2:
            return None

        latest_ts, current_mid = self._history[-1]
        cutoff = latest_ts - window_seconds

        past_mid: float | None = None
        for ts, mid in reversed(self._history):
            if ts <= cutoff:
                past_mid = mid
                break

        if past_mid is None or past_mid == 0.0:
            return None

        return (current_mid - past_mid) / past_mid

    def velocity(self, window_seconds: int = 60) -> float | None:
        """Midpoint price change per second over the lookback window."""
        ret = self.return_over_window(window_seconds)
        if ret is None or window_seconds <= 0:
            return None
        return ret / window_seconds

    # ── Batch compute ──────────────────────────────────────────────

    def compute(
        self, snapshot: MarketSnapshot | dict[str, Any]
    ) -> dict[str, Any]:
        """Update internal buffer and return momentum features.

        Returns a dict with keys ``f"return_{w}s"`` for each configured
        window, ``velocity_60s``, and ``timestamp``.
        """
        s = _unwrap(snapshot)

        if s.midpoint is not None and s.timestamp is not None:
            self.update(s.timestamp, s.midpoint)

        result: dict[str, Any] = {
            f"return_{w}s": self.return_over_window(w) for w in self.windows
        }
        result["velocity_60s"] = self.velocity(60)
        result["timestamp"] = s.timestamp or datetime.now(UTC).isoformat()
        return result


# ── Helpers ────────────────────────────────────────────────────────────


def _to_unix(timestamp_str: str) -> float:
    """Convert an ISO-8601 string to a Unix timestamp (seconds)."""
    return datetime.fromisoformat(
        timestamp_str.replace("Z", "+00:00")
    ).timestamp()


def _unwrap(
    snapshot: MarketSnapshot | dict[str, Any],
) -> MarketSnapshot:
    if isinstance(snapshot, MarketSnapshot):
        return snapshot
    return MarketSnapshot.from_row(snapshot)
