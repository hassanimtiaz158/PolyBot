"""Risk summary endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_db
from app.api.models import (
    ExposureSummary,
    PaginatedResponse,
    PaginationMeta,
    RiskEventResponse,
    RiskResponse,
)
from app.config.settings import settings
from app.storage.db import Database
from app.storage.repositories import PositionRepository, RiskEventRepository

router = APIRouter(tags=["risk"])


def _risk_limits() -> dict[str, float | int]:
    """Expose the configured hard limits (public configuration, no secrets)."""
    return {
        "max_position_pct": settings.max_position_pct,
        "max_market_exposure_pct": settings.max_market_exposure_pct,
        "max_total_exposure_pct": settings.max_total_exposure_pct,
        "max_daily_loss_pct": settings.max_daily_loss_pct,
        "max_consecutive_losses": settings.max_consecutive_losses,
        "max_open_positions": settings.max_open_positions,
        "max_spread": settings.max_spread,
        "min_liquidity": settings.min_liquidity,
        "min_net_edge": settings.min_net_edge,
        "min_confidence": settings.min_confidence,
        "data_max_age_seconds": settings.data_max_age_seconds,
    }


@router.get("/risk", response_model=RiskResponse, summary="Risk summary")
async def get_risk(
    db: Database = Depends(get_db),
    limit: int = Query(20, ge=1, le=100, description="Page size for events"),
    offset: int = Query(0, ge=0, description="Number of events to skip"),
) -> RiskResponse:
    """Return exposure, configured limits, and recent risk events."""
    position_repo = PositionRepository(db)
    event_repo = RiskEventRepository(db)
    total_exposure = await position_repo.total_exposure()
    open_positions = await position_repo.count(open_only=True)
    raw_events, event_total = await event_repo.list_paginated(
        limit=limit, offset=offset
    )
    events = [RiskEventResponse.model_validate(e) for e in raw_events]
    return RiskResponse(
        exposure=ExposureSummary(
            total_exposure=total_exposure, open_positions=open_positions
        ),
        limits=_risk_limits(),
        events=PaginatedResponse(
            items=events,
            pagination=PaginationMeta(
                total=event_total,
                limit=limit,
                offset=offset,
                has_more=offset + len(events) < event_total,
            ),
        ),
    )
