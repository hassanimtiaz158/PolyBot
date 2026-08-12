"""Orchestrator — main trading loop that drives data collection, strategy
evaluation, signal routing, and periodic health checks.

The orchestrator integrates with ``app.main.Application`` to provide
the top-level event loop for the Polymarket Quant Bot.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from app.audit.events import EventBus, default_bus
from app.config.settings import settings
from app.modes.state import ModeState, OperatingMode
from app.monitoring.health import health_status, run_all_checks
from app.orchestrator.pipeline import PipelineResult
from app.orchestrator.router import SignalRouter
from app.risk.circuit_breaker import BreakerState, CircuitBreaker
from app.risk.kill_switch import KILL_SWITCH_REASON, KillSwitch

logger = logging.getLogger(__name__)


class OrchestratorError(Exception):
    """Base exception for orchestrator failures."""


class Orchestrator:
    """Main trading loop that coordinates all subsystems.

    The orchestrator periodically:
    1. Runs health checks.
    2. Evaluates circuit breaker conditions.
    3. Fetches market features via a user-provided callback.
    4. Runs all enabled strategies and routes signals through the pipeline.
    5. Sleeps until the next scan interval.

    Every meaningful state change is emitted as a structured audit event
    via the :class:`EventBus` for full traceability.

    Parameters
    ----------
    router : SignalRouter
        Routes strategy signals through the EV/risk/execution pipeline.
    breaker : CircuitBreaker
        Three-state circuit breaker with SQLite persistence.
    mode : ModeState
        Operating mode state machine.
    kill_switch : KillSwitch | None
        Backend-controlled emergency kill switch.  When provided and
        KILLED, the orchestrator skips signal generation entirely and no
        new orders are produced.
    get_equity : Callable[[], float] | None
        Returns current portfolio equity.  Used by circuit breaker checks.
        Falls back to 10 000 when ``None``.
    data_provider : Callable[[], Any] | None
        Async callback returning ``dict[str, dict[str, Any]]`` mapping
        ``market_id`` → feature dict.  When ``None``, produces empty list.
    scan_interval : int | None
        Seconds between market scans (default from ``settings``).
    event_bus : EventBus | None
        Structured event bus for audit logging.  Uses the module-level
        ``default_bus`` when ``None``.
    """

    def __init__(
        self,
        router: SignalRouter,
        breaker: CircuitBreaker,
        mode: ModeState,
        kill_switch: KillSwitch | None = None,
        get_equity: Callable[[], float] | None = None,
        data_provider: Callable[[], Any] | None = None,
        scan_interval: int | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self._router = router
        self._breaker = breaker
        self._mode = mode
        self._kill_switch = kill_switch
        self._get_equity = get_equity or (lambda: 10_000.0)
        self._data_provider = data_provider
        self._scan_interval = (
            scan_interval or settings.market_scan_interval_seconds
        )
        self._running = False
        self._daily_pnl: float = 0.0
        self._consecutive_losses: int = 0
        self._bus = event_bus or default_bus

    # ── Lifecycle ───────────────────────────────────────────────────

    async def startup(self) -> None:
        """Run initial health checks and log system state."""
        logger.info("Orchestrator starting (mode=%s)", self._mode.mode.value)
        await run_all_checks()
        logger.info(
            "Initial health: all_healthy=%s",
            health_status.all_healthy(),
        )
        await self._bus.emit(
            "SYSTEM_START",
            reason=f"mode={self._mode.mode.value}",
            healthy=health_status.all_healthy(),
        )
        if self._mode.mode == OperatingMode.HALTED:
            logger.warning("System starts in HALTED — operator must transition")

    async def shutdown(self) -> None:
        """Gracefully stop the orchestrator."""
        logger.info("Orchestrator shutting down")
        await self._bus.emit("SYSTEM_STOP", reason="graceful shutdown")
        self._running = False

    async def run(self) -> None:
        """Main event loop — runs until ``stop()`` is called or interrupted."""
        self._running = True
        await self.startup()
        try:
            while self._running:
                try:
                    await self._run_iteration()
                except Exception as exc:
                    logger.exception("Iteration failed: %s", exc)
                await asyncio.sleep(self._scan_interval)
        except asyncio.CancelledError:
            logger.info("Orchestrator cancelled")
        finally:
            await self.shutdown()

    def stop(self) -> None:
        """Signal the main loop to exit at the next opportunity."""
        self._running = False

    # ── Per-iteration logic ─────────────────────────────────────────

    async def _run_iteration(self) -> None:
        """Execute a single scan iteration."""
        if not self._running:
            return

        await run_all_checks()

        previous_state = self._breaker.state

        await self._breaker.check_and_trigger(
            daily_pnl=self._daily_pnl,
            consecutive_losses=self._consecutive_losses,
            data_fresh=health_status.is_healthy("data_freshness"),
            api_healthy=health_status.is_healthy("api"),
            equity=self._get_equity(),
        )

        # Emit circuit breaker event on state change
        if self._breaker.state != previous_state:
            await self._bus.emit(
                "CIRCUIT_BREAKER",
                decision=self._breaker.state.value,
                reason=", ".join(self._breaker.reasons) or "state change",
                previous_state=previous_state.value if previous_state else None,
                daily_pnl=self._daily_pnl,
                consecutive_losses=self._consecutive_losses,
            )

        if self._breaker.state == BreakerState.HALTED:
            logger.warning("Skipping iteration — circuit breaker HALTED")
            return

        if self._kill_switch is not None and await self._kill_switch.is_killed():
            logger.warning(
                "Skipping iteration — kill switch active (%s)",
                KILL_SWITCH_REASON,
            )
            return

        market_features: dict[str, dict[str, Any]] = {}
        if self._data_provider is not None:
            try:
                data = await self._data_provider()
                if isinstance(data, dict):
                    market_features = data
                    for market_id in market_features:
                        await self._bus.data_received(market_id=market_id)
            except Exception as exc:
                logger.exception("Data provider failed: %s", exc)
                # Trigger circuit breaker on data provider failure
                await self._breaker.trigger("DATA_PROVIDER_ERROR", severity="SOFT")
                await self._bus.emit(
                    "DATA_STALE",
                    reason=f"data provider error: {exc}",
                )
                return

        # Emit stale data event if data freshness check failed
        # (skip if already halted to avoid redundant alerts)
        if not health_status.is_healthy("data_freshness"):
            state_val = str(self._breaker.state)
            if state_val != "HALTED":
                await self._bus.emit(
                    "DATA_STALE",
                    reason="data freshness check failed",
                )

        all_results: list[PipelineResult] = []
        for market_id, features in market_features.items():
            results = await self._router.route_all(
                market_id=market_id,
                features=features,
                daily_pnl=self._daily_pnl,
                consecutive_losses=self._consecutive_losses,
            )
            all_results.extend(results)

        self._update_tracking(all_results)

        trade_count = sum(
            1 for r in all_results if r.order_result is not None
        )
        if trade_count > 0:
            logger.info(
                "Iteration: %d signals, %d trades",
                len(all_results), trade_count,
            )

    # ── Tracking helpers ────────────────────────────────────────────

    def _update_tracking(self, results: list[PipelineResult]) -> None:
        """Update daily P&L and consecutive loss tracking from fills."""
        for result in results:
            if result.order_result is None:
                continue
            order = result.order_result
            if order.status not in ("FILLED", "PARTIALLY_FILLED"):
                continue
            if order.average_fill is None or order.filled_size <= 0:
                continue
            entry = order.average_fill
            if order.side == "YES":
                pnl = order.filled_size * (0.50 - entry)
            else:
                pnl = order.filled_size * (entry - 0.50)
            self._daily_pnl += pnl
            if pnl < 0:
                self._consecutive_losses += 1
            else:
                self._consecutive_losses = 0

    def reset_daily_tracking(self) -> None:
        """Reset daily P&L and consecutive losses (e.g. at market open)."""
        self._daily_pnl = 0.0
        self._consecutive_losses = 0
        logger.info("Daily tracking reset")
