"""Application orchestrator.

Wires all components together: strategies, EV engine, risk engine,
execution engine, and the main trading loop via the Orchestrator.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.audit.events import EventBus
from app.backtesting.lifecycle import WalkForwardService
from app.config.settings import settings
from app.ev.costs import CostEstimator
from app.ev.expected_value import ExpectedValueEngine
from app.execution.engine import ExecutionEngine
from app.execution.paper import PaperExecution
from app.execution.polymarket import PolymarketExecution
from app.modes.state import ModeState, OperatingMode
from app.monitoring.health import health_status, run_all_checks
from app.orchestrator.engine import Orchestrator
from app.orchestrator.pipeline import TradePipeline
from app.orchestrator.router import SignalRouter
from app.portfolio.tracker import PortfolioTracker
from app.reconciliation.reconciler import Reconciler
from app.risk.circuit_breaker import CircuitBreaker
from app.risk.engine import RiskEngine
from app.risk.kill_switch import KillSwitch
from app.risk.limits import RiskLimits
from app.risk.position_sizing import PositionSizer
from app.storage.db import db
from app.storage.repositories import (
    OrderRepository,
    PositionRepository,
    RiskEventRepository,
    SignalRepository,
    SnapshotRepository,
)
from app.strategies.arbitrage import ArbitrageStrategy
from app.strategies.ensemble import EnsembleStrategy
from app.strategies.microstructure import MicrostructureStrategy
from app.strategies.probability import ProbabilityStrategy

logger = logging.getLogger(__name__)


class Application:
    """Top-level application orchestrator.

    Creates all subsystem instances, wires them together, and runs the
    main trading loop.
    """

    def __init__(self) -> None:
        self.mode_mode: ModeState = ModeState(
            initial_mode=OperatingMode(settings.mode)
            if settings.mode in {m.value for m in OperatingMode}
            else OperatingMode.HALTED
        )
        self._orchestrator: Orchestrator | None = None
        self._event_bus: EventBus | None = None

    async def startup(self) -> None:
        """Initialise database, health checks, and the orchestrator."""
        logger.info("Starting Polymarket Quant Bot", extra={"mode": self.mode_mode.mode.value})
        await db.init_schema()
        await run_all_checks()

        # ── Event bus ──────────────────────────────────────────────
        risk_repo = RiskEventRepository()
        self._event_bus = EventBus(repo=risk_repo)

        # ── Core components ─────────────────────────────────────────
        portfolio = PortfolioTracker()
        cost_estimator = CostEstimator()
        ev_engine = ExpectedValueEngine(cost_estimator=cost_estimator)
        risk_limits = RiskLimits()
        position_sizer = PositionSizer()
        circuit_breaker = CircuitBreaker()

        # Wire event bus into circuit breaker for CIRCUIT_BREAKER events
        circuit_breaker.set_event_bus(self._event_bus)

        # ── Emergency kill switch ────────────────────────────────
        # Persisted to SQLite; loading KILLED keeps trading stopped
        # across restarts until an operator explicitly resumes.
        kill_switch = KillSwitch(db=db, event_bus=self._event_bus)
        await kill_switch.load_state()

        risk_engine = RiskEngine(
            portfolio=portfolio,
            limits=risk_limits,
            sizer=position_sizer,
            breaker=circuit_breaker,
        )

        # ── Repositories ─────────────────────────────────────────────
        signal_repo = SignalRepository()
        order_repo = OrderRepository()
        position_repo = PositionRepository()

        # ── Execution adapter ─────────────────────────────────────────
        # Live orders are only ever routed to PolymarketExecution when the
        # operator has explicitly set mode=LIVE_GUARDED AND
        # LIVE_TRADING_ENABLED=true. Every other mode (including an
        # unrecognised one) falls back to paper execution — this must
        # fail closed, never fail open into live trading.
        if (
            self.mode_mode.mode is OperatingMode.LIVE_GUARDED
            and settings.live_trading_enabled
        ):
            adapter: PaperExecution | PolymarketExecution = PolymarketExecution(
                portfolio=portfolio,
                order_repo=order_repo,
                breaker=circuit_breaker,
                kill_switch=kill_switch,
            )
            logger.warning(
                "LIVE execution adapter active — real orders may be submitted"
            )
            # Every order submission is gated on _account_state.is_valid;
            # without this the adapter would reject every single order
            # with "Account state not reconciled" forever.
            live_recon = await adapter.reconcile()
            if not live_recon.success:
                logger.error(
                    "Live account reconciliation failed: %s — "
                    "all live orders will be rejected until this succeeds",
                    live_recon.error,
                )
        else:
            adapter = PaperExecution(
                rejection_rate=0.01,
                latency_ms=200.0,
                fee_rate=0.05,
            )
        exec_engine = ExecutionEngine(
            adapter=adapter,
            kill_switch=kill_switch,
            event_bus=self._event_bus,
        )

        # ── Pipeline ─────────────────────────────────────────────────
        pipeline = TradePipeline(
            ev_engine=ev_engine,
            risk_engine=risk_engine,
            exec_engine=exec_engine,
            portfolio=portfolio,
            signal_repo=signal_repo,
            order_repo=order_repo,
            position_repo=position_repo,
            risk_repo=risk_repo,
            event_bus=self._event_bus,
        )

        # ── Router ───────────────────────────────────────────────────
        router = SignalRouter(pipeline=pipeline, mode=self.mode_mode)

        # Register strategies
        router.register_strategy("microstructure", MicrostructureStrategy(), enabled=True)
        router.register_strategy("arbitrage", ArbitrageStrategy(), enabled=True)
        router.register_strategy("probability", ProbabilityStrategy(), enabled=False)
        router.register_strategy("ensemble", EnsembleStrategy(), enabled=False)

        # ── Orchestrator ─────────────────────────────────────────────
        self._orchestrator = Orchestrator(
            router=router,
            breaker=circuit_breaker,
            mode=self.mode_mode,
            kill_switch=kill_switch,
            get_equity=lambda: portfolio.equity,
            data_provider=None,
            event_bus=self._event_bus,
        )

        # ── Walk-forward validation service ────────────────────────
        snapshot_repo = SnapshotRepository(db=db)
        self._walk_forward = WalkForwardService(
            snapshot_repo=snapshot_repo,
            event_bus=self._event_bus,
            interval_seconds=settings.walk_forward_interval_seconds,
            enabled=settings.walk_forward_enabled,
            train_size=settings.walk_forward_train_size,
            val_size=settings.walk_forward_val_size,
            windows=settings.walk_forward_windows,
            fallback_synthetic=settings.walk_forward_fallback_synthetic,
        )

        # Load persisted circuit breaker state
        await circuit_breaker.load_state()

        # ── Reconciliation ─────────────────────────────────────────
        reconciler = Reconciler(
            order_repo=order_repo,
            position_repo=position_repo,
        )
        recon_result = await reconciler.reconcile_all()
        if not recon_result.is_clean:
            logger.warning(
                "Reconciliation found discrepancies: %s — "
                "review before enabling live trading",
                recon_result.summary(),
            )
            if self._event_bus is not None:
                await self._event_bus.emit(
                    "RISK_REJECTED",
                    reason="RECONCILIATION_DISCREPANCIES",
                    orders_missing=len(recon_result.orders_missing),
                    orders_status_mismatch=len(recon_result.orders_status_mismatch),
                    positions_missing=len(recon_result.positions_missing),
                )

        logger.info(
            "Startup complete — %d strategies registered, mode=%s, healthy=%s",
            len(router.registered_strategies),
            self.mode_mode.mode.value,
            health_status.all_healthy(),
        )

    async def shutdown(self) -> None:
        """Gracefully shut down all subsystems."""
        logger.info("Shutting down")
        if self._orchestrator is not None:
            self._orchestrator.stop()
        if self._walk_forward is not None:
            self._walk_forward.stop()
        await db.close()
        if self._event_bus is not None:
            await self._event_bus.emit("SYSTEM_STOP", reason="application shutdown")
        logger.info("Shutdown complete")

    async def run(self) -> None:
        """Main entry point — runs the trading loop until interrupted."""
        await self.startup()
        tasks: list[asyncio.Task[Any]] = []
        if self._orchestrator is not None:
            tasks.append(asyncio.create_task(self._orchestrator.run()))
        if self._walk_forward is not None:
            tasks.append(asyncio.create_task(self._walk_forward.run()))
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("Application cancelled")
        except Exception as exc:
            logger.exception("Application exited with error: %s", exc)
        await self.shutdown()


def main() -> None:
    """CLI entry point for the Polymarket Quant Bot."""
    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    app = Application()
    asyncio.run(app.run())


if __name__ == "__main__":
    main()
