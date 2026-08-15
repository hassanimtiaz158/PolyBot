"""Position listing endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_db
from app.api.models import PaginatedResponse, PaginationMeta, PositionResponse
from app.storage.db import Database
from app.storage.repositories import PositionRepository

router = APIRouter(tags=["positions"])


@router.get(
    "/positions",
    response_model=PaginatedResponse[PositionResponse],
    summary="List positions",
)
async def list_positions(
    db: Database = Depends(get_db),
    limit: int = Query(50, ge=1, le=100, description="Page size"),
    offset: int = Query(0, ge=0, description="Number of rows to skip"),
    side: str | None = Query(
        None, min_length=1, max_length=10, description="Filter by side (YES/NO)"
    ),
    open_only: bool = Query(True, description="Only return open positions"),
) -> PaginatedResponse[PositionResponse]:
    """Return a paginated list of positions from the database."""
    repo = PositionRepository(db)
    rows, total = await repo.list_paginated(
        limit=limit, offset=offset, side=side or None, open_only=open_only
    )
    items = [PositionResponse.model_validate(r) for r in rows]
    return PaginatedResponse(
        items=items,
        pagination=PaginationMeta(
            total=total,
            limit=limit,
            offset=offset,
            has_more=offset + len(items) < total,
        ),
    )
