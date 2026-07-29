"""System health-check mechanism.

Provides composable check classes and a global ``health_status`` instance
that modules can read to gate trading decisions.
"""

import asyncio
from datetime import UTC, datetime
from typing import Any

from app.config.settings import settings


class HealthStatus:
    """Tracks pass/fail status for each registered health check."""

    def __init__(self) -> None:
        self.checks: dict[str, bool] = {}
        self.last_updated: dict[str, str] = {}

    def set_healthy(self, check_name: str) -> None:
        self.checks[check_name] = True
        self.last_updated[check_name] = datetime.now(UTC).isoformat()

    def set_unhealthy(self, check_name: str) -> None:
        self.checks[check_name] = False
        self.last_updated[check_name] = datetime.now(UTC).isoformat()

    def is_healthy(self, check_name: str) -> bool:
        return self.checks.get(check_name, False)

    def all_healthy(self) -> bool:
        return all(self.checks.values()) if self.checks else False

    def summary(self) -> dict[str, Any]:
        return {
            "overall": self.all_healthy(),
            "checks": {
                name: {
                    "healthy": status,
                    "last_updated": self.last_updated.get(name),
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


checks: dict[str, Any] = {
    "database": DatabaseCheck(),
    "data_freshness": DataFreshnessCheck(
        max_age_seconds=settings.data_max_age_seconds
    ),
    "api": ApiHealthCheck(),
    "model_availability": ModelAvailabilityCheck(),
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
