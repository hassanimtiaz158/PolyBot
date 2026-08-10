"""Read-only dashboard endpoints.

Every endpoint in this module is a ``GET`` that aggregates persisted
database state (or live health/status) for the Streamlit dashboard.
There are no order-submission, configuration, or trading endpoints
here, and no secrets (API keys, private keys, tokens) are ever
serialized in responses.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query

from app.api import dashboard_service
from app.api.dependencies import get_db
from app.api.models import (
    DashboardOverviewResponse,
    DashboardRiskResponse,
    EquityResponse,
    HealthCheckDetail,
    HealthResponse,
    MarketResponse,
    OrderResponse,
    PaginatedResponse,
    PaginationMeta,
    PerformanceResponse,
    PositionResponse,
    RiskEventResponse,
    SignalResponse,
)
from app.monitoring.health import checks, health_status
from app.storage.db import Database
from app.storage.repositories import (
    MarketRepository,
    OrderRepository,
    PositionRepository,
    RiskEventRepository,
    SignalRepository,
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
) -> PaginatedResponse[SignalResponse]:
    """Return a paginated list of signals for the dashboard."""
    repo = SignalRepository(db)
    items, total = await repo.list_paginated(
        limit=limit, offset=offset, decision=decision or None
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
) -> PaginatedResponse[PositionResponse]:
    """Return a paginated list of positions for the dashboard."""
    repo = PositionRepository(db)
    items, total = await repo.list_paginated(
        limit=limit, offset=offset, open_only=open_only
    )
    return PaginatedResponse(
        items=[PositionResponse.model_validate(p) for p in items],
        pagination=PaginationMeta(
            total=total,
            limit=limit,
            offset=offset,
            has_more=offset + len(items) < total,
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
) -> PaginatedResponse[OrderResponse]:
    """Return a paginated list of orders for the dashboard."""
    repo = OrderRepository(db)
    items, total = await repo.list_paginated(
        limit=limit, offset=offset, status=status or None
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
) -> PerformanceResponse:
    """Return P&L totals and activity counts for the dashboard."""
    position_repo = PositionRepository(db)
    order_repo = OrderRepository(db)
    pnl = await position_repo.pnl_summary()
    return PerformanceResponse(
        total_realised_pnl=pnl["total_realised_pnl"],
        total_unrealised_pnl=pnl["total_unrealised_pnl"],
        total_pnl=pnl["total_realised_pnl"] + pnl["total_unrealised_pnl"],
        open_positions=await position_repo.count(open_only=True),
        total_markets=await MarketRepository(db).count(),
        total_signals=await SignalRepository(db).count(),
        total_orders=await order_repo.count(),
        filled_orders=await order_repo.count_filled(),
        timestamp=datetime.now(UTC).isoformat(),
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
    results: dict[str, bool] = {"database": await db.health()}
    for name in ("data_freshness", "api", "model_availability"):
        try:
            results[name] = await checks[name].check()
        except Exception:
            results[name] = False
    return HealthResponse(
        healthy=all(results.values()),
        checks={
            name: HealthCheckDetail(
                healthy=ok,
                last_updated=health_status.last_updated.get(name),
            )
            for name, ok in results.items()
        },
        timestamp=datetime.now(UTC).isoformat(),
    )


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
) -> PaginatedResponse[RiskEventResponse]:
    """Return a paginated audit trail for the dashboard."""
    repo = RiskEventRepository(db)
    items, total = await repo.list_paginated(
        limit=limit, offset=offset, severity=severity or None
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
