"""Market listing endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_db
from app.api.models import MarketResponse, PaginatedResponse, PaginationMeta
from app.storage.db import Database
from app.storage.repositories import MarketRepository

router = APIRouter(tags=["markets"])


@router.get("/markets", response_model=PaginatedResponse[MarketResponse], summary="List markets")
async def list_markets(
    db: Database = Depends(get_db),
    limit: int = Query(50, ge=1, le=100, description="Page size"),
    offset: int = Query(0, ge=0, description="Number of rows to skip"),
    status: str | None = Query(
        None, min_length=1, max_length=50, description="Filter by market status"
    ),
) -> PaginatedResponse[MarketResponse]:
    """Return a paginated list of markets read from the database."""
    repo = MarketRepository(db)
    items, total = await repo.list_paginated(
        limit=limit, offset=offset, status=status or None
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
