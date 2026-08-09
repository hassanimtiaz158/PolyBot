"""Unit tests for enhanced monitoring and audit functionality.

Covers:
- AlertDispatcher: de-duplication, repeat detection, context sanitization.
- ExecutionHealthCheck / RiskEngineHealthCheck: error tracking.
- EventBus: emit, event structure, convenience methods.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.monitoring.alerts import AlertDispatcher
from app.monitoring.health import (
    ExecutionHealthCheck,
    HealthStatus,
    RiskEngineHealthCheck,
    health_status,
)

# ── AlertDispatcher tests ────────────────────────────────────────────


class TestAlertDispatcherSanitization:
    """Verify sensitive keys are stripped from alert context."""

    def test_sanitize_strips_credentials(self):
        context = {
            "api_key": "sk-12345",
            "poly_secret": "mysecret",
            "market_id": "mkt_001",
            "nested": {"password": "hunter2", "value": 42},
        }
        result = AlertDispatcher._sanitize_context(context)
        assert result["api_key"] == "[REDACTED]"
        assert result["poly_secret"] == "[REDACTED]"
        assert result["market_id"] == "mkt_001"
        assert result["nested"]["password"] == "[REDACTED]"
        assert result["nested"]["value"] == 42

    def test_sanitize_passphrase(self):
        context = {"poly_passphrase": "abc123", "side": "YES"}
        result = AlertDispatcher._sanitize_context(context)
        assert result["poly_passphrase"] == "[REDACTED]"
        assert result["side"] == "YES"

    def test_sanitize_empty_context(self):
        result = AlertDispatcher._sanitize_context({})
        assert result == {}

    def test_sanitize_case_insensitive(self):
        context = {"API_KEY": "secret", "ApiKey": "also-secret", "api_key": "yep"}
        result = AlertDispatcher._sanitize_context(context)
        for k in ("API_KEY", "ApiKey", "api_key"):
            assert result[k] == "[REDACTED]"


class TestAlertDispatcherDeDuplication:
    """Verify de-duplication suppresses repeated alerts."""

    @pytest.mark.asyncio
    async def test_first_alert_passes(self):
        dispatcher = AlertDispatcher()
        dispatcher._enabled = False  # disable webhook for test
        with patch.object(dispatcher, "_log_alert") as mock_log:
            await dispatcher.send("test", key="dup_key")
            mock_log.assert_called_once()

    @pytest.mark.asyncio
    async def test_duplicate_within_window_suppressed(self):
        dispatcher = AlertDispatcher()
        dispatcher._enabled = False
        dispatcher._min_interval = 60.0  # long window

        with patch.object(dispatcher, "_log_alert") as mock_log:
            await dispatcher.send("first", key="dup_key")
            await dispatcher.send("second", key="dup_key")
            # Only the first should be logged
            assert mock_log.call_count == 1

    @pytest.mark.asyncio
    async def test_duplicate_after_window_passes(self):
        dispatcher = AlertDispatcher()
        dispatcher._enabled = False
        dispatcher._min_interval = 0.0  # no window

        with patch.object(dispatcher, "_log_alert") as mock_log:
            await dispatcher.send("first", key="dup_key")
            await dispatcher.send("second", key="dup_key")
            assert mock_log.call_count == 2

    @pytest.mark.asyncio
    async def test_empty_key_always_passes(self):
        dispatcher = AlertDispatcher()
        dispatcher._enabled = False
        dispatcher._min_interval = 60.0

        with patch.object(dispatcher, "_log_alert") as mock_log:
            await dispatcher.send("first", key="")
            await dispatcher.send("second", key="")
            assert mock_log.call_count == 2


class TestAlertDispatcherRepeatDetection:
    """Verify repeat detection escalates alert level."""

    @pytest.mark.asyncio
    async def test_escalation_on_repeated_alerts(self):
        dispatcher = AlertDispatcher()
        dispatcher._enabled = False
        dispatcher._min_interval = 0.0
        dispatcher._repeat_threshold = 3
        dispatcher._repeat_window = 60.0

        with patch.object(dispatcher, "_log_alert") as mock_log:
            await dispatcher.send("a", level="WARNING", key="repeat")
            await dispatcher.send("b", level="WARNING", key="repeat")
            await dispatcher.send("c", level="WARNING", key="repeat")

            # Third alert should be escalated to CRITICAL
            calls = mock_log.call_args_list
            assert calls[0][0][2] == "WARNING"
            assert calls[1][0][2] == "WARNING"
            assert calls[2][0][2] == "CRITICAL"


# ── ExecutionHealthCheck tests ───────────────────────────────────────


class TestExecutionHealthCheck:
    def test_record_success_sets_healthy(self):
        check = ExecutionHealthCheck()
        health_status.checks.pop("execution", None)
        check.record_success()
        assert health_status.is_healthy("execution") is True

    def test_consecutive_errors_mark_unhealthy(self):
        check = ExecutionHealthCheck(max_consecutive_errors=2)
        health_status.checks.pop("execution", None)
        check.record_error()
        assert health_status.is_healthy("execution") is True  # 1 error, still ok
        check.record_error()
        assert health_status.is_healthy("execution") is False  # 2 errors, unhealthy

    def test_success_resets_error_count(self):
        check = ExecutionHealthCheck(max_consecutive_errors=2)
        health_status.checks.pop("execution", None)
        check.record_error()
        check.record_success()
        check.record_error()
        # Only 1 consecutive error after the success
        assert health_status.is_healthy("execution") is True

    @pytest.mark.asyncio
    async def test_check_reflects_state(self):
        check = ExecutionHealthCheck(max_consecutive_errors=1)
        health_status.checks.pop("execution", None)
        check.record_error()
        result = await check.check()
        assert result is False
        check.record_success()
        result = await check.check()
        assert result is True


# ── RiskEngineHealthCheck tests ──────────────────────────────────────


class TestRiskEngineHealthCheck:
    def test_record_evaluation_sets_healthy(self):
        check = RiskEngineHealthCheck()
        health_status.checks.pop("risk_engine", None)
        check.record_evaluation()
        assert health_status.is_healthy("risk_engine") is True

    def test_consecutive_errors_mark_unhealthy(self):
        check = RiskEngineHealthCheck(max_consecutive_errors=2)
        health_status.checks.pop("risk_engine", None)
        check.record_error()
        assert health_status.is_healthy("risk_engine") is True
        check.record_error()
        assert health_status.is_healthy("risk_engine") is False

    def test_success_resets_consecutive_errors(self):
        check = RiskEngineHealthCheck(max_consecutive_errors=2)
        health_status.checks.pop("risk_engine", None)
        check.record_error()
        check.record_error()
        check.record_evaluation()
        assert health_status.is_healthy("risk_engine") is True

    @pytest.mark.asyncio
    async def test_check_reflects_state(self):
        check = RiskEngineHealthCheck(max_consecutive_errors=1)
        health_status.checks.pop("risk_engine", None)
        check.record_error()
        result = await check.check()
        assert result is False
        check.record_evaluation()
        result = await check.check()
        assert result is True


# ── EventBus tests ───────────────────────────────────────────────────


class TestAuditEvent:
    """Test AuditEvent dataclass and serialization."""

    def test_event_creation(self):
        from app.audit.events import AuditEvent

        event = AuditEvent(
            event_type="TEST_EVENT",
            market_id="mkt_001",
            strategy="test_strat",
            decision="APPROVED",
            reason="test reason",
        )
        assert event.event_type == "TEST_EVENT"
        assert event.market_id == "mkt_001"
        assert event.strategy == "test_strat"
        assert event.event_id  # auto-generated

    def test_to_dict(self):
        from app.audit.events import AuditEvent

        event = AuditEvent(
            event_type="ORDER_FILLED",
            market_id="mkt_001",
            values={"filled_size": 10.0, "side": "YES"},
        )
        d = event.to_dict()
        assert d["event_type"] == "ORDER_FILLED"
        assert d["market_id"] == "mkt_001"
        assert d["filled_size"] == 10.0
        assert d["side"] == "YES"

    def test_as_risk_event(self):
        from app.audit.events import AuditEvent

        event = AuditEvent(
            event_type="RISK_REJECTED",
            severity="WARNING",
            reason="STALE_DATA",
        )
        re = event.as_risk_event()
        assert re.event_type == "RISK_REJECTED"
        assert re.severity == "WARNING"
        assert re.reason == "STALE_DATA"


class TestEventBus:
    """Test EventBus emit and convenience methods."""

    @pytest.mark.asyncio
    async def test_emit_creates_event(self):
        from app.audit.events import EventBus

        bus = EventBus(persist=False)
        event = await bus.emit("SYSTEM_START", reason="test")
        assert event.event_type == "SYSTEM_START"
        assert event.severity == "INFO"

    @pytest.mark.asyncio
    async def test_emit_unknown_type(self):
        from app.audit.events import EventBus

        bus = EventBus(persist=False)
        event = await bus.emit("UNKNOWN_EVENT")
        assert event.event_type == "UNKNOWN_EVENT"
        assert event.severity == "INFO"  # default

    @pytest.mark.asyncio
    async def test_data_received_convenience(self):
        from app.audit.events import EventBus

        bus = EventBus(persist=False)
        event = await bus.data_received(market_id="mkt_001", fresh_seconds=2.5)
        assert event.event_type == "DATA_RECEIVED"
        assert event.market_id == "mkt_001"
        assert event.values.get("fresh_seconds") == 2.5

    @pytest.mark.asyncio
    async def test_signal_created_convenience(self):
        from app.audit.events import EventBus

        bus = EventBus(persist=False)
        event = await bus.signal_created(
            market_id="mkt_001",
            strategy="microstructure",
            side="YES",
            edge=0.05,
            confidence=0.8,
        )
        assert event.event_type == "SIGNAL_CREATED"
        assert event.decision == "CANDIDATE"
        assert event.values["side"] == "YES"

    @pytest.mark.asyncio
    async def test_order_rejected_convenience(self):
        from app.audit.events import EventBus

        bus = EventBus(persist=False)
        event = await bus.order_rejected(
            order_id="ord_001",
            market_id="mkt_001",
            reason="adapter error",
            side="YES",
        )
        assert event.event_type == "ORDER_REJECTED"
        assert event.reason == "adapter error"

    @pytest.mark.asyncio
    async def test_emit_with_persistence(self, test_db):
        from app.audit.events import EventBus
        from app.storage.repositories import RiskEventRepository

        repo = RiskEventRepository()
        bus = EventBus(repo=repo, persist=True)
        event = await bus.emit("ORDER_FILLED", order_id="ord_test")
        assert event.event_type == "ORDER_FILLED"

    @pytest.mark.asyncio
    async def test_emit_routes_to_alert(self):
        from app.audit.events import EventBus

        mock_notifier = AsyncMock()
        bus = EventBus(persist=False, notifier=mock_notifier)
        await bus.emit("CIRCUIT_BREAKER", reason="test trigger")
        mock_notifier.send.assert_called_once()


class TestHealthStatusEnhanced:
    """Test enhanced HealthStatus features."""

    def test_consecutive_failures_tracking(self):
        hs = HealthStatus()
        hs.set_unhealthy("test_check")
        hs.set_unhealthy("test_check")
        assert hs.consecutive_failures("test_check") == 2
        hs.set_healthy("test_check")
        assert hs.consecutive_failures("test_check") == 0

    def test_total_errors_counting(self):
        hs = HealthStatus()
        hs.set_unhealthy("test_check")
        hs.set_healthy("test_check")
        hs.set_unhealthy("test_check")
        assert hs.total_errors("test_check") == 2

    def test_summary_includes_new_fields(self):
        hs = HealthStatus()
        hs.set_unhealthy("db")
        summary = hs.summary()
        assert summary["checks"]["db"]["consecutive_failures"] == 1
        assert summary["checks"]["db"]["total_errors"] == 1
