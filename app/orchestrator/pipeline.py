"""Trade pipeline — wires EV engine → risk engine → execution → storage.

Every signal from a strategy passes through this pipeline in sequence.
At each stage the pipeline may reject the opportunity, in which case
execution is skipped.  Persistence is incremental: signals and risk
events are stored even when the trade is rejected.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.config.settings import settings
from app.ev.costs import DEFAULT_FEE_RATE
from app.ev.expected_value import EVResult, ExpectedValueEngine
from app.execution.engine import ExecutionEngine, OrderResult
from app.monitoring.health import health_status
from app.portfolio.tracker import PortfolioTracker
from app.risk.engine import RiskDecision, RiskEngine
from app.storage.models import Order as OrderModel
from app.storage.models import RiskEvent
from app.storage.models import Signal as SignalModel
from app.storage.repositories import (
    OrderRepository,
    PositionRepository,
    RiskEventRepository,
    SignalRepository,
)
from app.strategies.base import Signal, StrategyDecision

logger = logging.getLogger(__name__)

_DEFAULT_NOMINAL_SIZE = 100.0


@dataclass
class PipelineResult:
    """Complete outcome of a single signal passing through the pipeline.

    Every stage is optional — a rejection at EV or Risk will leave
    downstream fields as ``None``.
    """

    signal: Signal
    ev_result: EVResult | None = None
    risk_decision: RiskDecision | None = None
    order_result: OrderResult | None = None
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class TradePipeline:
    """Sequential pipeline: EV evaluation → risk check → execution → storage.

    Parameters
    ----------
    ev_engine : ExpectedValueEngine
        Computes gross and net expected value.
    risk_engine : RiskEngine
        Fail-closed risk gate.
    exec_engine : ExecutionEngine
        Order submission and lifecycle.
    portfolio : PortfolioTracker
        Tracks equity, positions, and P&L.
    signal_repo : SignalRepository
        Persists signal records.
    order_repo : OrderRepository
        Persists order records.
    position_repo : PositionRepository
        Persists position records.
    risk_repo : RiskEventRepository
        Persists risk events.
    """

    def __init__(
        self,
        ev_engine: ExpectedValueEngine,
        risk_engine: RiskEngine,
        exec_engine: ExecutionEngine,
        portfolio: PortfolioTracker,
        signal_repo: SignalRepository,
        order_repo: OrderRepository,
        position_repo: PositionRepository,
        risk_repo: RiskEventRepository,
    ) -> None:
        self._ev = ev_engine
        self._risk = risk_engine
        self._exec = exec_engine
        self._portfolio = portfolio
        self._signal_repo = signal_repo
        self._order_repo = order_repo
        self._position_repo = position_repo
        self._risk_repo = risk_repo

    async def run(
        self,
        signal: Signal,
        features: dict[str, Any],
        fee_rate: float = DEFAULT_FEE_RATE,
        daily_pnl: float = 0.0,
        consecutive_losses: int = 0,
    ) -> PipelineResult:
        """Run the full pipeline for a single strategy signal.

        Parameters
        ----------
        signal : Signal
            Strategy output (must be ``CANDIDATE`` to proceed).
        features : dict
            Feature snapshot for cost and risk estimation.
        fee_rate : float
            Polymarket fee coefficient Θ (default 0.05).
        daily_pnl : float
            Cumulative P&L for the current trading day.
        consecutive_losses : int
            Number of consecutive losing trades.

        Returns
        -------
        PipelineResult
            Full pipeline result with all stage outputs.
        """
        if signal.decision != StrategyDecision.CANDIDATE:
            return PipelineResult(signal=signal)

        net_edge: float | None = None
        ev_result: EVResult | None = None

        # ── Stage 1: Expected Value ──────────────────────────────────
        try:
            price = features.get("midpoint", signal.implied_probability or 0.5)
            spread = features.get("spread", 0.0)
            depth = features.get("liquidity_score", _DEFAULT_NOMINAL_SIZE)
            nominal_size = self._nominal_size()

            ev_result = self._ev.evaluate(
                model_probability=signal.model_probability or 0.5,
                implied_probability=signal.implied_probability or 0.5,
                spread=spread,
                price=price,
                size=nominal_size,
                depth=depth,
                confidence=signal.confidence or 0.0,
                fee_rate=fee_rate,
            )
            net_edge = ev_result.net_edge if ev_result.tradeable else None
        except Exception as exc:
            logger.exception("EV engine failed for %s/%s", signal.market_id, signal.side)
            result = PipelineResult(signal=signal, error=f"EV engine error: {exc}")
            await self._persist_signal(signal, net_edge=None)
            return result

        # ── Stage 2: Risk Engine ─────────────────────────────────────
        risk_decision: RiskDecision | None = None
        try:
            risk_decision = await self._risk.evaluate(
                signal=signal,
                net_edge=net_edge,
                daily_pnl=daily_pnl,
                consecutive_losses=consecutive_losses,
                api_healthy=health_status.is_healthy("api"),
                model_available=health_status.is_healthy("model_availability"),
                database_available=health_status.is_healthy("database"),
            )
        except Exception as exc:
            logger.exception("Risk engine failed for %s/%s", signal.market_id, signal.side)
            result = PipelineResult(
                signal=signal,
                ev_result=ev_result,
                error=f"Risk engine error: {exc}",
            )
            await self._persist_signal(signal, net_edge=net_edge)
            return result

        if not risk_decision.approved:
            result = PipelineResult(
                signal=signal,
                ev_result=ev_result,
                risk_decision=risk_decision,
            )
            await self._persist_signal(signal, net_edge=net_edge)
            await self._persist_risk_event(risk_decision)
            return result

        # ── Stage 3: Execution ───────────────────────────────────────
        order_result: OrderResult | None = None
        try:
            order_result = await self._exec.execute(risk_decision)
        except Exception as exc:
            logger.exception("Execution failed for %s/%s", signal.market_id, signal.side)
            result = PipelineResult(
                signal=signal,
                ev_result=ev_result,
                risk_decision=risk_decision,
                error=f"Execution error: {exc}",
            )
            await self._persist_signal(signal, net_edge=net_edge)
            await self._persist_risk_event(risk_decision)
            return result

        # ── Persist everything ───────────────────────────────────────
        await self._persist_signal(signal, net_edge=net_edge)
        await self._persist_risk_event(risk_decision)
        await self._persist_order(order_result)
        await self._update_portfolio(order_result)

        logger.info(
            "Pipeline complete: %s/%s size=%.2f filled=%.2f status=%s",
            signal.market_id,
            signal.side,
            risk_decision.size,
            order_result.filled_size,
            order_result.status,
        )

        return PipelineResult(
            signal=signal,
            ev_result=ev_result,
            risk_decision=risk_decision,
            order_result=order_result,
        )

    # ── Private helpers ─────────────────────────────────────────────

    def _nominal_size(self) -> float:
        equity = self._portfolio.equity
        if equity <= 0:
            return _DEFAULT_NOMINAL_SIZE
        return equity * settings.max_position_pct

    async def _persist_signal(
        self, signal: Signal, net_edge: float | None
    ) -> None:
        try:
            await self._signal_repo.insert(
                SignalModel(
                    signal_id=signal.signal_id,
                    market_id=signal.market_id,
                    strategy=signal.strategy,
                    side=signal.side,
                    decision=signal.decision.value,
                    model_probability=signal.model_probability,
                    implied_probability=signal.implied_probability,
                    gross_edge=signal.gross_edge,
                    estimated_cost=None,
                    net_edge=net_edge,
                    confidence=signal.confidence,
                    rejection_reason=signal.reason or None,
                    timestamp=signal.timestamp,
                )
            )
        except Exception:
            logger.exception("Failed to persist signal %s", signal.signal_id)

    async def _persist_risk_event(self, decision: RiskDecision) -> None:
        if decision.approved:
            return
        try:
            await self._risk_repo.insert(
                RiskEvent(
                    event_id=f"risk_{decision.signal_id or 'unknown'}",
                    event_type=decision.reason or "REJECTED",
                    severity="HIGH" if decision.reason in (
                        "SYSTEM_HALTED", "DAILY_LOSS_LIMIT_REACHED",
                        "CONSECUTIVE_LOSS_LIMIT_REACHED",
                    ) else "MEDIUM",
                    details=f"{decision.market_id}/{decision.side}: {decision.reason}",
                )
            )
        except Exception:
            logger.exception("Failed to persist risk event")

    async def _persist_order(self, result: OrderResult) -> None:
        try:
            await self._order_repo.insert(
                OrderModel(
                    order_id=result.order_id,
                    market_id=result.market_id,
                    side=result.side,
                    status=result.status,
                    requested_price=result.average_fill,
                    requested_size=result.requested_size,
                    filled_size=result.filled_size,
                    average_fill=result.average_fill,
                )
            )
        except Exception:
            logger.exception("Failed to persist order %s", result.order_id)

    async def _update_portfolio(self, result: OrderResult) -> None:
        if result.filled_size <= 0:
            return
        try:
            self._portfolio.update_position({
                "market_id": result.market_id,
                "side": result.side,
                "size": result.filled_size,
                "average_entry": result.average_fill,
                "current_price": result.average_fill,
                "realised_pnl": 0.0,
                "unrealised_pnl": 0.0,
            })
        except Exception:
            logger.exception("Failed to update portfolio for %s", result.market_id)
