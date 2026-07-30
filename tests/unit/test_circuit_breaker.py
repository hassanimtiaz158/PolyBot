"""Unit tests for the circuit breaker module."""

import pytest

from app.risk.circuit_breaker import BreakerState, CircuitBreaker


class TestCircuitBreaker:
    def setup_method(self):
        self.breaker = CircuitBreaker(persist=False)

    def test_initial_state(self):
        assert self.breaker.state == BreakerState.NORMAL

    @pytest.mark.asyncio
    async def test_trigger_soft(self):
        await self.breaker.trigger("SOFT_TRIGGER", severity="SOFT")
        assert self.breaker.state == BreakerState.WARNING
        assert "SOFT_TRIGGER" in self.breaker.reasons

    @pytest.mark.asyncio
    async def test_trigger_hard(self):
        await self.breaker.trigger("HARD_TRIGGER", severity="HARD")
        assert self.breaker.state == BreakerState.HALTED
        assert "HARD_TRIGGER" in self.breaker.reasons

    @pytest.mark.asyncio
    async def test_clear(self):
        await self.breaker.trigger("DAILY_LOSS", severity="HARD")
        assert self.breaker.state == BreakerState.HALTED
        await self.breaker.clear("DAILY_LOSS")
        assert self.breaker.state == BreakerState.NORMAL

    @pytest.mark.asyncio
    async def test_clear_all(self):
        await self.breaker.trigger("DAILY_LOSS", severity="HARD")
        await self.breaker.trigger("STALE_DATA", severity="SOFT")
        await self.breaker.clear_all()
        assert self.breaker.state == BreakerState.NORMAL
        assert self.breaker.reasons == []
