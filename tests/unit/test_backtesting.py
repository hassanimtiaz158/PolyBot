"""Deterministic tests for the backtesting framework.

Uses tiny synthetic datasets with known expected outcomes to verify
correctness of the engine, execution, metrics, and reports.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Any

import pytest

from app.backtesting.engine import BacktestEngine
from app.backtesting.metrics import MetricsCalculator
from app.backtesting.models import (
    BacktestResult,
    EquityPoint,
    FillRecord,
    MarketSnapshot,
)
from app.backtesting.report import ReportGenerator
from app.config.settings import settings
from app.strategies.base import Signal, Strategy, StrategyDecision

# ── Helpers ──────────────────────────────────────────────────────────

EPS = 1e-6


def approx(val: float) -> Any:
    return pytest.approx(val, rel=EPS, abs=1e-12)


def synthetic_market(
    prices: list[float],
    market_id: str = "btc_up",
    spread: float = 0.02,
    depth: float = 1_000_000.0,
) -> list[MarketSnapshot]:
    """Build a chronological list of ``MarketSnapshot`` from prices.

    Timestamps use ``time.time()`` fresh per call so the data-freshness
    check in ``RiskEngine`` (``data_max_age_seconds = 5``) passes.
    """
    base = time.time()
    return [
        MarketSnapshot(
            timestamp=base + float(i * 60),
            market_id=market_id,
            midpoint=p,
            spread=spread,
            bid=p - spread / 2,
            ask=p + spread / 2,
            depth=depth,
        )
        for i, p in enumerate(prices)
    ]


# ── Test strategies ──────────────────────────────────────────────────


class StepStrategy(Strategy):
    """Strategy that generates a CANDIDATE only at given step indices.

    Useful for single-direction signals (always same side).
    """

    name = "step_strategy"

    def __init__(
        self,
        signal_at: list[int],
        side: str = "YES",
        model_prob: float = 0.60,
        implied_prob: float = 0.50,
    ) -> None:
        super().__init__(min_confidence=0.0, min_liquidity_score=0.0)
        self._signal_at = set(signal_at)
        self._side = side
        self._model_prob = model_prob
        self._implied_prob = implied_prob
        self._step = -1

    def generate_signal(
        self,
        features: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> Signal:
        self._step += 1
        market_id = features.get("market_id", "unknown")

        if self._step in self._signal_at:
            return self._candidate(
                market_id=market_id,
                side=self._side,
                model_probability=self._model_prob,
                implied_probability=self._implied_prob,
                confidence=0.80,
                reason=f"step_{self._step}",
                feature_snapshot=features,
            )

        return self._reject(market_id, f"skip_{self._step}")


class OpenCloseStrategy(Strategy):
    """Strategy that opens at ``open_at`` and closes at ``close_at``.

    Uses opposite-side close signal so ``PortfolioTracker`` reduces
    the open position and realises P&L.

    If ``market`` is provided the strategy only generates signals
    when the current snapshot matches that market.
    """

    name = "open_close_strategy"

    def __init__(
        self,
        open_at: int,
        close_at: int,
        side: str = "YES",
        model_prob: float = 0.60,
        implied_prob: float = 0.50,
        market: str | None = None,
    ) -> None:
        super().__init__(min_confidence=0.0, min_liquidity_score=0.0)
        self._open_at = open_at
        self._close_at = close_at
        self._side = side
        self._model_prob = model_prob
        self._implied_prob = implied_prob
        self._target_market = market
        self._step = -1
        self._opened = False

    def generate_signal(
        self,
        features: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> Signal:
        self._step += 1
        market_id = features.get("market_id", "unknown")

        # Only act on the target market if one is specified
        if self._target_market is not None and market_id != self._target_market:
            return self._reject(market_id, "wrong_market")

        if self._step == self._open_at and not self._opened:
            self._opened = True
            return self._candidate(
                market_id=market_id,
                side=self._side,
                model_probability=self._model_prob,
                implied_probability=self._implied_prob,
                confidence=0.80,
                reason=f"open_{self._step}",
                feature_snapshot=features,
            )

        if self._step == self._close_at and self._opened:
            close_side = "NO" if self._side == "YES" else "YES"
            self._opened = False
            return self._candidate(
                market_id=market_id,
                side=close_side,
                model_probability=self._model_prob,
                implied_probability=self._implied_prob,
                confidence=0.80,
                reason=f"close_{self._step}",
                feature_snapshot=features,
            )

        return self._reject(market_id, f"skip_{self._step}")


# ── Tests ────────────────────────────────────────────────────────────


class TestBacktestEngine:
    """Verify the engine loop, look-ahead isolation, and state."""

    @pytest.mark.asyncio
    async def test_empty_no_strategies(self) -> None:
        """No strategies registered → no trades, equity unchanged."""
        snaps = synthetic_market([0.50, 0.55, 0.60])
        engine = BacktestEngine(initial_equity=10_000.0, fee_rate=0.0)
        result = await engine.run(snaps)

        assert result.num_trades == 0
        assert result.final_equity == approx(10_000.0)
        assert result.total_pnl == approx(0.0)
        assert len(result.equity_curve) == 3

    @pytest.mark.asyncio
    async def test_open_and_close(self) -> None:
        """Buy YES at t=0 (mid 0.50), close by buying NO at t=4 (mid 0.60)."""
        snaps = synthetic_market([0.50, 0.52, 0.55, 0.58, 0.60, 0.58, 0.55, 0.52])

        engine = BacktestEngine(initial_equity=10_000.0, fee_rate=0.0)
        engine.add_strategy(OpenCloseStrategy(open_at=0, close_at=4, side="YES"))

        result = await engine.run(snaps)

        # t=0: buy YES at 0.51 (mid+half)
        #       size = equity * max_position_pct * confidence = 80
        # t=4: buy NO at (1-0.60)+0.01 = 0.41
        #       The close reduces YES position fully; remaining size from
        #       the risk engine (based on equity with unrealised gains)
        #       may flip into a tiny NO position, adding ~0.004 P&L.
        assert result.num_trades == 1
        assert result.total_pnl == pytest.approx(6.404, rel=1e-3)
        assert result.final_equity == pytest.approx(10_006.404, rel=1e-3)
        assert result.num_wins == 1
        assert result.num_losses == 0
        assert result.win_rate == approx(1.0)

    @pytest.mark.asyncio
    async def test_two_round_trips(self) -> None:
        """Two sequential open-close cycles on the same market."""
        snaps = synthetic_market(
            [0.50, 0.52, 0.55, 0.58, 0.60, 0.58, 0.55, 0.52],
        )

        engine = BacktestEngine(initial_equity=10_000.0, fee_rate=0.0)
        # Two independent strategies for two round trips
        engine.add_strategy(OpenCloseStrategy(open_at=0, close_at=3, side="YES"))
        engine.add_strategy(OpenCloseStrategy(open_at=4, close_at=7, side="YES"))

        result = await engine.run(snaps)

        assert result.num_trades == 2
        # Trade 1 (mid 0.50 → 0.58): win  (~4.8)
        # Trade 2 (mid 0.60 → 0.52): loss (~−8.0)
        assert result.num_wins == 1
        assert result.num_losses == 1

    @pytest.mark.asyncio
    async def test_fees_deducted(self) -> None:
        """With fee_rate > 0, P&L is lower than fee-free."""
        snaps = synthetic_market([0.50, 0.60, 0.60])

        engine = BacktestEngine(initial_equity=10_000.0, fee_rate=0.05)
        engine.add_strategy(OpenCloseStrategy(open_at=0, close_at=1, side="YES"))

        result = await engine.run(snaps)

        assert result.num_trades == 1
        assert result.total_fees > 0.0
        # Fee-free would give ~3.2 (80 * ((1-0.41) - 0.51) = 6.4 but with 3 snapshots
        # and midpoint 0.60, close_price = (1-0.60)+0.01 = 0.41)
        # With fees, it's less
        assert 0.0 < result.total_pnl < 6.4

    @pytest.mark.asyncio
    async def test_risk_rejection_logged(self) -> None:
        """When risk rejects, no fill is recorded."""
        original = settings.max_position_pct
        try:
            settings.max_position_pct = 0.0  # no position allowed

            snaps = synthetic_market([0.50, 0.55, 0.60])
            engine = BacktestEngine(
                initial_equity=10_000.0, fee_rate=0.0,
            )
            engine.add_strategy(OpenCloseStrategy(
                open_at=0, close_at=2, side="YES",
                model_prob=0.51, implied_prob=0.50,
            ))

            result = await engine.run(snaps)
            assert result.num_trades == 0
            assert result.total_pnl == approx(0.0)
        finally:
            settings.max_position_pct = original

    @pytest.mark.asyncio
    async def test_look_ahead_bias_isolation(self) -> None:
        """Engine processes snapshots in order; only current snapshot
        is available when generating signals."""
        snaps = synthetic_market([0.50, 0.55, 0.60, 0.65, 0.70])

        captured_features: list[float] = []

        class CaptureStrategy(Strategy):
            name = "capture"
            _step = -1

            def generate_signal(self, features, context=None):
                self._step += 1
                captured_features.append(features.get("midpoint", -1))
                return self._reject(
                    features.get("market_id", "unknown"), "capture",
                )

        engine = BacktestEngine(initial_equity=10_000.0, fee_rate=0.0)
        engine.add_strategy(CaptureStrategy())
        await engine.run(snaps)

        assert captured_features == [0.50, 0.55, 0.60, 0.65, 0.70]


class TestBacktestExecution:
    """Verify fill-price calculation."""

    def test_yes_buy_at_ask(self) -> None:
        from app.backtesting.execution import BacktestExecution
        from app.portfolio.tracker import PortfolioTracker

        pt = PortfolioTracker(initial_equity=10_000.0)
        exec_ = BacktestExecution(portfolio=pt, fee_rate=0.0)

        snap = MarketSnapshot(
            timestamp=0.0, market_id="m", midpoint=0.50, spread=0.02,
        )
        result = exec_.execute("m", "YES", 100, snap)
        assert result["fill_price"] == approx(0.51)

    def test_no_buy_at_no_ask(self) -> None:
        from app.backtesting.execution import BacktestExecution
        from app.portfolio.tracker import PortfolioTracker

        pt = PortfolioTracker(initial_equity=10_000.0)
        exec_ = BacktestExecution(portfolio=pt, fee_rate=0.0)

        snap = MarketSnapshot(
            timestamp=0.0, market_id="m", midpoint=0.50, spread=0.02,
        )
        result = exec_.execute("m", "NO", 100, snap)
        # NO ask = (1 - mid) + half_spread = 0.50 + 0.01 = 0.51
        assert result["fill_price"] == approx(0.51)

    def test_slippage_applied(self) -> None:
        from app.backtesting.execution import BacktestExecution
        from app.portfolio.tracker import PortfolioTracker

        pt = PortfolioTracker(initial_equity=10_000.0)
        exec_ = BacktestExecution(portfolio=pt, fee_rate=0.0)

        snap = MarketSnapshot(
            timestamp=0.0, market_id="m",
            midpoint=0.50, spread=0.02, depth=100,
        )
        result = exec_.execute("m", "YES", 500, snap)
        assert result["fill_price"] > 0.51
        assert result["slippage"] > 0.0

    def test_fees_increase_fill_price(self) -> None:
        from app.backtesting.execution import BacktestExecution
        from app.portfolio.tracker import PortfolioTracker

        pt = PortfolioTracker(initial_equity=10_000.0)
        exec_ = BacktestExecution(portfolio=pt, fee_rate=0.10)

        snap = MarketSnapshot(
            timestamp=0.0, market_id="m", midpoint=0.50, spread=0.02,
        )
        result = exec_.execute("m", "YES", 100, snap)
        assert result["fee"] > 0.0
        assert result["fill_price"] > result["raw_fill_price"]


class TestMetricsCalculator:
    """Unit tests for individual metric computations."""

    def test_no_trades(self) -> None:
        calc = MetricsCalculator(
            initial_equity=10_000.0,
            equity_curve=[EquityPoint(0, 10000, 0, 0, 0, 0)],
            fills=[],
        )
        r = calc.compute()
        assert r.num_trades == 0
        assert r.total_pnl == approx(0.0)
        assert r.win_rate == approx(0.0)
        assert r.profit_factor == approx(0.0)

    def test_max_drawdown(self) -> None:
        curve = [
            EquityPoint(0, 10000, 0, 0, 0, 0),
            EquityPoint(1, 11000, 0, 0, 0, 0),
            EquityPoint(2, 10500, 0, 0, 0, 0),
            EquityPoint(3, 9500, 0, 0, 0, 0),
            EquityPoint(4, 10800, 0, 0, 0, 0),
        ]
        calc = MetricsCalculator(10000, curve, [])
        r = calc.compute()
        assert r.max_drawdown == approx(1500.0)
        # max_drawdown_pct = max_drawdown / peak * 100
        assert r.max_drawdown_pct == pytest.approx(1500.0 / 11000.0 * 100.0)

    def test_win_rate(self) -> None:
        fills = [
            FillRecord(0, "m", "YES", 100, 0.50, 0, 0.0, edge=0.05),
            FillRecord(1, "m", "NO", 100, 0.50, 0, 5.0, edge=0.03),
            FillRecord(2, "m", "YES", 100, 0.50, 0, 0.0, edge=0.04),
            FillRecord(3, "m", "NO", 100, 0.50, 0, -3.0, edge=-0.02),
        ]
        calc = MetricsCalculator(10000, [
            EquityPoint(0, 10000, 0, 0, 0, 0),
            EquityPoint(1, 10005, 0, 0, 0, 0),
            EquityPoint(2, 10002, 0, 0, 0, 0),
        ], fills)
        r = calc.compute()
        # Trade 0: fill[0] opens (YES), fill[1] closes (NO, +5) → win
        # Trade 1: fill[2] opens (YES), fill[3] closes (NO, -3) → loss
        assert r.num_trades == 2
        assert r.num_wins == 1
        assert r.num_losses == 1
        assert r.win_rate == approx(0.5)
        assert r.gross_profit == approx(5.0)
        assert r.gross_loss == approx(-3.0)
        assert r.profit_factor == approx(5.0 / 3.0)
        assert r.expectancy == approx((5.0 - 3.0) / 2.0)

    def test_sharpe_and_sortino(self) -> None:
        curve = [
            EquityPoint(0, 10000, 0, 0, 0, 0),
            EquityPoint(1, 10100, 0, 0, 0, 0),
            EquityPoint(2, 10050, 0, 0, 0, 0),
            EquityPoint(3, 10200, 0, 0, 0, 0),
        ]
        calc = MetricsCalculator(10000, curve, [])
        r = calc.compute()
        assert r.sharpe_ratio != 0.0
        assert r.sortino_ratio != 0.0

    def test_calibration(self) -> None:
        fills = [
            FillRecord(0, "m", "YES", 100, 0.50, 0, 0.0, edge=0.10),
            FillRecord(1, "m", "NO", 100, 0.50, 0, 8.0, edge=0.08),
        ]
        calc = MetricsCalculator(10000, [
            EquityPoint(0, 10000, 0, 0, 0, 0),
            EquityPoint(1, 10008, 0, 0, 0, 0),
        ], fills)
        r = calc.compute()
        assert 0.0 <= r.calibration_score <= 1.0


class TestReportGenerator:
    """Verify JSON, CSV, and equity curve output."""

    def _minimal_result(self, **overrides: Any) -> BacktestResult:
        defaults = dict(
            initial_equity=10000, final_equity=10008,
            total_return=0.0008, total_pnl=8.0,
            max_drawdown=0.0, max_drawdown_pct=0.0,
            win_rate=1.0, loss_rate=0.0, profit_factor=float("inf"),
            expectancy=8.0, sharpe_ratio=0.5, sortino_ratio=0.3,
            turnover=0.01, avg_holding_period=240.0,
            avg_net_edge=0.05, calibration_score=0.01,
            slippage_impact=0.0,
            num_trades=1, num_wins=1, num_losses=0,
            total_fees=0.0, gross_profit=8.0, gross_loss=0.0,
            equity_curve=[
                EquityPoint(0, 10000, 0, 0, 0, 0),
                EquityPoint(60, 10008, 0, 0, 0, 0),
            ],
            fills=[
                FillRecord(0, "m", "YES", 100, 0.51, 0, 0.0),
                FillRecord(60, "m", "NO", 100, 0.41, 0, 8.0),
            ],
            trades=[],
        )
        defaults.update(overrides)
        return BacktestResult(**defaults)

    def test_write_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gen = ReportGenerator(output_dir=tmp)
            paths = gen.generate(self._minimal_result(), label="test_json")
            assert os.path.exists(paths["json"])
            with open(paths["json"]) as f:
                data = json.load(f)
            assert data["metrics"]["total_pnl"] == 8.0
            assert data["metrics"]["num_trades"] == 1
            assert len(data["fills"]) == 2
            assert len(data["equity_curve"]) == 2

    def test_write_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gen = ReportGenerator(output_dir=tmp)
            paths = gen.generate(
                self._minimal_result(profit_factor=0.0),
                label="test_csv",
            )
            assert os.path.exists(paths["csv"])

    def test_equity_curve_png(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gen = ReportGenerator(output_dir=tmp)
            paths = gen.generate(
                self._minimal_result(
                    profit_factor=0.0,
                    equity_curve=[
                        EquityPoint(0, 10000, 0, 0, 0, 0),
                        EquityPoint(60, 10008, 0, 0, 0, 0),
                    ],
                ),
                label="test_eq",
            )
            assert os.path.exists(paths["equity_curve"])


class TestBacktestEngineIntegration:
    """End-to-end integration tests."""

    @pytest.mark.asyncio
    async def test_full_pipeline_generates_report(self) -> None:
        snaps = synthetic_market([0.50, 0.55, 0.60])

        engine = BacktestEngine(initial_equity=10_000.0, fee_rate=0.0)
        engine.add_strategy(OpenCloseStrategy(open_at=0, close_at=2, side="YES"))

        result = await engine.run(snaps)

        assert result.num_trades == 1

        with tempfile.TemporaryDirectory() as tmp:
            gen = ReportGenerator(output_dir=tmp)
            paths = gen.generate(result, label="integration_test")
            assert os.path.exists(paths["json"])
            assert os.path.exists(paths["csv"])

    @pytest.mark.asyncio
    async def test_progress_callback_invoked(self) -> None:
        snaps = synthetic_market([0.50, 0.55, 0.60])
        engine = BacktestEngine(initial_equity=10_000.0, fee_rate=0.0)
        engine.add_strategy(OpenCloseStrategy(open_at=0, close_at=2, side="YES"))

        calls: list[tuple[int, int]] = []

        def cb(curr: int, total: int) -> None:
            calls.append((curr, total))

        await engine.run(snaps, progress_callback=cb)

        assert len(calls) == 3
        assert calls[-1] == (3, 3)
