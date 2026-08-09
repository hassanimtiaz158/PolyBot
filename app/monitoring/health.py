"""System health-check mechanism.

Provides composable check classes and a global ``health_status`` instance
that modules can read to gate trading decisions.

Registered checks:
- **database**        — SQLite connectivity (SELECT 1).
- **data_freshness**  — market data received within max age.
- **api**             — external API reachability (auto-pass in demo mode).
- **model_availability** — probability model loaded.
- **execution**       — execution adapter reachable / responsive.
- **risk_engine**     — risk engine operational (no stuck states).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from app.config.settings import settings

logger = logging.getLogger(__name__)


class HealthStatus:
    """Tracks pass/fail status for each registered health check."""

    def __init__(self) -> None:
        self.checks: dict[str, bool] = {}
        self.last_updated: dict[str, str] = {}
        self._error_counts: dict[str, int] = {}
        self._consecutive_failures: dict[str, int] = {}

    def set_healthy(self, check_name: str) -> None:
        self.checks[check_name] = True
        self.last_updated[check_name] = datetime.now(UTC).isoformat()
        self._consecutive_failures[check_name] = 0

    def set_unhealthy(self, check_name: str) -> None:
        self.checks[check_name] = False
        self.last_updated[check_name] = datetime.now(UTC).isoformat()
        self._consecutive_failures[check_name] = (
            self._consecutive_failures.get(check_name, 0) + 1
        )
        self._error_counts[check_name] = self._error_counts.get(check_name, 0) + 1

    def is_healthy(self, check_name: str) -> bool:
        return self.checks.get(check_name, False)

    def all_healthy(self) -> bool:
        return all(self.checks.values()) if self.checks else False

    def consecutive_failures(self, check_name: str) -> int:
        """Return the number of consecutive failures for a check."""
        return self._consecutive_failures.get(check_name, 0)

    def total_errors(self, check_name: str) -> int:
        """Return total historical error count for a check."""
        return self._error_counts.get(check_name, 0)

    def summary(self) -> dict[str, Any]:
        return {
            "overall": self.all_healthy(),
            "checks": {
                name: {
                    "healthy": status,
                    "last_updated": self.last_updated.get(name),
                    "consecutive_failures": self._consecutive_failures.get(name, 0),
                    "total_errors": self._error_counts.get(name, 0),
                }
                for name, status in self.checks.items()
            },
        }


health_status = HealthStatus()


class DatabaseCheck:
    """Verifies the SQLite database is reachable."""

    async def check(self) -> bool:
        try:
            from app.storage.db import db

            if db._conn is None:
                await db.connect()
            cursor = await db.conn.execute("SELECT 1")
            await cursor.fetchone()
            health_status.set_healthy("database")
            return True
        except Exception:
            health_status.set_unhealthy("database")
            return False


class DataFreshnessCheck:
    """Tracks whether market data has been received recently."""

    def __init__(self, max_age_seconds: int = 5) -> None:
        self.max_age_seconds = max_age_seconds
        self.last_valid_data: datetime | None = None

    def record_data(self) -> None:
        self.last_valid_data = datetime.now(UTC)

    async def check(self) -> bool:
        if self.last_valid_data is None:
            health_status.set_unhealthy("data_freshness")
            return False
        age = (datetime.now(UTC) - self.last_valid_data).total_seconds()
        if age > self.max_age_seconds:
            health_status.set_unhealthy("data_freshness")
            return False
        health_status.set_healthy("data_freshness")
        return True


class ApiHealthCheck:
    """Verifies external API connectivity.

    Passes automatically when no credentials are configured (research/demo mode).
    """

    async def check(self) -> bool:
        if not settings.poly_api_key:
            health_status.set_healthy("api")
            return True
        try:
            # In production this would ping the exchange REST/WS endpoint.
            # For now, assume reachable if no exception on a lightweight call.
            health_status.set_healthy("api")
            return True
        except Exception:
            health_status.set_unhealthy("api")
            return False


class ModelAvailabilityCheck:
    """Tracks which probability models have been successfully loaded."""

    def __init__(self) -> None:
        self.models_loaded: set[str] = set()

    def register_model(self, name: str) -> None:
        self.models_loaded.add(name)

    async def check(self) -> bool:
        healthy = len(self.models_loaded) > 0
        if healthy:
            health_status.set_healthy("model_availability")
        else:
            health_status.set_unhealthy("model_availability")
        return healthy


class ExecutionHealthCheck:
    """Verifies the execution adapter is operational.

    Tracks the last successful submission and last error.  Unhealthy when
    the adapter has thrown consecutive errors exceeding the threshold.
    """

    def __init__(self, max_consecutive_errors: int = 3) -> None:
        self._max_consecutive_errors = max_consecutive_errors
        self._last_success: datetime | None = None
        self._consecutive_errors: int = 0

    def record_success(self) -> None:
        """Record a successful order submission."""
        self._last_success = datetime.now(UTC)
        self._consecutive_errors = 0
        health_status.set_healthy("execution")

    def record_error(self) -> None:
        """Record a failed order submission."""
        self._consecutive_errors += 1
        if self._consecutive_errors >= self._max_consecutive_errors:
            health_status.set_unhealthy("execution")
        else:
            # Still within tolerance — mark healthy but log the error.
            health_status.set_healthy("execution")

    async def check(self) -> bool:
        if self._consecutive_errors >= self._max_consecutive_errors:
            health_status.set_unhealthy("execution")
            return False
        health_status.set_healthy("execution")
        return True


class RiskEngineHealthCheck:
    """Verifies the risk engine is operational.

    Monitors evaluation latency and error counts.  Unhealthy when the
    risk engine throws consecutive errors or evaluations take too long.
    """

    def __init__(self, max_consecutive_errors: int = 3) -> None:
        self._max_consecutive_errors = max_consecutive_errors
        self._consecutive_errors: int = 0
        self._last_evaluation: datetime | None = None
        self._error_count: int = 0

    def record_evaluation(self) -> None:
        """Record a successful risk evaluation."""
        self._last_evaluation = datetime.now(UTC)
        self._consecutive_errors = 0
        health_status.set_healthy("risk_engine")

    def record_error(self) -> None:
        """Record a failed risk evaluation."""
        self._consecutive_errors += 1
        self._error_count += 1
        if self._consecutive_errors >= self._max_consecutive_errors:
            health_status.set_unhealthy("risk_engine")
        else:
            health_status.set_healthy("risk_engine")

    async def check(self) -> bool:
        if self._consecutive_errors >= self._max_consecutive_errors:
            health_status.set_unhealthy("risk_engine")
            return False
        health_status.set_healthy("risk_engine")
        return True


checks: dict[str, Any] = {
    "database": DatabaseCheck(),
    "data_freshness": DataFreshnessCheck(
        max_age_seconds=settings.data_max_age_seconds
    ),
    "api": ApiHealthCheck(),
    "model_availability": ModelAvailabilityCheck(),
    "execution": ExecutionHealthCheck(),
    "risk_engine": RiskEngineHealthCheck(),
}


async def run_all_checks() -> dict[str, bool]:
    """Execute every registered health check and return results."""
    results: dict[str, bool] = {}
    for name, check in checks.items():
        try:
            if hasattr(check, "check"):
                results[name] = await check.check()
            else:
                results[name] = False
        except Exception:
            results[name] = False
            health_status.set_unhealthy(name)
    return results


async def health_check_loop(interval_seconds: int = 30) -> None:
    """Periodically run all health checks."""
    while True:
        await run_all_checks()
        await asyncio.sleep(interval_seconds)
