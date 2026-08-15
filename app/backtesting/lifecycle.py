"""Periodic walk-forward validation wired into the application lifecycle.

Run once on construction, then on a configured cadence alongside the
orchestrator event loop.  Emits a ``WALK_FORWARD`` audit event with the
verdict and key metrics, and writes reports to ``backtest_reports/``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from typing import Any

from app.audit.events import EventBus, default_bus
from app.backtesting.walk_forward import WalkForwardReporter, WalkForwardValidator
from app.config.settings import settings
from app.storage.repositories import SnapshotRepository
from app.strategies.microstructure import MicrostructureStrategy

logger = logging.getLogger(__name__)

# ── Helpers ──────────────────────────────────────────────────────────

# ── Public API ────────────────────────────────────────────────────────

class WalkForwardService:
    """Background walk-forward validation service.

    Parameters
    ----------
    snapshot_repo : SnapshotRepository | None
        DB snapshot repository.  When ``None`` the service starts without
        a repo and will always use the synthetic fallback.
    event_bus : EventBus | None
        Audit event bus.  When ``None`` the module-level ``default_bus`` is used.
    interval_seconds : int
        seconds between validation runs (default from ``settings``).
    enabled : bool
        When ``False`` the service does nothing (useful for toggling via
        environment/config without removing the wiring).
    train_size : int
        minimum training snapshots per window (default from ``settings``).
    val_size : int
        validation snapshots per window (default from ``settings``).
    windows : int
        number of validation windows (default from ``settings``).
    fallback_synthetic : bool
        When ``True`` and the DB does not supply enough snapshots,
        synthetic data is used.  When ``False`` the run is skipped with a
        log message.
    """
    def __init__(
        self,
        snapshot_repo: SnapshotRepository | None = None,
        event_bus: EventBus | None = None,
        interval_seconds: int | None = None,
        enabled: bool | None = None,
        train_size: int | None = None,
        val_size: int | None = None,
        windows: int | None = None,
        fallback_synthetic: bool | None = None,
    ) -> None:
        self._snapshot_repo = snapshot_repo
        self._event_bus = event_bus or default_bus
        self._interval = interval_seconds or settings.walk_forward_interval_seconds
        self._enabled = enabled if enabled is not None else settings.walk_forward_enabled
        self._train_size = train_size or settings.walk_forward_train_size
        self._val_size = val_size or settings.walk_forward_val_size
        self._windows = windows or settings.walk_forward_windows
        self._fallback_synthetic = (
            fallback_synthetic if fallback_synthetic is not None
            else settings.walk_forward_fallback_synthetic
        )
        self._running = False
        self._seed = 42

    # ── Public API ────────────────────────────────────────────────────

    async def run_once(self) -> Any | None:
        """Execute a single walk-forward validation cycle."""
        if not self._enabled:
            logger.info("Walk-forward validation disabled per config")
            return None

        min_required = self._train_size + self._windows * self._val_size
        snapshots: list[Any] = []

        # 1️⃣ Try DB first
        if self._snapshot_repo is not None:
            db_snapshots = await self._snapshot_repo.list_recent(
                limit=self._train_size + self._windows * self._val_size + 50,
            )
            if len(db_snapshots) >= min_required:
                # Convert storage snapshots to walk-forward format
                # storage MarketSnapshot → backtesting MarketSnapshot
                converted: list[Any] = []
                for s in db_snapshots[: min_required + 50]:
                    # Use only the fields that backtesting MarketSnapshot accepts
                    ts = (
                        datetime.fromisoformat(s.timestamp.replace("Z", "+00:00"))
                        .replace(tzinfo=UTC)
                        .timestamp()
                        if s.timestamp
                        else 0.0
                    )
                    converted.append(
                        {
                            "timestamp": ts,
                            "market_id": s.market_id,
                            "midpoint": s.midpoint if s.midpoint is not None else 0.5,
                            "spread": s.spread if s.spread is not None else 0.02,
                            "bid": s.bid,
                            "ask": s.ask,
                            "depth": s.bid_depth if s.bid_depth is not None else 500.0,
                            "volume": s.volume if s.volume is not None else 0.0,
                            "obi": 0.0,
                            "velocity_60s": 0.0,
                            "time_to_resolution": s.time_to_resolution
                            if s.time_to_resolution is not None
                            else 3600.0,
                        }
                    )
                snapshots = converted
                logger.info("Walk-forward: loaded %d snapshots from DB", len(snapshots))
            else:
                logger.info(
                    "Walk-forward: only %d DB snapshots (need %d); using synthetic fallback",
                    len(db_snapshots),
                    min_required,
                )

        # 2️⃣ Fallback to synthetic if still short
        if len(snapshots) < min_required:
            if self._fallback_synthetic:
                logger.info("Walk-forward: using synthetic data fallback")
                from app.backtesting.synthetic import generate_synthetic_data

                total = min_required
                snapshots = generate_synthetic_data(
                    num_markets=20,
                    num_snapshots=int(__import__("math").ceil(total / 20)),
                    seed=self._seed,
                )[:total]
            else:
                logger.info(
                    "Walk-forward: insufficient data and fallback disabled; skipping run",
                )
                return None

        # 3️⃣ Run the validator
        strategy_factory = MicrostructureStrategy
        validator = WalkForwardValidator(
            strategy_factory=strategy_factory,
            train_size=self._train_size,
            val_size=self._val_size,
            mode="expanding",
            initial_equity=10_000.0,
            fee_rate=0.05,
            evaluate_in_sample=True,
        )

        progress_logging = True
        report = await validator.run(snapshots, progress_callback=lambda cur, total: logger.info(
            "Walk-forward window %d/%d", cur, total
        ) if progress_logging else None)

        # 4️⃣ Emit audit event
        d = report.diagnostics
        verdict_map = {
            "STABLE": "INFO",
            "SUSPECT": "WARNING",
            "UNSTABLE": "WARNING",
        }
        verdict_severity = verdict_map.get(d.verdict, "WARNING")

        values: dict[str, Any] = {
            "verdict": d.verdict,
            "overfitting": d.overfitting,
            "unstable_parameters": d.unstable_parameters,
            "regime_sensitive": d.regime_sensitive,
            "degradation": d.degradation,
            "single_period_luck": d.single_period_luck,
            "codes": d.codes,
            "reasons": d.reasons,
            "total_pnl": report.total_pnl,
            "total_return": report.total_return,
            "profit_factor": report.profit_factor,
            "win_rate": report.win_rate,
            "expectancy": report.expectancy,
            "calibration_score": report.calibration_score,
            "avg_net_edge": report.avg_net_edge,
            "num_trades": report.num_trades,
            "num_wins": report.num_wins,
            "num_losses": report.num_losses,
            "max_drawdown": report.max_drawdown,
            "max_drawdown_pct": report.max_drawdown_pct,
        }
        event = await self._event_bus.emit(
            "WALK_FORWARD",
            severity=verdict_severity,
            values=values,
        )
        logger.info("Walk-forward audit event emitted: %s (verdict=%s)", event.event_id, d.verdict)

        # 5️⃣ Write reports
        out_label = f"walk_forward_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}"
        output_dir = os.path.join("backtest_reports", out_label)
        reporter = WalkForwardReporter(output_dir=output_dir)
        reporter.generate(report, label="walk_forward")
        logger.info("Walk-forward reports written to %s", output_dir)

        return report

    async def run(self) -> None:
        """Background loop that calls ``run_once`` every ``interval_seconds``."""
        self._running = True
        logger.info(
            "Walk-forward service started (interval=%ds, enabled=%s)",
            self._interval,
            self._enabled,
        )
        try:
            # Run initial cycle immediately, then on the configured interval
            await self.run_once()
            while self._running:
                await asyncio.sleep(self._interval)
                await self.run_once()
        except asyncio.CancelledError:
            logger.info("Walk-forward service cancelled")
        except Exception:  # pragma: no cover
            logger.exception("Walk-forward service error")
        finally:
            self._running = False

    def stop(self) -> None:
        """Signal the background loop to exit at the next iteration."""
        self._running = False
