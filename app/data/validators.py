"""Data quality assessment for market-data ingestion.

Defines a ``DataQuality`` enum (HEALTHY / STALE / INVALID / DISCONNECTED /
UNKNOWN) and the ``DataValidator`` class used to evaluate every incoming
piece of data before it enters the pipeline. Trading decisions **must**
reject any data whose quality is not HEALTHY.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any


class DataQuality(Enum):
    """Categorical quality of a market-data payload.

    Values ordered from best to worst so they can be compared:
    ``HEALTHY < STALE < INVALID < DISCONNECTED < UNKNOWN``.
    """

    HEALTHY = "HEALTHY"
    STALE = "STALE"
    INVALID = "INVALID"
    DISCONNECTED = "DISCONNECTED"
    UNKNOWN = "UNKNOWN"

    def __lt__(self, other: Any) -> bool:
        if not isinstance(other, DataQuality):
            return NotImplemented
        order = list(DataQuality)
        return order.index(self) < order.index(other)

    def __le__(self, other: Any) -> bool:
        if not isinstance(other, DataQuality):
            return NotImplemented
        return self < other or self == other

    def __gt__(self, other: Any) -> bool:
        if not isinstance(other, DataQuality):
            return NotImplemented
        return not self <= other

    def __ge__(self, other: Any) -> bool:
        if not isinstance(other, DataQuality):
            return NotImplemented
        return not self < other


@dataclass
class QualityReport:
    """Result of a single quality check on a data payload."""

    quality: DataQuality
    reason: str | None = None
    details: dict[str, Any] | None = None


# ── Fields required for each data type ────────────────────────────────

REQUIRED_MARKET_FIELDS: set[str] = {"market_id", "question", "condition_id"}
REQUIRED_SNAPSHOT_FIELDS: set[str] = {"market_id", "bid", "ask", "timestamp"}
REQUIRED_SNAPSHOT_MIDPOINT_ASK: set[str] = {"midpoint", "spread"}

# Prices must be within [0, 1] — they are probabilities
VALID_PRICE_RANGE: tuple[float, float] = (0.0, 1.0)
# Spread must be non-negative
VALID_SPREAD_RANGE: tuple[float, float] = (0.0, 1.0)


class DataValidator:
    """Inspects a normalised data payload and returns a quality assessment.

    Parameters
    ----------
    max_age_seconds:
        Maximum allowed age (in seconds) for a data point before it is
        considered stale.  Default matches ``settings.data_max_age_seconds``.
    """

    def __init__(self, max_age_seconds: int = 5) -> None:
        self._max_age_seconds = max_age_seconds

    # ── Public API ──────────────────────────────────────────────────

    def assess(self, data: dict[str, Any], data_type: str = "snapshot") -> DataQuality:
        """High-level quality check — returns a ``DataQuality`` value.

        Parameters
        ----------
        data:
            Normalised data dict (output of ``DataNormalizer``).
        data_type:
            ``"snapshot"`` (order-book snapshot) or ``"market"`` (metadata).
        """
        if data_type == "snapshot":
            return self.check_snapshot(data).quality
        return self.check_market(data).quality

    def check_snapshot(self, data: dict[str, Any]) -> QualityReport:
        """Full quality check for an order-book snapshot."""
        # ── Required fields ─────────────────────────────────────────
        report = self._check_required(data, REQUIRED_SNAPSHOT_FIELDS)
        if report.quality != DataQuality.HEALTHY:
            return report

        # ── Timestamp validity & freshness ──────────────────────────
        ts_val = data.get("timestamp")
        ts_field: str | None = ts_val if isinstance(ts_val, str) else None
        if ts_field is not None:
            report = self._check_timestamp(ts_field)
            if report.quality != DataQuality.HEALTHY:
                return report

        # ── Price bounds ────────────────────────────────────────────
        for price_field in ("bid", "ask", "midpoint"):
            val = data.get(price_field)
            if val is not None:
                report = self._check_price(val, price_field)
                if report.quality != DataQuality.HEALTHY:
                    return report

        # ── Spread validity ─────────────────────────────────────────
        spread = data.get("spread")
        if spread is not None:
            report = self._check_spread(spread)
            if report.quality != DataQuality.HEALTHY:
                return report

        return QualityReport(quality=DataQuality.HEALTHY)

    def check_market(self, data: dict[str, Any]) -> QualityReport:
        """Full quality check for market metadata."""
        report = self._check_required(data, REQUIRED_MARKET_FIELDS)
        if report.quality != DataQuality.HEALTHY:
            return report
        return QualityReport(quality=DataQuality.HEALTHY)

    def check_timestamp(self, timestamp_str: str | None) -> QualityReport:
        """Check whether a timestamp string is present and fresh."""
        return self._check_timestamp(timestamp_str)

    # ── Internal helpers ────────────────────────────────────────────

    @staticmethod
    def _check_required(data: dict[str, Any], fields: set[str]) -> QualityReport:
        missing = [f for f in fields if data.get(f) is None]
        if missing:
            return QualityReport(
                quality=DataQuality.INVALID,
                reason=f"Missing required fields: {missing}",
                details={"missing": missing},
            )
        return QualityReport(quality=DataQuality.HEALTHY)

    def _check_timestamp(self, timestamp_str: str | None) -> QualityReport:
        if timestamp_str is None:
            return QualityReport(
                quality=DataQuality.INVALID,
                reason="Timestamp is missing",
            )
        try:
            ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            # Might be a numeric timestamp (seconds or milliseconds)
            try:
                numeric = float(timestamp_str)
                if numeric > 1e12:
                    # Milliseconds — convert to seconds
                    numeric = numeric / 1000.0
                ts = datetime.fromtimestamp(numeric, tz=UTC)
            except (ValueError, TypeError, OSError):
                return QualityReport(
                    quality=DataQuality.INVALID,
                    reason=f"Cannot parse timestamp: {timestamp_str!r}",
                    details={"timestamp": timestamp_str},
                )

        age = (datetime.now(UTC) - ts).total_seconds()
        if age < 0:
            # Future timestamp — likely milliseconds misparse; clamp to 0
            age = 0
        if age > self._max_age_seconds:
            return QualityReport(
                quality=DataQuality.STALE,
                reason=f"Data is {age:.1f}s old (max {self._max_age_seconds}s)",
                details={"age_seconds": age, "max_age": self._max_age_seconds},
            )
        return QualityReport(quality=DataQuality.HEALTHY)

    @staticmethod
    def _check_price(value: float, field_name: str) -> QualityReport:
        lo, hi = VALID_PRICE_RANGE
        if not isinstance(value, (int, float)):
            return QualityReport(
                quality=DataQuality.INVALID,
                reason=f"{field_name} is not numeric: {value!r}",
                details={"field": field_name, "value": value},
            )
        if value < lo or value > hi:
            return QualityReport(
                quality=DataQuality.INVALID,
                reason=f"{field_name}={value} outside [{lo}, {hi}]",
                details={"field": field_name, "value": value, "min": lo, "max": hi},
            )
        return QualityReport(quality=DataQuality.HEALTHY)

    @staticmethod
    def _check_spread(spread: float) -> QualityReport:
        lo, hi = VALID_SPREAD_RANGE
        if not isinstance(spread, (int, float)):
            return QualityReport(
                quality=DataQuality.INVALID,
                reason=f"spread is not numeric: {spread!r}",
            )
        if spread < lo or spread > hi:
            return QualityReport(
                quality=DataQuality.INVALID,
                reason=f"spread={spread} outside [{lo}, {hi}]",
                details={"spread": spread, "min": lo, "max": hi},
            )
        return QualityReport(quality=DataQuality.HEALTHY)
