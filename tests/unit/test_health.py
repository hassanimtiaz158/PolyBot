"""Unit tests for health check mechanism."""

import pytest

from app.monitoring.health import (
    ApiHealthCheck,
    HealthStatus,
)


class TestHealthStatus:
    def test_initial_state(self):
        hs = HealthStatus()
        assert hs.all_healthy() is False

    def test_set_healthy(self):
        hs = HealthStatus()
        hs.set_healthy("database")
        assert hs.is_healthy("database") is True

    def test_set_unhealthy(self):
        hs = HealthStatus()
        hs.set_healthy("database")
        hs.set_unhealthy("database")
        assert hs.is_healthy("database") is False

    def test_all_healthy_true(self):
        hs = HealthStatus()
        hs.set_healthy("db")
        hs.set_healthy("api")
        assert hs.all_healthy() is True

    def test_all_healthy_false(self):
        hs = HealthStatus()
        hs.set_healthy("db")
        hs.set_unhealthy("api")
        assert hs.all_healthy() is False

    def test_summary(self):
        hs = HealthStatus()
        hs.set_healthy("db")
        summary = hs.summary()
        assert summary["overall"] is True
        assert "db" in summary["checks"]


class TestApiHealthCheck:
    @pytest.mark.asyncio
    async def test_no_credentials_is_healthy(self):
        check = ApiHealthCheck()
        result = await check.check()
        assert result is True

    @pytest.mark.asyncio
    async def test_detects_api_health(self):
        import os
        old = os.environ.get("POLY_API_KEY")
        os.environ["POLY_API_KEY"] = "test-key"
        # Refresh settings cache
        from app.config.settings import settings
        settings.poly_api_key = "test-key"
        check = ApiHealthCheck()
        result = await check.check()
        assert result is True
        if old is None:
            del os.environ["POLY_API_KEY"]
        else:
            os.environ["POLY_API_KEY"] = old
        settings.poly_api_key = old
