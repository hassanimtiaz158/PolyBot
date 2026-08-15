"""Audit trail endpoint.

The audit trail is the persisted ``risk_events`` table — every risk
event is written there with a structured type, severity, and details.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_db
from app.api.models import PaginatedResponse, PaginationMeta, RiskEventResponse
from app.storage.db import Database
from app.storage.repositories import RiskEventRepository

router = APIRouter(tags=["audit"])


@router.get("/audit", response_model=PaginatedResponse[RiskEventResponse], summary="Audit trail")
async def get_audit(
    db: Database = Depends(get_db),
    limit: int = Query(50, ge=1, le=100, description="Page size"),
    offset: int = Query(0, ge=0, description="Number of rows to skip"),
    event_type: str | None = Query(
        None, min_length=1, max_length=64, description="Filter by event type"
    ),
    severity: str | None = Query(
        None, min_length=1, max_length=16, description="Filter by severity"
    ),
) -> PaginatedResponse[RiskEventResponse]:
    """Return a paginated audit trail from the database."""
    repo = RiskEventRepository(db)
    rows, total = await repo.list_paginated(
        limit=limit,
        offset=offset,
        event_type=event_type or None,
        severity=severity or None,
    )
    items = [RiskEventResponse.model_validate(r) for r in rows]
    return PaginatedResponse(
        items=items,
        pagination=PaginationMeta(
            total=total,
            limit=limit,
            offset=offset,
            has_more=offset + len(items) < total,
        ),
    )
