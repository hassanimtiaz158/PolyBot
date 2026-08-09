"""Unified risk engine — fail-closed gate before execution.

Flow
----
1. Receive a ``CANDIDATE`` signal and portfolio + system state.
2. Check system health: circuit breaker, data freshness, data validity.
3. Check trade parameters: spread, liquidity, net edge, confidence.
4. Check portfolio limits: position size, exposures, daily loss, etc.
5. Check correlated exposure: event, strategy, directional, resolution
   (markets sharing an underlying event or resolution time are one bucket).
6. Compute position size (conservative fixed-risk only).
7. Return a ``RiskDecision`` — single machine-readable ``reason``.

Invariants
----------
* If ``RiskDecision.approved is False``, execution MUST NEVER occur.
* Every rejection includes a machine-readable ``reason`` string.
* No component can bypass the risk engine.
* NaN/Inf values in any numeric field cause immediate rejection.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

from app.portfolio.tracker import PortfolioTracker
from app.risk.circuit_breaker import BreakerState, CircuitBreaker
from app.risk.correlation import CorrelationRegistry, PortfolioRiskLimits
from app.risk.limits import RiskLimits
from app.risk.position_sizing import PositionSizer
from app.strategies.base import Signal, StrategyDecision

logger = logging.getLogger(__name__)


@dataclass
class RiskDecision:
    """Outcome of a single risk-engine evaluation.

    Parameters
    ----------
    approved : bool
        ``True`` when the signal passes **all** gates.
    market_id : str
        Target market.
    side : str
        ``"YES"`` or ``"NO"``.
    size : float
        Final position size (0 when rejected).
    reason : str
        Machine-readable rejection code (empty string when approved).
        Example values: ``"STALE_DATA"``, ``"SYSTEM_HALTED"``,
        ``"SPREAD_TOO_HIGH"``, etc.
    signal_id : str
        Corresponding signal identifier for audit trailing.
    net_edge : float | None
        Net expected value passed through from EV engine.
    risk_metrics : dict
        Snapshot of portfolio risk metrics at evaluation time.
    breaker_state : str | None
        Circuit breaker state at time of evaluation.
    extra : dict
        Arbitrary extra data for downstream consumers.
    """

    approved: bool
    market_id: str
    side: str
    size: float = 0.0
    reason: str = ""
    signal_id: str = ""
    net_edge: float | None = None
    risk_metrics: dict[str, float] = field(default_factory=dict)
    breaker_state: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class RiskEngine:
    """Evaluates candidate signals against all risk gates.

    The engine is **fail-closed**: any check that cannot be definitively
    passed results in rejection with a machine-readable reason.

    Parameters
    ----------
    portfolio : PortfolioTracker
        Tracks equity, positions, and P&L.
    limits : RiskLimits
        Hard limit checker.  Created fresh when ``None``.
    sizer : PositionSizer
        Conservative position size calculator.  Created fresh when ``None``.
    breaker : CircuitBreaker
        Circuit breaker state.  Created fresh when ``None``.
    registry : CorrelationRegistry
        Maps markets to underlying events, directions, and resolution
        times for correlated-exposure checks.  Created fresh when
        ``None`` (every market treated as its own event).
    portfolio_limits : PortfolioRiskLimits
        Portfolio-level concentration limit checker.  Created fresh
        when ``None``.
    """

    def __init__(
        self,
        portfolio: PortfolioTracker,
        limits: RiskLimits | None = None,
        sizer: PositionSizer | None = None,
        breaker: CircuitBreaker | None = None,
        registry: CorrelationRegistry | None = None,
        portfolio_limits: PortfolioRiskLimits | None = None,
    ) -> None:
        self._portfolio = portfolio
        self._limits = limits or RiskLimits()
        self._sizer = sizer or PositionSizer()
        self._breaker = breaker or CircuitBreaker()
        self._registry = registry or CorrelationRegistry()
        self._portfolio_limits = portfolio_limits or PortfolioRiskLimits()

    # ── Public API ───────────────────────────────────────────────────

    async def evaluate(
        self,
        signal: Signal,
        net_edge: float | None = None,
        daily_pnl: float = 0.0,
        consecutive_losses: int = 0,
        api_healthy: bool = True,
        model_available: bool = True,
        database_available: bool = True,
    ) -> RiskDecision:
        """Evaluate a candidate signal through all risk gates.

        Checks are ordered so that system-level failures are caught first
        (fail-fast), followed by trade parameters, then portfolio limits.

        Parameters
        ----------
        signal : Signal
            Strategy output.
        net_edge : float | None
            Net expected value from the EV engine.
        daily_pnl : float
            Cumulative P&L for the current trading day.
        consecutive_losses : int
            Number of consecutive losing trades.
        api_healthy : bool
            Whether the exchange API is reachable.
        model_available : bool
            Whether the probability model is loaded and ready.
        database_available : bool
            Whether the database connection is active.

        Returns
        -------
        RiskDecision
            Fully populated decision.
        """
        # ── 1. Fast-reject NO_SIGNAL ──────────────────────────────────
        if signal.decision != StrategyDecision.CANDIDATE:
            return self._reject(
                signal, "NO_SIGNAL",
                net_edge=net_edge,
            )

        # ── 1b. Validate signal numeric fields ────────────────────────
        # Reject NaN/Inf in any numeric field that could corrupt sizing.
        for field_name, value in [
            ("model_probability", signal.model_probability),
            ("implied_probability", signal.implied_probability),
            ("gross_edge", signal.gross_edge),
            ("confidence", signal.confidence),
            ("net_edge", net_edge),
        ]:
            if value is not None and not math.isfinite(value):
                return self._reject(
                    signal, "INVALID_NUMERIC",
                    net_edge=net_edge,
                    reason_suffix=f"NaN/Inf in {field_name}",
                )

        # ── 2. System health ──────────────────────────────────────────

        if not api_healthy:
            return self._reject(signal, "API_HEALTH", net_edge=net_edge)

        if not model_available:
            return self._reject(signal, "MODEL_UNAVAILABLE", net_edge=net_edge)

        if not database_available:
            return self._reject(signal, "DB_UNAVAILABLE", net_edge=net_edge)

        # Circuit breaker
        breaker_state = self._breaker.state
        if breaker_state == BreakerState.HALTED:
            return self._reject(
                signal, "SYSTEM_HALTED",
                net_edge=net_edge,
                breaker_state=breaker_state.value,
            )

        # Data freshness
        feat = signal.feature_snapshot
        data_check = self._limits.check_data_freshness(
            feat.get("timestamp")
        )
        if not data_check.approved:
            return self._reject(
                signal, data_check.reason,
                net_edge=net_edge,
                breaker_state=breaker_state.value,
            )

        # Data validity
        validity_check = self._limits.check_data_validity(feat)
        if not validity_check.approved:
            return self._reject(
                signal, validity_check.reason,
                net_edge=net_edge,
                breaker_state=breaker_state.value,
            )

        # ── 3. Trade parameters ───────────────────────────────────────

        spread_check = self._limits.check_spread(feat.get("spread"))
        if not spread_check.approved:
            return self._reject(
                signal, spread_check.reason,
                net_edge=net_edge,
                breaker_state=breaker_state.value,
            )

        liquidity_check = self._limits.check_liquidity(
            feat.get("liquidity_score")
        )
        if not liquidity_check.approved:
            return self._reject(
                signal, liquidity_check.reason,
                net_edge=net_edge,
                breaker_state=breaker_state.value,
            )

        edge_check = self._limits.check_net_edge(net_edge)
        if not edge_check.approved:
            return self._reject(
                signal, edge_check.reason,
                net_edge=net_edge,
                breaker_state=breaker_state.value,
            )

        confidence_check = self._limits.check_confidence(signal.confidence)
        if not confidence_check.approved:
            return self._reject(
                signal, confidence_check.reason,
                net_edge=net_edge,
                breaker_state=breaker_state.value,
            )

        # ── 4. Portfolio state ────────────────────────────────────────

        equity = self._portfolio.equity
        total_exposure = self._portfolio.total_exposure()
        open_positions = len(self._portfolio.summary().get("positions", {}))
        market_exposure = self._market_exposure(signal.market_id, signal.side)

        # Compute proposed size
        proposed_size = self._sizer.determine_size(
            equity=equity,
            confidence=signal.confidence or 0.0,
            net_edge=net_edge or 0.0,
            liquidity=feat.get("liquidity_score", float("inf")),
            risk_limit=equity * 0.25,
        )

        # Validate proposed size is finite and positive
        if not math.isfinite(proposed_size) or proposed_size <= 0:
            return self._reject(
                signal, "POSITION_SIZE_ZERO",
                net_edge=net_edge,
                breaker_state=breaker_state.value,
            )

        # Position size check
        size_check = self._limits.check_position_size(proposed_size, equity)
        if not size_check.approved:
            return self._reject(
                signal, size_check.reason,
                net_edge=net_edge,
                breaker_state=breaker_state.value,
                risk_metrics={
                    "equity": equity,
                    "proposed_size": proposed_size,
                    "market_exposure": market_exposure,
                    "total_exposure": total_exposure,
                    "open_positions": float(open_positions),
                    "daily_pnl": daily_pnl,
                    "consecutive_losses": float(consecutive_losses),
                },
            )

        # All portfolio-level checks
        portfolio_check = self._limits.all_checks(
            proposed_size=proposed_size,
            equity=equity,
            market_exposure=market_exposure + proposed_size,
            total_exposure=total_exposure + proposed_size,
            daily_pnl=daily_pnl,
            consecutive_losses=consecutive_losses,
            open_positions=open_positions,
        )
        if not portfolio_check.approved:
            return self._reject(
                signal, portfolio_check.reason,
                net_edge=net_edge,
                breaker_state=breaker_state.value,
                risk_metrics={
                    "equity": equity,
                    "proposed_size": proposed_size,
                    "market_exposure": market_exposure + proposed_size,
                    "total_exposure": total_exposure + proposed_size,
                    "open_positions": float(open_positions),
                    "daily_pnl": daily_pnl,
                    "consecutive_losses": float(consecutive_losses),
                },
            )

        # ── 5. Correlation & portfolio concentration ─────────────────
        # Markets that depend on the same underlying event, or that
        # resolve at the same time, are correlated exposure.  When the
        # combined exposure would exceed the portfolio-level limit the
        # bot prefers NO TRADE over breaking the limit.

        event_id = self._registry.event_for(signal.market_id)
        direction = self._registry.direction_for(signal.market_id)
        event_markets = self._registry.markets_in_event(event_id)
        side_sign = 1.0 if signal.side == "YES" else -1.0

        event_exposure = self._portfolio.exposure_for(event_markets) + proposed_size
        event_reason = self._portfolio_limits.check_event_exposure(
            event_exposure, equity,
        )
        if event_reason:
            return self._reject(
                signal, event_reason,
                net_edge=net_edge,
                breaker_state=breaker_state.value,
                risk_metrics={
                    "equity": equity,
                    "proposed_size": proposed_size,
                    "event_exposure": event_exposure,
                    "strategy_exposure": (
                        self._portfolio.strategy_exposure(signal.strategy)
                        + proposed_size
                    ),
                    "directional_exposure": 0.0,
                    "open_positions": float(open_positions),
                },
            )

        strategy_exposure = (
            self._portfolio.strategy_exposure(signal.strategy) + proposed_size
        )
        strategy_reason = self._portfolio_limits.check_strategy_exposure(
            strategy_exposure, equity,
        )
        if strategy_reason:
            return self._reject(
                signal, strategy_reason,
                net_edge=net_edge,
                breaker_state=breaker_state.value,
                risk_metrics={
                    "equity": equity,
                    "proposed_size": proposed_size,
                    "event_exposure": event_exposure,
                    "strategy_exposure": strategy_exposure,
                    "directional_exposure": 0.0,
                    "open_positions": float(open_positions),
                },
            )

        directional_exposure = (
            self._portfolio.directional_exposure(event_markets)
            + direction * side_sign * proposed_size
        )
        direction_reason = self._portfolio_limits.check_directional_exposure(
            directional_exposure, equity,
        )
        if direction_reason:
            return self._reject(
                signal, direction_reason,
                net_edge=net_edge,
                breaker_state=breaker_state.value,
                risk_metrics={
                    "equity": equity,
                    "proposed_size": proposed_size,
                    "event_exposure": event_exposure,
                    "strategy_exposure": strategy_exposure,
                    "directional_exposure": directional_exposure,
                    "open_positions": float(open_positions),
                },
            )

        resolution_time = self._registry.resolution_time_for(signal.market_id)
        resolution_exposure = 0.0
        if resolution_time is not None:
            resolution_exposure = (
                self._portfolio.exposure_for(
                    self._registry.markets_with_resolution(resolution_time),
                )
                + proposed_size
            )
            resolution_reason = (
                self._portfolio_limits.check_resolution_concentration(
                    resolution_exposure, equity,
                )
            )
            if resolution_reason:
                return self._reject(
                    signal, resolution_reason,
                    net_edge=net_edge,
                    breaker_state=breaker_state.value,
                    risk_metrics={
                        "equity": equity,
                        "proposed_size": proposed_size,
                        "event_exposure": event_exposure,
                        "strategy_exposure": strategy_exposure,
                        "directional_exposure": directional_exposure,
                        "resolution_exposure": resolution_exposure,
                        "open_positions": float(open_positions),
                    },
                )

        # ── Approved ──────────────────────────────────────────────────
        return RiskDecision(
            approved=True,
            market_id=signal.market_id,
            side=signal.side,
            size=proposed_size,
            reason="",
            signal_id=signal.signal_id,
            net_edge=net_edge,
            risk_metrics={
                "equity": equity,
                "proposed_size": proposed_size,
                "proposed_size_pct": (
                    (proposed_size / equity * 100) if equity > 0 else 0.0
                ),
                "market_exposure": market_exposure + proposed_size,
                "total_exposure": total_exposure + proposed_size,
                "open_positions": float(open_positions),
                "daily_pnl": daily_pnl,
                "daily_pnl_pct": (
                    (daily_pnl / equity * 100) if equity > 0 else 0.0
                ),
                "consecutive_losses": float(consecutive_losses),
                "event_exposure": event_exposure,
                "strategy_exposure": strategy_exposure,
                "directional_exposure": directional_exposure,
                "resolution_exposure": resolution_exposure,
            },
            breaker_state=breaker_state.value,
        )

    # ── Private helpers ──────────────────────────────────────────────

    def _reject(
        self,
        signal: Signal,
        reason: str,
        net_edge: float | None = None,
        breaker_state: str | None = None,
        risk_metrics: dict[str, float] | None = None,
        reason_suffix: str = "",
    ) -> RiskDecision:
        """Build a rejected RiskDecision with the given reason."""
        full_reason = f"{reason}: {reason_suffix}" if reason_suffix else reason
        logger.info(
            "RiskEngine rejected %s/%s: %s",
            signal.market_id, signal.side, full_reason,
        )
        return RiskDecision(
            approved=False,
            market_id=signal.market_id,
            side=signal.side,
            size=0.0,
            reason=full_reason,
            signal_id=signal.signal_id,
            net_edge=net_edge,
            risk_metrics=risk_metrics or {},
            breaker_state=breaker_state,
        )

    def _market_exposure(self, market_id: str, side: str) -> float:
        """Return current exposure to a given market."""
        pos = self._portfolio._positions.get(market_id)
        if pos is None:
            return 0.0
        return float(pos.get("size", 0))
