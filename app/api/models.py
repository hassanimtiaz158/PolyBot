"""Pydantic response models for the read-only FastAPI backend.

All models are read-only projections of database entities.  No model
carries credentials, API keys, or private keys.
"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T")


class _EntityModel(BaseModel):
    """Base model that can be populated from repository dataclasses."""

    model_config = ConfigDict(
        from_attributes=True,
        protected_namespaces=(),
    )


# ── Entity projections ──────────────────────────────────────────────


class MarketResponse(_EntityModel):
    market_id: str
    question: str
    condition_id: str | None = None
    status: str | None = None
    resolution_time: str | None = None
    liquidity: float | None = None
    created_at: str | None = None
    updated_at: str | None = None


class SignalResponse(_EntityModel):
    signal_id: str
    market_id: str
    strategy: str
    side: str
    decision: str
    model_probability: float | None = None
    implied_probability: float | None = None
    gross_edge: float | None = None
    estimated_cost: float | None = None
    net_edge: float | None = None
    confidence: float | None = None
    rejection_reason: str | None = None
    timestamp: str | None = None


class PositionResponse(_EntityModel):
    position_id: str
    market_id: str
    side: str
    size: float | None = None
    average_entry: float | None = None
    current_price: float | None = None
    realised_pnl: float | None = None
    unrealised_pnl: float | None = None


class OrderResponse(_EntityModel):
    order_id: str
    market_id: str
    side: str
    status: str
    requested_price: float | None = None
    requested_size: float | None = None
    filled_size: float | None = None
    average_fill: float | None = None
    submitted_at: str | None = None
    completed_at: str | None = None


class RiskEventResponse(_EntityModel):
    event_id: str
    event_type: str
    severity: str
    details: str | None = None
    timestamp: str | None = None
    market_id: str | None = None
    strategy: str | None = None
    decision: str | None = None
    reason: str | None = None


# ── Pagination ──────────────────────────────────────────────────────


class PaginationMeta(BaseModel):
    total: int
    limit: int
    offset: int
    has_more: bool


class PaginatedResponse(BaseModel, Generic[T]):
    model_config = ConfigDict(from_attributes=True)

    items: list[T]
    pagination: PaginationMeta


# ── Health & system ─────────────────────────────────────────────────


class HealthCheckDetail(BaseModel):
    healthy: bool
    last_updated: str | None = None


class HealthResponse(BaseModel):
    healthy: bool
    checks: dict[str, HealthCheckDetail]
    timestamp: str


class CircuitBreakerInfo(BaseModel):
    state: str
    reasons: list[str] = []
    triggered_at: str | None = None


class SystemStatusResponse(BaseModel):
    mode: str
    trading_enabled: bool
    live_enabled: bool
    database_connected: bool
    schema_version: int
    circuit_breaker: CircuitBreakerInfo | None = None
    version: str
    uptime_seconds: float
    started_at: str | None = None
    timestamp: str


# ── Risk & performance summaries ────────────────────────────────────


class ExposureSummary(BaseModel):
    total_exposure: float
    open_positions: int


class RiskResponse(BaseModel):
    exposure: ExposureSummary
    limits: dict[str, float | int]
    events: PaginatedResponse[RiskEventResponse]


class PerformanceResponse(BaseModel):
    total_realised_pnl: float
    total_unrealised_pnl: float
    total_pnl: float
    open_positions: int
    total_markets: int
    total_signals: int
    total_orders: int
    filled_orders: int
    timestamp: str


# ── Dashboard aggregates ────────────────────────────────────────────


class DashboardOverviewResponse(BaseModel):
    """Headline numbers for the dashboard Overview page."""

    account_balance: float
    available_balance: float
    today_pnl: float
    total_pnl: float
    realized_pnl: float
    unrealized_pnl: float
    max_drawdown: float
    total_exposure: float
    open_positions: int
    active_signals: int
    bot_mode: str
    circuit_breaker: CircuitBreakerInfo | None = None


class EquityPoint(BaseModel):
    """A single point on the equity curve."""

    timestamp: str
    equity: float


class EquityResponse(BaseModel):
    """Historical equity curve derived from persisted order fills."""

    points: list[EquityPoint]


class DashboardRiskResponse(BaseModel):
    """Risk utilisation summary for the dashboard Risk page.

    Bundles every value the dedicated Risk page renders — account,
    loss-control, market, system, and circuit-breaker state — so the
    frontend displays only backend-computed numbers and never derives
    risk controls client-side.
    """

    # ACCOUNT RISK
    account_balance: float
    available_balance: float
    exposure: float
    exposure_pct: float
    exposure_limit: float

    # LOSS CONTROL
    today_pnl: float
    daily_loss: float
    daily_loss_limit: float
    consecutive_losses: int
    consecutive_loss_limit: int

    # MARKET RISK
    open_positions: int
    max_open_positions: int
    largest_position: float
    largest_position_market: str | None = None
    largest_market_exposure: float
    average_spread: float | None = None
    minimum_liquidity: float | None = None

    # SYSTEM RISK (freshness + live health checks)
    spread_status: str
    liquidity_status: str
    data_freshness: str

    # CIRCUIT BREAKER
    circuit_breaker: CircuitBreakerInfo | None = None
