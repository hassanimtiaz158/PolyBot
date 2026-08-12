"""Unit tests for the backend-controlled emergency kill switch.

Covers the kill switch state machine, persistence across a restart,
explicit-confirmation resume, and the requirement that an order cannot
be submitted while the switch is KILLED.
"""

from typing import Any

import pytest

from app.execution.engine import ExecutionEngine
from app.execution.interface import ExecutionAdapter
from app.risk.engine import RiskDecision
from app.risk.kill_switch import (
    KILL_SWITCH_REASON,
    KillSwitch,
    KillSwitchResumeError,
    KillSwitchState,
)


class TestKillSwitchState:
    def test_defaults_to_active(self) -> None:
        ks = KillSwitch(persist=False)
        assert ks.state == KillSwitchState.ACTIVE
        assert ks.reason is None

    def test_reason_constant(self) -> None:
        assert KILL_SWITCH_REASON == "MANUAL EMERGENCY STOP"


class TestKillSwitchKill:
    @pytest.mark.asyncio
    async def test_kill_sets_killed_with_reason(self) -> None:
        ks = KillSwitch(persist=False)
        state = await ks.kill(operator="ops-1")
        assert state == KillSwitchState.KILLED
        assert ks.state == KillSwitchState.KILLED
        assert ks.reason == KILL_SWITCH_REASON
        assert ks.killed_by == "ops-1"
        assert ks.killed_at is not None

    @pytest.mark.asyncio
    async def test_is_killed_true_after_kill(self) -> None:
        ks = KillSwitch(persist=False)
        assert await ks.is_killed() is False
        await ks.kill()
        assert await ks.is_killed() is True

    @pytest.mark.asyncio
    async def test_kill_is_idempotent(self) -> None:
        ks = KillSwitch(persist=False)
        await ks.kill(operator="one")
        killed_at = ks.killed_at
        await ks.kill(operator="two")
        assert ks.killed_at == killed_at
        assert ks.killed_by == "one"


class TestKillSwitchResume:
    @pytest.mark.asyncio
    async def test_resume_requires_confirmation(self) -> None:
        ks = KillSwitch(persist=False)
        await ks.kill()
        with pytest.raises(KillSwitchResumeError):
            await ks.resume()
        # Still killed after a failed resume.
        assert ks.state == KillSwitchState.KILLED
        assert await ks.is_killed() is True

    @pytest.mark.asyncio
    async def test_resume_with_confirmation_reactivates(self) -> None:
        ks = KillSwitch(persist=False)
        await ks.kill(operator="ops-1")
        state = await ks.resume(operator="ops-2", confirm=True)
        assert state == KillSwitchState.ACTIVE
        assert ks.state == KillSwitchState.ACTIVE
        assert ks.killed_at is None
        assert await ks.is_killed() is False

    @pytest.mark.asyncio
    async def test_resume_active_is_idempotent(self) -> None:
        ks = KillSwitch(persist=False)
        state = await ks.resume(confirm=True)
        assert state == KillSwitchState.ACTIVE


class TestKillSwitchAudit:
    @pytest.mark.asyncio
    async def test_kill_emits_audit_event(self) -> None:
        bus = _RecordingBus()
        ks = KillSwitch(persist=False, event_bus=bus)
        await ks.kill(operator="ops-1")
        assert [e["event_type"] for e in bus.events] == ["KILL_SWITCH"]
        event = bus.events[0]
        assert event["decision"] == "KILLED"
        assert event["reason"] == KILL_SWITCH_REASON
        assert event["operator"] == "ops-1"
        assert event["severity"] == "CRITICAL"

    @pytest.mark.asyncio
    async def test_resume_emits_audit_event(self) -> None:
        bus = _RecordingBus()
        ks = KillSwitch(persist=False, event_bus=bus)
        await ks.kill()
        await ks.resume(confirm=True, operator="ops-2")
        types = [e["event_type"] for e in bus.events]
        assert types == ["KILL_SWITCH", "KILL_SWITCH_RESUMED"]
        assert bus.events[1]["decision"] == "ACTIVE"
        assert bus.events[1]["operator"] == "ops-2"
        assert bus.events[1]["severity"] == "INFO"


