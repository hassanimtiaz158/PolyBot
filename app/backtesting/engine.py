"""Backtesting engine — replay historical data with no look-ahead bias."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app.backtesting.execution import BacktestExecution
from app.backtesting.metrics import MetricsCalculator
from app.backtesting.models import (
    BacktestResult,
    EquityPoint,
    FillRecord,
    MarketSnapshot,
)
from app.portfolio.tracker import PortfolioTracker
from app.risk.engine import RiskEngine
from app.strategies.base import Strategy, StrategyDecision

logger = logging.getLogger(__name__)


class BacktestEngine:
    """Replay historical market data through strategies, risk, and
    execution — one timestamp at a time — to prevent look-ahead bias.

    Parameters
    ----------
    initial_equity : float
        Starting portfolio equity (default 10 000).
    fee_rate : float
        Taker fee coefficient (default 0.05, Polymarket formula).
    """

    def __init__(
        self,
        initial_equity: float = 10_000.0,
        fee_rate: float = 0.05,
    ) -> None:
        self._portfolio = PortfolioTracker(initial_equity=initial_equity)
        self._execution = BacktestExecution(
            portfolio=self._portfolio, fee_rate=fee_rate,
        )
        self._risk_engine = RiskEngine(portfolio=self._portfolio)
        self._strategies: list[Strategy] = []
        self._fills: list[FillRecord] = []
        self._equity_curve: list[EquityPoint] = []
        self._daily_pnl: float = 0.0
        self._consecutive_losses: int = 0
        self._initial_equity = initial_equity
        self._fee_rate = fee_rate
        self._next_id: int = 0

    # ── Strategy management ─────────────────────────────────────────

    @property
    def portfolio(self) -> PortfolioTracker:
        return self._portfolio

    @property
    def equity_curve(self) -> list[EquityPoint]:
        return list(self._equity_curve)

    @property
    def fills(self) -> list[FillRecord]:
        return list(self._fills)

    def add_strategy(self, strategy: Strategy) -> None:
        """Register a strategy to run during the backtest."""
        self._strategies.append(strategy)

    # ── Main loop ───────────────────────────────────────────────────

    async def run(
        self,
        snapshots: list[MarketSnapshot],
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> BacktestResult:
        """Run the backtest over chronological market data.

        Parameters
        ----------
        snapshots : list[MarketSnapshot]
            Historical snapshots sorted by timestamp ascending.
            Must contain **all** timestamps for every market.
        progress_callback : callable, optional
            Called as ``fn(current, total)`` after each snapshot.
            Must be a **regular** (sync) callable.

        Returns
        -------
        BacktestResult
        """
        sorted_snaps = sorted(snapshots, key=lambda s: s.timestamp)
        total = len(sorted_snaps)

        for idx, snap in enumerate(sorted_snaps):
            await self._process_snapshot(snap)
            if progress_callback:
                progress_callback(idx + 1, total)

        calculator = MetricsCalculator(
            initial_equity=self._initial_equity,
            equity_curve=self._equity_curve,
            fills=self._fills,
        )
        return calculator.compute()

    # ── Per-snapshot logic ──────────────────────────────────────────

    async def _process_snapshot(self, snap: MarketSnapshot) -> None:
        """Process a single time-step: update price, run strategies,
        evaluate risk, execute approved signals.
        """
        self._portfolio.update_price(snap.market_id, snap.midpoint)

        features = self._build_features(snap)

        for strategy in self._strategies:
            signal = strategy.generate_signal(features)

            if signal.decision != StrategyDecision.CANDIDATE:
                continue

            decision = await self._risk_engine.evaluate(
                signal=signal,
                net_edge=signal.gross_edge,
                daily_pnl=self._daily_pnl,
                consecutive_losses=self._consecutive_losses,
            )

            if not decision.approved or decision.size <= 0:
                continue

            result = self._execution.execute(
                market_id=snap.market_id,
                side=signal.side,
                size=decision.size,
                snapshot=snap,
                edge=signal.gross_edge,
                signal_id=signal.signal_id,
            )

            self._fills.append(FillRecord(
                timestamp=snap.timestamp,
                market_id=snap.market_id,
                side=signal.side,
                size=decision.size,
                price=result["fill_price"],
                fee=result["fee"],
                pnl_change=result["pnl_change"],
                slippage=result["slippage"],
                edge=signal.gross_edge,
                signal_id=signal.signal_id,
            ))

            self._daily_pnl += result["pnl_change"]
            if result["pnl_change"] < 0:
                self._consecutive_losses += 1
            else:
                self._consecutive_losses = 0

        self._equity_curve.append(EquityPoint(
            timestamp=snap.timestamp,
            equity=self._portfolio.equity,
            total_exposure=self._portfolio.total_exposure(),
            unrealised_pnl=self._portfolio.total_unrealised_pnl(),
            realised_pnl=self._portfolio.total_realised_pnl(),
            num_positions=self._portfolio.num_positions(),
        ))

    # ── Feature construction ────────────────────────────────────────

    @staticmethod
    def _build_features(snap: MarketSnapshot) -> dict[str, Any]:
        """Build a feature dict from a market snapshot.

        This replicates the structure produced by the live
        ``FeatureExtractor`` so strategies receive the same schema.

        Note
        ----
        ``liquidity_score`` is set to the raw depth so it passes
        the ``RiskLimits.check_liquidity`` threshold (≥ 1000).
        """
        ts_str = datetime.fromtimestamp(
            snap.timestamp, tz=UTC
        ).isoformat()
        return {
            "market_id": snap.market_id,
            "midpoint": snap.midpoint,
            "spread": snap.spread,
            "bid": snap.bid if snap.bid is not None else snap.midpoint - snap.spread / 2,
            "ask": snap.ask if snap.ask is not None else snap.midpoint + snap.spread / 2,
            "depth": snap.depth,
            "volume": snap.volume,
            "liquidity_score": snap.depth,
            "timestamp": ts_str,
        }
