"""Walk-forward validation — rolling out-of-sample evaluation.

Splits a chronological dataset into successive train → validation
windows and evaluates a **frozen** strategy on each unseen period::

    Train        Validation
    Jan-Mar  →   Apr
    Jan-Apr  →   May
    Jan-May  →   Jun

Rules
-----
* Time-series observations are **never shuffled** — data is always
  processed in ascending timestamp order.
* For each window: (1) train the model, (2) freeze its parameters,
  (3) evaluate on the unseen validation period, (4) record metrics,
  (5) move forward.
* The **final evaluation period is never used to tune anything**.
  All detection thresholds are fixed module constants.
* The strategy factory must be deterministic: fitting two fresh
  instances on identical data must produce identical parameters
  (this is what makes IS/OOS isolation and reproduction sound).

Detection
---------
* ``overfitting`` — in-sample P&L positive while out-of-sample ≤ 0.
* ``unstable_parameters`` — fitted parameters drift across windows.
* ``regime_sensitive`` — per-window P&L dispersion is high.
* ``degradation`` — performance declines over successive windows.
* ``single_period_luck`` — the strategy profits in only one period
  (explicitly marked **UNSTABLE**).

Note
----
The underlying ``BacktestEngine`` applies the live risk-engine
freshness gate.  Historical data whose timestamps are older than
``settings.data_max_age_seconds`` will be rejected as ``STALE_DATA``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.backtesting.engine import BacktestEngine
from app.backtesting.models import (
    BacktestResult,
    EquityPoint,
    FillRecord,
    MarketSnapshot,
)
from app.strategies.base import Strategy

logger = logging.getLogger(__name__)

# ── Detection thresholds ─────────────────────────────────────────────
# Fixed module constants.  Deliberately NOT tuned against any
# evaluation period — they are set once, up front.
_OVERFIT_MIN_WINDOWS = 1
_PARAM_CV_THRESHOLD = 0.50
_REGIME_CV_THRESHOLD = 1.5
_DEGRADATION_MIN_WINDOWS = 3
_DEGRADATION_CORR_THRESHOLD = -0.5
_SINGLE_PERIOD_MIN_WINDOWS = 2
_EPS = 1e-12


# ── Window metrics (aggregated per evaluation) ───────────────────────


@dataclass
class WindowMetrics:
    """Subset of ``BacktestResult`` metrics for a single evaluation."""

    total_pnl: float
    total_return: float
    max_drawdown: float
    max_drawdown_pct: float
    profit_factor: float
    expectancy: float
    calibration_score: float
    avg_net_edge: float
    win_rate: float
    num_trades: int
    num_wins: int
    num_losses: int
    gross_profit: float
    gross_loss: float
    total_fees: float
    avg_holding_period: float

    @staticmethod
    def from_result(result: BacktestResult) -> WindowMetrics:
        return WindowMetrics(
            total_pnl=result.total_pnl,
            total_return=result.total_return,
            max_drawdown=result.max_drawdown,
            max_drawdown_pct=result.max_drawdown_pct,
            profit_factor=result.profit_factor,
            expectancy=result.expectancy,
            calibration_score=result.calibration_score,
            avg_net_edge=result.avg_net_edge,
            win_rate=result.win_rate,
            num_trades=result.num_trades,
            num_wins=result.num_wins,
            num_losses=result.num_losses,
            gross_profit=result.gross_profit,
            gross_loss=result.gross_loss,
            total_fees=result.total_fees,
            avg_holding_period=result.avg_holding_period,
        )


@dataclass
class WalkForwardWindow:
    """One train → validation split with its recorded metrics."""

    index: int
    train_start: float
    train_end: float
    val_start: float
    val_end: float
    train_count: int
    val_count: int
    out_of_sample: WindowMetrics
    in_sample: WindowMetrics | None = None
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class WalkForwardDiagnostics:
    """Stability diagnostics for the whole walk-forward run."""

    overfitting: bool
    unstable_parameters: bool
    regime_sensitive: bool
    degradation: bool
    single_period_luck: bool
    verdict: str
    reasons: list[str] = field(default_factory=list)
    codes: list[str] = field(default_factory=list)


@dataclass
class WalkForwardReport:
    """Reproducible walk-forward validation report."""

    strategy: str
    mode: str
    train_size: int
    val_size: int
    step: int
    initial_equity: float
    fee_rate: float
    evaluate_in_sample: bool
    data_hash: str
    created_at: str
    windows: list[WalkForwardWindow]
    total_pnl: float
    total_return: float
    max_drawdown: float
    max_drawdown_pct: float
    profit_factor: float
    expectancy: float
    calibration_score: float
    num_trades: int
    num_wins: int
    num_losses: int
    avg_net_edge: float
    win_rate: float
    total_fees: float
    mean_window_pnl: float
    median_window_pnl: float
    std_window_pnl: float
    best_window_pnl: float
    worst_window_pnl: float
    num_profitable_windows: int
    num_losing_windows: int
    num_zero_trade_windows: int
    failure_periods: list[int]
    diagnostics: WalkForwardDiagnostics
    param_drift: dict[str, float] = field(default_factory=dict)
    equity_curve: list[EquityPoint] = field(default_factory=list)
    fills: list[FillRecord] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)
    report_paths: dict[str, str] = field(default_factory=dict)


# ── Detectors ────────────────────────────────────────────────────────
# Pure functions — unit-testable without the engine.


def detect_overfitting(
    is_pnls: list[float], oos_pnls: list[float]
) -> tuple[bool, str]:
    """Overfitting when in-sample is profitable but OOS is not."""
    if not is_pnls or not oos_pnls:
        return False, ""
    mean_is = sum(is_pnls) / len(is_pnls)
    mean_oos = sum(oos_pnls) / len(oos_pnls)
    if mean_is > 0.0 and mean_oos <= 0.0:
        return True, (
            f"IS mean P&L {mean_is:+.2f} > 0 while OOS mean P&L "
            f"{mean_oos:+.2f} <= 0"
        )
    return False, ""


def detect_unstable_parameters(
    param_history: list[dict[str, Any]],
) -> tuple[bool, list[str], dict[str, float]]:
    """Flag parameters whose coefficient of variation exceeds threshold."""
    keys: set[str] = set()
    for p in param_history:
        keys.update(k for k, v in p.items() if isinstance(v, (int, float)))
    drift: dict[str, float] = {}
    unstable: list[str] = []
    for key in sorted(keys):
        values = [
            float(p[key]) for p in param_history
            if key in p and isinstance(p[key], (int, float))
        ]
        if len(values) < 2:
            continue
        mean = sum(values) / len(values)
        variance = sum((v - mean) ** 2 for v in values) / len(values)
        std = math.sqrt(variance)
        cv = std / abs(mean) if abs(mean) > _EPS else std
        drift[key] = cv
        if cv > _PARAM_CV_THRESHOLD:
            unstable.append(key)
    return bool(unstable), unstable, drift


def detect_regime_sensitivity(pnls: list[float]) -> tuple[bool, str]:
    """High dispersion of per-window P&L suggests regime sensitivity."""
    if len(pnls) < 3:
        return False, ""
    mean = sum(pnls) / len(pnls)
    variance = sum((p - mean) ** 2 for p in pnls) / len(pnls)
    std = math.sqrt(variance)
    if std / max(abs(mean), 1.0) > _REGIME_CV_THRESHOLD:
        return True, (
            f"Per-window P&L dispersion is high "
            f"(std ${std:.2f} vs mean ${mean:.2f})"
        )
    return False, ""


def detect_degradation(pnls: list[float]) -> tuple[bool, float]:
    """Negative trend in per-window P&L (Pearson correlation)."""
    if len(pnls) < _DEGRADATION_MIN_WINDOWS:
        return False, 0.0
    indices = list(range(len(pnls)))
    n = len(pnls)
    mean_x = (n - 1) / 2.0
    mean_y = sum(pnls) / n
    cov = sum(
        (i - mean_x) * (p - mean_y) for i, p in zip(indices, pnls)
    ) / n
    var_x = sum((i - mean_x) ** 2 for i in indices) / n
    var_y = sum((p - mean_y) ** 2 for p in pnls) / n
    if var_x < _EPS or var_y < _EPS:
        return False, 0.0
    slope = cov / var_x
    corr = cov / math.sqrt(var_x * var_y)
    if slope < 0.0 and corr <= _DEGRADATION_CORR_THRESHOLD:
        return True, slope
    return False, 0.0


def detect_single_period_luck(pnls: list[float]) -> tuple[bool, str]:
    """Profits concentrated in a single period → unstable."""
    if len(pnls) < _SINGLE_PERIOD_MIN_WINDOWS:
        return False, ""
    profitable = [p for p in pnls if p > 0.0]
    if not profitable:
        return False, ""
    if len(profitable) == 1:
        return True, (
            f"Only 1 of {len(pnls)} windows is profitable "
            f"(P&L {profitable[0]:+.2f})"
        )
    best = max(pnls)
    rest = sum(pnls) - best
    if rest <= 0.0:
        return True, (
            f"Total OOS P&L without the best window is {rest:+.2f} "
            f"(best window ${best:.2f})"
        )
    return False, ""


def _compute_verdict(d: WalkForwardDiagnostics) -> str:
    """UNSTABLE for any hard instability, SUSPECT for regime
    sensitivity, STABLE otherwise."""
    if (
        d.overfitting
        or d.unstable_parameters
        or d.degradation
        or d.single_period_luck
    ):
        return "UNSTABLE"
    if d.regime_sensitive:
        return "SUSPECT"
    return "STABLE"


# ── Window splitting ─────────────────────────────────────────────────


def split_windows(
    snapshots: list[MarketSnapshot],
    train_size: int,
    val_size: int,
    mode: str = "expanding",
    step: int | None = None,
) -> list[tuple[list[MarketSnapshot], list[MarketSnapshot]]]:
    """Split chronologically into (train, validation) window pairs.

    Snapshots are sorted by ascending timestamp (never shuffled) and
    sliced contiguously — validation data never precedes training data
    within a window.

    Parameters
    ----------
    snapshots : list[MarketSnapshot]
        Historical data.
    train_size : int
        Minimum number of snapshots in each training window.
    val_size : int
        Number of snapshots in each validation window.
    mode : str
        ``"expanding"`` — training always starts at the beginning of
        the dataset (Jan-Mar → Apr, Jan-Apr → May, …).
        ``"rolling"`` — training is a fixed-size trailing window.
    step : int | None
        Snapshots to advance between windows (default ``val_size``).

    Returns
    -------
    list of (train, validation) pairs.
    """
    if train_size < 1 or val_size < 1:
        raise ValueError(
            "train_size and val_size must be >= 1 "
            f"(got {train_size}, {val_size})"
        )
    if mode not in ("expanding", "rolling"):
        raise ValueError(f"mode must be 'expanding' or 'rolling' (got {mode!r})")
    step = val_size if step is None else step
    if step < 1:
        raise ValueError(f"step must be >= 1 (got {step})")

    ordered = sorted(snapshots, key=lambda s: s.timestamp)
    windows: list[tuple[list[MarketSnapshot], list[MarketSnapshot]]] = []

    if mode == "expanding":
        i = 0
        while True:
            end = train_size + i * step
            if end + val_size > len(ordered):
                break
            windows.append((ordered[0:end], ordered[end : end + val_size]))
            i += 1
    else:
        i = 0
        while True:
            start = i * step
            end = start + train_size
            if end + val_size > len(ordered):
                break
            windows.append((ordered[start:end], ordered[end : end + val_size]))
            i += 1

    return windows


# ── Validator ────────────────────────────────────────────────────────


class WalkForwardValidator:
    """Rolling train→validate evaluation with stability diagnostics.

    Parameters
    ----------
    strategy_factory : callable
        Returns a **fresh** ``Strategy`` instance.  Must be
        deterministic — fitting two instances on identical data must
        produce identical parameters.
    train_size : int
        Minimum training snapshots per window.
    val_size : int
        Validation snapshots per window (unseen period).
    mode : str
        ``"expanding"`` or ``"rolling"``.
    step : int | None
        Advance between windows (default ``val_size``).
    initial_equity : float
        Starting equity for each window evaluation.
    fee_rate : float
        Taker fee coefficient (Polymarket formula).
    evaluate_in_sample : bool
        Also run a backtest on the training window to enable the
        overfitting comparison (in-sample vs out-of-sample).
    """

    def __init__(
        self,
        strategy_factory: Callable[[], Strategy],
        train_size: int,
        val_size: int,
        mode: str = "expanding",
        step: int | None = None,
        initial_equity: float = 10_000.0,
        fee_rate: float = 0.05,
        evaluate_in_sample: bool = True,
    ) -> None:
        self._strategy_factory = strategy_factory
        self._train_size = train_size
        self._val_size = val_size
        self._mode = mode
        self._step = val_size if step is None else step
        self._initial_equity = initial_equity
        self._fee_rate = fee_rate
        self._evaluate_in_sample = evaluate_in_sample
        self._strategy_name = self._strategy_factory().name

    @property
    def strategy_name(self) -> str:
        return self._strategy_name

    async def run(
        self,
        snapshots: list[MarketSnapshot],
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> WalkForwardReport:
        """Run walk-forward validation.

        Parameters
        ----------
        snapshots : list[MarketSnapshot]
            Historical data (sorted internally, never shuffled).
        progress_callback : callable, optional
            Sync callable ``fn(current_window, total_windows)``.

        Returns
        -------
        WalkForwardReport
        """
        if not snapshots:
            raise ValueError("snapshots must not be empty")

        windows = split_windows(
            snapshots,
            train_size=self._train_size,
            val_size=self._val_size,
            mode=self._mode,
            step=self._step,
        )
        if not windows:
            raise ValueError(
                "insufficient data for walk-forward validation "
                f"(need > {self._train_size + self._val_size} snapshots)"
            )

        ordered = sorted(snapshots, key=lambda s: s.timestamp)
        results: list[WalkForwardWindow] = []
        oos_results: list[BacktestResult] = []
        total = len(windows)

        for idx, (train, val) in enumerate(windows):
            # 1. Train the model on the training window.
            strategy = self._strategy_factory()
            self._fit(strategy, train)
            params = self._params(strategy)

            # 2. Freeze parameters; evaluate on the unseen period.
            oos = await self._run_engine(val, strategy)
            oos_results.append(oos)

            # In-sample reference (optional) — a *second* deterministic
            # instance fitted on the same data, run on the same data.
            in_sample: WindowMetrics | None = None
            if self._evaluate_in_sample:
                is_strategy = self._strategy_factory()
                self._fit(is_strategy, train)
                is_result = await self._run_engine(train, is_strategy)
                in_sample = WindowMetrics.from_result(is_result)

            results.append(WalkForwardWindow(
                index=idx,
                train_start=train[0].timestamp,
                train_end=train[-1].timestamp,
                val_start=val[0].timestamp,
                val_end=val[-1].timestamp,
                train_count=len(train),
                val_count=len(val),
                out_of_sample=WindowMetrics.from_result(oos),
                in_sample=in_sample,
                params=params,
            ))

            if progress_callback:
                progress_callback(idx + 1, total)

        return self._aggregate(results, oos_results, ordered)

    # ── Internals ───────────────────────────────────────────────────

    async def _run_engine(
        self,
        snapshots: list[MarketSnapshot],
        strategy: Strategy,
    ) -> BacktestResult:
        engine = BacktestEngine(
            initial_equity=self._initial_equity,
            fee_rate=self._fee_rate,
        )
        engine.add_strategy(strategy)
        return await engine.run(snapshots)

    @staticmethod
    def _fit(strategy: Strategy, snapshots: list[MarketSnapshot]) -> None:
        fit = getattr(strategy, "fit", None)
        if callable(fit):
            fit(snapshots)

    @staticmethod
    def _params(strategy: Strategy) -> dict[str, Any]:
        params = getattr(strategy, "params", None)
        if callable(params):
            return dict(params())
        return {}

    def _aggregate(
        self,
        windows: list[WalkForwardWindow],
        oos_results: list[BacktestResult],
        ordered: list[MarketSnapshot],
    ) -> WalkForwardReport:
        oos_pnls = [w.out_of_sample.total_pnl for w in windows]

        # Chain OOS equity curves so the combined drawdown is real.
        chain: list[EquityPoint] = []
        all_fills: list[FillRecord] = []
        cumulative = self._initial_equity
        for result in oos_results:
            offset = cumulative - result.initial_equity
            for pt in result.equity_curve:
                chain.append(EquityPoint(
                    timestamp=pt.timestamp,
                    equity=pt.equity + offset,
                    total_exposure=pt.total_exposure,
                    unrealised_pnl=pt.unrealised_pnl,
                    realised_pnl=pt.realised_pnl,
                    num_positions=pt.num_positions,
                ))
            cumulative += result.total_pnl
            all_fills.extend(result.fills)

        total_pnl = cumulative - self._initial_equity
        max_dd, max_dd_pct = _max_drawdown(chain)

        num_trades = sum(w.out_of_sample.num_trades for w in windows)
        num_wins = sum(w.out_of_sample.num_wins for w in windows)
        num_losses = sum(w.out_of_sample.num_losses for w in windows)
        gross_profit = sum(w.out_of_sample.gross_profit for w in windows)
        gross_loss = sum(w.out_of_sample.gross_loss for w in windows)
        total_fees = sum(w.out_of_sample.total_fees for w in windows)

        profit_factor = (
            gross_profit / abs(gross_loss)
            if abs(gross_loss) > _EPS
            else float("inf") if gross_profit > 0.0 else 0.0
        )
        expectancy = total_pnl / num_trades if num_trades > 0 else 0.0
        win_rate = num_wins / num_trades if num_trades > 0 else 0.0

        traded = [w for w in windows if w.out_of_sample.num_trades > 0]
        edge_num = sum(
            w.out_of_sample.avg_net_edge * w.out_of_sample.num_trades
            for w in traded
        )
        avg_edge = (
            edge_num / num_trades
            if num_trades > 0
            else (
                sum(w.out_of_sample.avg_net_edge for w in traded) / len(traded)
                if traded
                else 0.0
            )
        )
        calibration = (
            sum(w.out_of_sample.calibration_score for w in traded) / len(traded)
            if traded
            else 0.0
        )

        # In-sample reference for overfitting detection.
        is_pnls = [
            w.in_sample.total_pnl for w in windows if w.in_sample is not None
        ]
        is_overfit, overfit_reason = (
            detect_overfitting(is_pnls, oos_pnls)
            if self._evaluate_in_sample
            else (False, "In-sample evaluation disabled — overfitting check skipped")
        )

        unstable, unstable_keys, drift = detect_unstable_parameters(
            [w.params for w in windows]
        )
        unstable_reason = (
            f"Fitted parameters drift across windows: {', '.join(unstable_keys)}"
            if unstable
            else ""
        )
        regime, regime_reason = detect_regime_sensitivity(oos_pnls)
        degraded, deg_slope = detect_degradation(oos_pnls)
        degradation_reason = (
            f"OOS P&L declines across windows (slope ${deg_slope:.2f}/window)"
            if degraded
            else ""
        )
        luck, luck_reason = detect_single_period_luck(oos_pnls)

        reasons: list[str] = []
        codes: list[str] = []
        for flag, code, reason in [
            (is_overfit, "OVERFITTING", overfit_reason),
            (unstable, "UNSTABLE_PARAMETERS", unstable_reason),
            (regime, "REGIME_SENSITIVE", regime_reason),
            (degraded, "DEGRADATION", degradation_reason),
            (luck, "SINGLE_PERIOD_LUCK", luck_reason),
        ]:
            if flag:
                codes.append(code)
                if reason:
                    reasons.append(reason)

        diagnostics = WalkForwardDiagnostics(
            overfitting=is_overfit,
            unstable_parameters=unstable,
            regime_sensitive=regime,
            degradation=degraded,
            single_period_luck=luck,
            verdict="",
            reasons=reasons,
            codes=codes,
        )
        diagnostics.verdict = _compute_verdict(diagnostics)

        failure = [w.index for w in windows if w.out_of_sample.total_pnl < 0.0]
        zero_trade = sum(
            1 for w in windows if w.out_of_sample.num_trades == 0
        )

        return WalkForwardReport(
            strategy=self._strategy_name,
            mode=self._mode,
            train_size=self._train_size,
            val_size=self._val_size,
            step=self._step,
            initial_equity=self._initial_equity,
            fee_rate=self._fee_rate,
            evaluate_in_sample=self._evaluate_in_sample,
            data_hash=_data_hash(ordered),
            created_at=datetime.now(UTC).isoformat(),
            windows=windows,
            total_pnl=total_pnl,
            total_return=total_pnl / self._initial_equity
            if self._initial_equity > 0
            else 0.0,
            max_drawdown=max_dd,
            max_drawdown_pct=max_dd_pct,
            profit_factor=profit_factor,
            expectancy=expectancy,
            calibration_score=calibration,
            num_trades=num_trades,
            num_wins=num_wins,
            num_losses=num_losses,
            avg_net_edge=avg_edge,
            win_rate=win_rate,
            total_fees=total_fees,
            mean_window_pnl=sum(oos_pnls) / len(oos_pnls),
            median_window_pnl=statistics.median(oos_pnls),
            std_window_pnl=statistics.pstdev(oos_pnls),
            best_window_pnl=max(oos_pnls),
            worst_window_pnl=min(oos_pnls),
            num_profitable_windows=sum(1 for p in oos_pnls if p > 0.0),
            num_losing_windows=sum(1 for p in oos_pnls if p < 0.0),
            num_zero_trade_windows=zero_trade,
            failure_periods=failure,
            diagnostics=diagnostics,
            param_drift=drift,
            equity_curve=chain,
            fills=all_fills,
            config={
                "train_size": self._train_size,
                "val_size": self._val_size,
                "mode": self._mode,
                "step": self._step,
                "initial_equity": self._initial_equity,
                "fee_rate": self._fee_rate,
                "evaluate_in_sample": self._evaluate_in_sample,
            },
        )


# ── Report writer ────────────────────────────────────────────────────

_REPORT_DIR_DEFAULT = "walk_forward_reports"


def _metrics_dict(m: WindowMetrics) -> dict[str, Any]:
    return {
        "total_pnl": m.total_pnl,
        "total_return": m.total_return,
        "total_return_pct": m.total_return * 100,
        "max_drawdown": m.max_drawdown,
        "max_drawdown_pct": m.max_drawdown_pct,
        "profit_factor": m.profit_factor,
        "expectancy": m.expectancy,
        "calibration_score": m.calibration_score,
        "avg_net_edge": m.avg_net_edge,
        "win_rate": m.win_rate,
        "win_rate_pct": m.win_rate * 100,
        "num_trades": m.num_trades,
        "num_wins": m.num_wins,
        "num_losses": m.num_losses,
        "gross_profit": m.gross_profit,
        "gross_loss": m.gross_loss,
        "total_fees": m.total_fees,
        "avg_holding_period": m.avg_holding_period,
    }


class WalkForwardReporter:
    """Write walk-forward reports as JSON and per-window CSV."""

    def __init__(self, output_dir: str = _REPORT_DIR_DEFAULT) -> None:
        self._output_dir = output_dir

    def generate(
        self,
        report: WalkForwardReport,
        label: str = "walk_forward",
    ) -> dict[str, str]:
        """Write ``label_report.json`` and ``label_windows.csv``.

        Returns ``{"json": …, "csv": …}`` paths.
        """
        os.makedirs(self._output_dir, exist_ok=True)
        json_path = os.path.join(self._output_dir, f"{label}_report.json")
        csv_path = os.path.join(self._output_dir, f"{label}_windows.csv")

        self._write_json(json_path, report, label)
        self._write_csv(csv_path, report)

        paths = {"json": json_path, "csv": csv_path}
        report.report_paths = paths
        logger.info("Walk-forward reports saved to %s", self._output_dir)
        return paths

    @staticmethod
    def _write_json(path: str, report: WalkForwardReport, label: str) -> None:
        data = {
            "label": label,
            "strategy": report.strategy,
            "mode": report.mode,
            "created_at": report.created_at,
            "data_hash": report.data_hash,
            "config": report.config,
            "num_windows": len(report.windows),
            "verdict": report.diagnostics.verdict,
            "diagnostics": {
                "overfitting": report.diagnostics.overfitting,
                "unstable_parameters": report.diagnostics.unstable_parameters,
                "regime_sensitive": report.diagnostics.regime_sensitive,
                "degradation": report.diagnostics.degradation,
                "single_period_luck": report.diagnostics.single_period_luck,
                "codes": report.diagnostics.codes,
                "reasons": report.diagnostics.reasons,
            },
            "oos_metrics": {
                "total_pnl": report.total_pnl,
                "total_return_pct": report.total_return * 100,
                "max_drawdown": report.max_drawdown,
                "max_drawdown_pct": report.max_drawdown_pct,
                "profit_factor": report.profit_factor,
                "expectancy": report.expectancy,
                "calibration_score": report.calibration_score,
                "num_trades": report.num_trades,
                "num_wins": report.num_wins,
                "num_losses": report.num_losses,
                "avg_net_edge": report.avg_net_edge,
                "win_rate_pct": report.win_rate * 100,
                "total_fees": report.total_fees,
            },
            "window_summary": {
                "mean_window_pnl": report.mean_window_pnl,
                "median_window_pnl": report.median_window_pnl,
                "std_window_pnl": report.std_window_pnl,
                "best_window_pnl": report.best_window_pnl,
                "worst_window_pnl": report.worst_window_pnl,
                "num_profitable_windows": report.num_profitable_windows,
                "num_losing_windows": report.num_losing_windows,
                "num_zero_trade_windows": report.num_zero_trade_windows,
                "failure_periods": report.failure_periods,
            },
            "param_drift": report.param_drift,
            "windows": [
                {
                    "index": w.index,
                    "train": {
                        "start": w.train_start,
                        "end": w.train_end,
                        "count": w.train_count,
                    },
                    "val": {
                        "start": w.val_start,
                        "end": w.val_end,
                        "count": w.val_count,
                    },
                    "in_sample": (
                        _metrics_dict(w.in_sample) if w.in_sample else None
                    ),
                    "out_of_sample": _metrics_dict(w.out_of_sample),
                    "params": w.params,
                }
                for w in report.windows
            ],
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)

    @staticmethod
    def _write_csv(path: str, report: WalkForwardReport) -> None:
        import csv as csv_module

        header = [
            "window", "train_start", "train_end", "train_count",
            "val_start", "val_end", "val_count",
            "in_sample_pnl", "oos_pnl", "oos_return_pct",
            "oos_max_drawdown_pct", "oos_profit_factor", "oos_expectancy",
            "oos_calibration", "oos_num_trades", "oos_num_wins",
            "oos_num_losses", "oos_avg_net_edge", "oos_win_rate_pct",
            "params",
        ]
        with open(path, "w", newline="") as f:
            writer = csv_module.writer(f)
            writer.writerow(header)
            for w in report.windows:
                oos = w.out_of_sample
                writer.writerow([
                    w.index,
                    w.train_start, w.train_end, w.train_count,
                    w.val_start, w.val_end, w.val_count,
                    w.in_sample.total_pnl if w.in_sample else "",
                    oos.total_pnl,
                    oos.total_return * 100,
                    oos.max_drawdown_pct,
                    oos.profit_factor,
                    oos.expectancy,
                    oos.calibration_score,
                    oos.num_trades,
                    oos.num_wins,
                    oos.num_losses,
                    oos.avg_net_edge,
                    oos.win_rate * 100,
                    json.dumps(w.params, default=str),
                ])


# ── Shared helpers ───────────────────────────────────────────────────


def _max_drawdown(curve: list[EquityPoint]) -> tuple[float, float]:
    if len(curve) < 2:
        return 0.0, 0.0
    peak = curve[0].equity
    max_dd = 0.0
    for pt in curve:
        if pt.equity > peak:
            peak = pt.equity
        dd = peak - pt.equity
        if dd > max_dd:
            max_dd = dd
    pct = max_dd / peak * 100.0 if peak > _EPS else 0.0
    return max_dd, pct


def _data_hash(snapshots: list[MarketSnapshot]) -> str:
    """SHA-256 of a canonical serialization of the input data.

    Makes reports reproducible: identical data → identical hash.
    """
    payload = json.dumps(
        [
            {
                "ts": s.timestamp,
                "market": s.market_id,
                "mid": s.midpoint,
                "spread": s.spread,
                "bid": s.bid,
                "ask": s.ask,
                "depth": s.depth,
                "vol": s.volume,
            }
            for s in snapshots
        ],
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
