"""Unit tests for the circuit breaker module."""

import pytest

from app.risk.circuit_breaker import CircuitBreaker


class TestCircuitBreaker:
    def setup_method(self):
        self.breaker = CircuitBreaker()

    def test_initial_state(self):
        assert not self.breaker.is_halted

    @pytest.mark.asyncio
    async def test_trigger(self):
        await self.breaker.trigger("DAILY_LOSS")
        assert self.breaker.is_halted
        assert "DAILY_LOSS" in self.breaker.active_breakers

    @pytest.mark.asyncio
    async def test_clear(self):
        await self.breaker.trigger("DAILY_LOSS")
        await self.breaker.clear("DAILY_LOSS")
        assert not self.breaker.is_halted

    @pytest.mark.asyncio
    async def test_clear_all(self):
        await self.breaker.trigger("DAILY_LOSS")
        await self.breaker.trigger("STALE_DATA")
        await self.breaker.clear_all()
        assert not self.breaker.is_halted
