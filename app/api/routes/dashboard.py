"""Read-only dashboard endpoints.

Every endpoint in this module is a ``GET`` that aggregates persisted
database state (or live health/status) for the Streamlit dashboard.
There are no order-submission, configuration, or trading endpoints
here, and no secrets (API keys, private keys, tokens) are ever
serialized in responses.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api import dashboard_service
from app.api.dependencies import get_db
from app.api.models import (
    DashboardOverviewResponse,
    DashboardRiskResponse,
    EquityResponse,
    HealthResponse,
    MarketResponse,
    MarketSnapshotResponse,
    OrderResponse,
    PaginatedResponse,
    PaginationMeta,
    PerformanceResponse,
    PositionResponse,
    RiskEventResponse,
    SignalResponse,
)
from app.storage.db import Database
from app.storage.repositories import (
    MarketRepository,
    OrderRepository,
    RiskEventRepository,
    SignalRepository,
    SnapshotRepository,
)

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get(
    "/overview",
    response_model=DashboardOverviewResponse,
    summary="Dashboard overview",
)
async def get_dashboard_overview(
    db: Database = Depends(get_db),
) -> DashboardOverviewResponse:
    """Return headline account, P&L, exposure, and status numbers."""
    return await dashboard_service.build_overview(db)


@router.get(
    "/equity",
    response_model=EquityResponse,
    summary="Equity curve",
)
async def get_dashboard_equity(
    db: Database = Depends(get_db),
) -> EquityResponse:
    """Return the equity curve reconstructed from filled orders."""
    return EquityResponse(points=await dashboard_service.equity_curve(db))


@router.get(
    "/signals",
    response_model=PaginatedResponse[SignalResponse],
    summary="Dashboard signals",
)
async def get_dashboard_signals(
    db: Database = Depends(get_db),
    limit: int = Query(50, ge=1, le=100, description="Page size"),
    offset: int = Query(0, ge=0, description="Number of rows to skip"),
    decision: str | None = Query(
        None, min_length=1, max_length=32, description="Filter by decision"
    ),
    strategy: str | None = Query(
        None, min_length=1, max_length=64, description="Filter by strategy"
    ),
    market_id: str | None = Query(
        None, min_length=1, max_length=128, description="Filter by market"
    ),
    min_edge: float | None = Query(
        None, description="Minimum net edge threshold"
    ),
    min_confidence: float | None = Query(
        None, description="Minimum confidence threshold"
    ),
    sort_by: str | None = Query(
        None,
        description="Sort column (timestamp, net_edge, confidence, gross_edge, "
        "model_probability, implied_probability)",
    ),
    sort_order: str = Query(
        "desc", description="Sort direction: asc or desc"
    ),
) -> PaginatedResponse[SignalResponse]:
    """Return a paginated list of signals for the dashboard."""
    repo = SignalRepository(db)
    items, total = await repo.list_paginated(
        limit=limit,
        offset=offset,
        decision=decision or None,
        strategy=strategy or None,
        market_id=market_id or None,
        min_edge=min_edge,
        min_confidence=min_confidence,
        sort_by=sort_by or None,
        sort_order=sort_order,
    )
    return PaginatedResponse(
        items=[SignalResponse.model_validate(s) for s in items],
        pagination=PaginationMeta(
            total=total,
            limit=limit,
            offset=offset,
            has_more=offset + len(items) < total,
        ),
    )


@router.get(
    "/markets",
    response_model=PaginatedResponse[MarketResponse],
    summary="Dashboard markets",
)
async def get_dashboard_markets(
    db: Database = Depends(get_db),
    limit: int = Query(50, ge=1, le=100, description="Page size"),
    offset: int = Query(0, ge=0, description="Number of rows to skip"),
    status: str | None = Query(
        None, min_length=1, max_length=50, description="Filter by market status"
    ),
) -> PaginatedResponse[MarketResponse]:
    """Return a paginated list of markets for the dashboard."""
    repo = MarketRepository(db)
    items, total = await repo.list_paginated(
        limit=limit, offset=offset, status=status or None
    )
    return PaginatedResponse(
        items=[MarketResponse.model_validate(m) for m in items],
        pagination=PaginationMeta(
            total=total,
            limit=limit,
            offset=offset,
            has_more=offset + len(items) < total,
        ),
    )


@router.get(
    "/positions",
    response_model=PaginatedResponse[PositionResponse],
    summary="Dashboard positions",
)
async def get_dashboard_positions(
    db: Database = Depends(get_db),
    limit: int = Query(50, ge=1, le=100, description="Page size"),
    offset: int = Query(0, ge=0, description="Number of rows to skip"),
    open_only: bool = Query(True, description="Only return open positions"),
    market_id: str | None = Query(
        None, min_length=1, max_length=128, description="Filter by market ID"
    ),
) -> PaginatedResponse[PositionResponse]:
    """Return a paginated, enriched list of positions for the dashboard."""
    items, total = await dashboard_service.build_positions(
        db, limit=limit, offset=offset, open_only=open_only, market_id=market_id
    )
    return PaginatedResponse(
        items=items,
        pagination=PaginationMeta(
            total=total,
            limit=limit,
            offset=offset,
            has_more=offset + len(items) < total,
        ),
    )


@router.get(
    "/markets/{market_id}/snapshots",
    response_model=PaginatedResponse[MarketSnapshotResponse],
    summary="Dashboard market price history",
)
async def get_dashboard_market_snapshots(
    market_id: str,
    db: Database = Depends(get_db),
    limit: int = Query(100, ge=1, le=500, description="Page size"),
    offset: int = Query(0, ge=0, description="Number of rows to skip"),
) -> PaginatedResponse[MarketSnapshotResponse]:
    """Return recent market_snapshots rows (price history for a market)."""
    rows = await SnapshotRepository(db).list_by_market(
        market_id, limit=limit, offset=offset
    )
    total = await SnapshotRepository(db).count_by_market(market_id)
    return PaginatedResponse(
        items=[MarketSnapshotResponse.model_validate(s) for s in rows],
        pagination=PaginationMeta(
            total=total,
            limit=limit,
            offset=offset,
            has_more=offset + len(rows) < total,
        ),
    )


@router.get(
    "/orders",
    response_model=PaginatedResponse[OrderResponse],
    summary="Dashboard orders",
)
async def get_dashboard_orders(
    db: Database = Depends(get_db),
    limit: int = Query(50, ge=1, le=100, description="Page size"),
    offset: int = Query(0, ge=0, description="Number of rows to skip"),
    status: str | None = Query(
        None, min_length=1, max_length=32, description="Filter by order status"
    ),
    market_id: str | None = Query(
        None, min_length=1, max_length=128, description="Filter by market ID"
    ),
) -> PaginatedResponse[OrderResponse]:
    """Return a paginated list of orders for the dashboard."""
    repo = OrderRepository(db)
    items, total = await repo.list_paginated(
        limit=limit, offset=offset, status=status or None, market_id=market_id or None
    )
    return PaginatedResponse(
        items=[OrderResponse.model_validate(o) for o in items],
        pagination=PaginationMeta(
            total=total,
            limit=limit,
            offset=offset,
            has_more=offset + len(items) < total,
        ),
    )


@router.get(
    "/performance",
    response_model=PerformanceResponse,
    summary="Dashboard performance",
)
async def get_dashboard_performance(
    db: Database = Depends(get_db),
    from_date: str | None = Query(
        None,
        min_length=8,
        max_length=32,
        description="Start of analysis window (YYYY-MM-DD or ISO datetime, inclusive)",
    ),
    to_date: str | None = Query(
        None,
        min_length=8,
        max_length=32,
        description="End of analysis window (YYYY-MM-DD or ISO datetime, inclusive)",
    ),
) -> PerformanceResponse:
    """Return P&L totals, trade statistics, and chart series.

    Every value is computed server-side from persisted order fills and
    attributed signals; ``from_date`` / ``to_date`` bound the analysis
    window for the statistics and charts.
    """
    return await dashboard_service.build_performance(
        db, from_date=from_date, to_date=to_date
    )


@router.get(
    "/risk",
    response_model=DashboardRiskResponse,
    summary="Dashboard risk",
)
async def get_dashboard_risk(
    db: Database = Depends(get_db),
) -> DashboardRiskResponse:
    """Return risk utilisation against configured limits for the dashboard."""
    return await dashboard_service.build_risk(db)


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Dashboard health",
)
async def get_dashboard_health(
    db: Database = Depends(get_db),
) -> HealthResponse:
    """Report the health of every registered check for the dashboard."""
    return await dashboard_service.build_health(db)


@router.get(
    "/audit",
    response_model=PaginatedResponse[RiskEventResponse],
    summary="Dashboard audit trail",
)
async def get_dashboard_audit(
    db: Database = Depends(get_db),
    limit: int = Query(50, ge=1, le=100, description="Page size"),
    offset: int = Query(0, ge=0, description="Number of rows to skip"),
    severity: str | None = Query(
        None, min_length=1, max_length=16, description="Filter by severity"
    ),
    market_id: str | None = Query(
        None, min_length=1, max_length=128, description="Filter by market ID"
    ),
) -> PaginatedResponse[RiskEventResponse]:
    """Return a paginated audit trail for the dashboard."""
    repo = RiskEventRepository(db)
    items, total = await repo.list_paginated(
        limit=limit, offset=offset, severity=severity or None, market_id=market_id or None
    )
    return PaginatedResponse(
        items=[RiskEventResponse.model_validate(r) for r in items],
        pagination=PaginationMeta(
            total=total,
            limit=limit,
            offset=offset,
            has_more=offset + len(items) < total,
        ),
    )
