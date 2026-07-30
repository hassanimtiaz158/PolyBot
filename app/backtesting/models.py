"""Data models for backtesting."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MarketSnapshot:
    """A single point-in-time view of a market."""

    timestamp: float
    market_id: str
    midpoint: float
    spread: float = 0.02
    bid: float | None = None
    ask: float | None = None
    depth: float = 1_000_000.0
    volume: float = 0.0


@dataclass
class FillRecord:
    """Record of a single fill event."""

    timestamp: float
    market_id: str
    side: str
    size: float
    price: float
    fee: float
    pnl_change: float
    slippage: float = 0.0
    edge: float | None = None
    signal_id: str | None = None


@dataclass
class EquityPoint:
    """Portfolio snapshot at a point in time."""

    timestamp: float
    equity: float
    total_exposure: float
    unrealised_pnl: float
    realised_pnl: float
    num_positions: int


@dataclass
class TradeRecord:
    """A completed round-trip trade (open to close)."""

    market_id: str
    side: str
    entry_time: float
    exit_time: float
    entry_price: float
    exit_price: float
    size: float
    pnl: float
    fees: float
    holding_period: float = 0.0
    edge: float | None = None


@dataclass
class BacktestResult:
    """Aggregated backtest result with all metrics."""

    initial_equity: float
    final_equity: float
    total_return: float
    total_pnl: float
    max_drawdown: float
    max_drawdown_pct: float
    win_rate: float
    loss_rate: float
    profit_factor: float
    expectancy: float
    sharpe_ratio: float
    sortino_ratio: float
    turnover: float
    avg_holding_period: float
    avg_net_edge: float
    calibration_score: float
    slippage_impact: float
    num_trades: int
    num_wins: int
    num_losses: int
    total_fees: float
    gross_profit: float
    gross_loss: float
    equity_curve: list[EquityPoint]
    fills: list[FillRecord]
    trades: list[TradeRecord]
    config: dict[str, Any] = field(default_factory=dict)
    report_paths: dict[str, str] = field(default_factory=dict)