class _RecordingBus:
    """Minimal event bus that records emitted events."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(self, event_type: str, **values: Any) -> Any:
        self.events.append({"event_type": event_type, **values})
        return None


# ── Persistence (state survives a restart) ───────────────────────────


class TestKillSwitchPersistence:
    @pytest.mark.asyncio
    async def test_killed_state_survives_restart(self, isolated_db) -> None:
        """A KILLED switch reloads as KILLED after a new instance starts.

        Simulates a process restart: a fresh KillSwitch bound to the same
        database must load the persisted KILLED state and stay stopped.
        """
        first = KillSwitch(db=isolated_db, persist=True)
        await first.kill(operator="ops-1")

        restarted = KillSwitch(db=isolated_db, persist=True)
        assert restarted.state == KillSwitchState.ACTIVE  # fresh memory
        await restarted.load_state()
        assert restarted.state == KillSwitchState.KILLED
        assert restarted.reason == KILL_SWITCH_REASON
        assert await restarted.is_killed() is True

    @pytest.mark.asyncio
    async def test_resume_persists_active_state(self, isolated_db) -> None:
        ks = KillSwitch(db=isolated_db, persist=True)
        await ks.kill(operator="ops-1")
        await ks.resume(confirm=True, operator="ops-2")

        restarted = KillSwitch(db=isolated_db, persist=True)
        await restarted.load_state()
        assert restarted.state == KillSwitchState.ACTIVE
        assert await restarted.is_killed() is False

    @pytest.mark.asyncio
    async def test_defaults_active_when_nothing_persisted(self, isolated_db) -> None:
        ks = KillSwitch(db=isolated_db, persist=True)
        await ks.load_state()
        assert ks.state == KillSwitchState.ACTIVE


# ── Execution engine gate: no order while KILLED ─────────────────────


class FakeAdapter(ExecutionAdapter):
    """Adapter stub that records every submission attempt."""

    def __init__(self) -> None:
        self.submit_calls: list[dict[str, Any]] = []

    async def submit(self, order: dict[str, Any]) -> dict[str, Any]:
        self.submit_calls.append(order)
        return {
            "order_id": order.get("order_id"),
            "status": "FILLED",
            "filled_size": float(order.get("size", 0)),
            "average_fill": float(order.get("price", 0.5)),
        }

    async def cancel(self, order_id: str) -> bool:
        return True

    async def status(self, order_id: str) -> dict[str, Any]:
        return {"order_id": order_id, "status": "FILLED"}


def _approved_decision() -> RiskDecision:
    return RiskDecision(
        approved=True,
        market_id="mkt_1",
        side="YES",
        size=100.0,
        risk_metrics={"equity": 10000, "entry_price": 0.50, "open_positions": 3},
        signal_id="sig_1",
        net_edge=0.05,
    )


class TestExecutionEngineKillSwitch:
    @pytest.mark.asyncio
    async def test_order_rejected_while_killed(self) -> None:
        adapter = FakeAdapter()
        ks = KillSwitch(persist=False)
        await ks.kill(operator="ops-1")
        engine = ExecutionEngine(adapter, kill_switch=ks)

        result = await engine.execute(_approved_decision())

        assert result.status == "REJECTED"
        assert "Kill switch active" in (result.error or "")
        assert result.requested_size == 0.0
        # The adapter must never have been reached.
        assert adapter.submit_calls == []

    @pytest.mark.asyncio
    async def test_order_submitted_when_active(self) -> None:
        adapter = FakeAdapter()
        ks = KillSwitch(persist=False)
        assert await ks.is_killed() is False
        engine = ExecutionEngine(adapter, kill_switch=ks)

        result = await engine.execute(_approved_decision())

        assert result.status == "FILLED"
        assert len(adapter.submit_calls) == 1

    @pytest.mark.asyncio
    async def test_order_allowed_after_confirmed_resume(self) -> None:
        adapter = FakeAdapter()
        ks = KillSwitch(persist=False)
        await ks.kill()
        # First attempt is blocked.
        assert (await engine_execute(ks, adapter)).status == "REJECTED"
        await ks.resume(confirm=True)
        # After an explicit, confirmed resume new orders are allowed.
        result = await engine_execute(ks, adapter)
        assert result.status == "FILLED"
        assert len(adapter.submit_calls) == 1

    @pytest.mark.asyncio
    async def test_engine_blocks_after_restart_with_same_db(self, isolated_db) -> None:
        """After a restart, a persisted KILLED switch still blocks orders."""
        first = KillSwitch(db=isolated_db, persist=True)
        await first.kill(operator="ops-1")

        restarted = KillSwitch(db=isolated_db, persist=True)
        await restarted.load_state()
        adapter = FakeAdapter()
        engine = ExecutionEngine(adapter, kill_switch=restarted)

        result = await engine.execute(_approved_decision())

        assert result.status == "REJECTED"
        assert adapter.submit_calls == []


async def engine_execute(ks: KillSwitch, adapter: FakeAdapter) -> Any:
    """Convenience helper: build a fresh engine and execute a decision."""
    engine = ExecutionEngine(adapter, kill_switch=ks)
    return await engine.execute(_approved_decision())
