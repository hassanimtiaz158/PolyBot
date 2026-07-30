"""Signal router — manages active strategies and routes signals through the pipeline.

The router is the bridge between strategy evaluation and the trade pipeline.
It decides which strategies are active, passes market features to them,
and forwards candidate signals to the pipeline for EV/risk/execution.
"""

from __future__ import annotations

import logging
from typing import Any

from app.modes.state import ModeState
from app.orchestrator.pipeline import PipelineResult, TradePipeline
from app.strategies.base import Signal, Strategy, StrategyDecision

logger = logging.getLogger(__name__)


class SignalRouter:
    """Manages active strategies and routes signals through the pipeline.

    Parameters
    ----------
    pipeline : TradePipeline
        Downstream EV/risk/execution pipeline.
    mode : ModeState
        Operating mode — only ``PAPER`` and ``LIVE_GUARDED`` permit trading.
    """

    def __init__(
        self,
        pipeline: TradePipeline,
        mode: ModeState,
    ) -> None:
        self._pipeline = pipeline
        self._mode = mode
        self._strategies: dict[str, Strategy] = {}
        self._enabled: dict[str, bool] = {}

    # ── Strategy management ─────────────────────────────────────────

    def register_strategy(
        self, name: str, strategy: Strategy, enabled: bool = True
    ) -> None:
        """Register a strategy for signal routing.

        Parameters
        ----------
        name : str
            Unique strategy name (overwrites any existing registration).
        strategy : Strategy
            Strategy instance.
        enabled : bool
            Whether the strategy is active for signal generation.
        """
        self._strategies[name] = strategy
        self._enabled[name] = enabled
        logger.info("Registered strategy '%s' (enabled=%s)", name, enabled)

    def enable_strategy(self, name: str) -> None:
        """Enable a previously registered strategy."""
        if name in self._strategies:
            self._enabled[name] = True
            logger.info("Enabled strategy '%s'", name)

    def disable_strategy(self, name: str) -> None:
        """Disable a strategy without removing it."""
        if name in self._strategies:
            self._enabled[name] = False
            logger.info("Disabled strategy '%s'", name)

    def is_enabled(self, name: str) -> bool:
        """Check whether a strategy is currently enabled."""
        return self._enabled.get(name, False)

    @property
    def active_strategies(self) -> list[str]:
        """Return names of all enabled strategies."""
        return [n for n, e in self._enabled.items() if e]

    @property
    def registered_strategies(self) -> dict[str, Strategy]:
        """Return all registered strategies (read-only view)."""
        return dict(self._strategies)

    # ── Routing ─────────────────────────────────────────────────────

    async def route_all(
        self,
        market_id: str,
        features: dict[str, Any],
        context: dict[str, Any] | None = None,
        fee_rate: float = 0.05,
        daily_pnl: float = 0.0,
        consecutive_losses: int = 0,
    ) -> list[PipelineResult]:
        """Run all enabled strategies for a single market and route signals.

        Mode-gating is applied: only ``PAPER`` and ``LIVE_GUARDED`` modes
        permit execution.  In ``RESEARCH`` and ``BACKTEST`` modes, signals
        are still generated and persisted but never executed.

        Parameters
        ----------
        market_id : str
            Target market identifier.
        features : dict
            Computed feature dictionary for the market.
        context : dict | None
            Optional strategy-specific context.
        fee_rate : float
            Polymarket fee coefficient Θ.
        daily_pnl : float
            Cumulative daily P&L for risk checks.
        consecutive_losses : int
            Consecutive losing trades for risk checks.

        Returns
        -------
        list[PipelineResult]
            Results for every enabled strategy (including NO_SIGNAL).
        """
        results: list[PipelineResult] = []
        can_trade = self._mode.is_trading()

        for name in self.active_strategies:
            strategy = self._strategies[name]
            try:
                signal = strategy.generate_signal(features, context)
            except Exception as exc:
                logger.exception("Strategy '%s' failed for %s", name, market_id)
                results.append(
                    PipelineResult(
                        signal=Signal(
                            strategy=name,
                            market_id=market_id,
                            side="NO_SIDE",
                            decision=StrategyDecision.NO_SIGNAL,
                            reason=f"Strategy error: {exc}",
                        ),
                        error=f"Strategy error: {exc}",
                    )
                )
                continue

            if signal.decision != StrategyDecision.CANDIDATE:
                results.append(PipelineResult(signal=signal))
                continue

            if not can_trade:
                logger.info(
                    "Signal %s/%s gated by mode %s",
                    market_id, signal.side, self._mode.mode.value,
                )
                results.append(
                    PipelineResult(
                        signal=signal,
                        error=f"Gated by mode: {self._mode.mode.value}",
                    )
                )
                continue

            result = await self._pipeline.run(
                signal=signal,
                features=features,
                fee_rate=fee_rate,
                daily_pnl=daily_pnl,
                consecutive_losses=consecutive_losses,
            )
            results.append(result)

        return results
