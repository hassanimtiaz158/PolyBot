"""Backtesting framework — replay historical data with no look-ahead."""

from app.backtesting.engine import BacktestEngine
from app.backtesting.execution import BacktestExecution
from app.backtesting.metrics import MetricsCalculator
from app.backtesting.models import (
    BacktestResult,
    EquityPoint,
    FillRecord,
    MarketSnapshot,
    TradeRecord,
)
from app.backtesting.report import ReportGenerator
from app.backtesting.walk_forward import (
    WalkForwardDiagnostics,
    WalkForwardReport,
    WalkForwardReporter,
    WalkForwardValidator,
    WalkForwardWindow,
    WindowMetrics,
    split_windows,
)

__all__ = [
    "BacktestEngine",
    "BacktestExecution",
    "MarketSnapshot",
    "FillRecord",
    "EquityPoint",
    "TradeRecord",
    "BacktestResult",
    "MetricsCalculator",
    "ReportGenerator",
    "WalkForwardValidator",
    "WalkForwardWindow",
    "WalkForwardReport",
    "WalkForwardDiagnostics",
    "WalkForwardReporter",
    "WindowMetrics",
    "split_windows",
]
