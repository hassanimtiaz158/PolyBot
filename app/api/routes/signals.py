"""Signal listing endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_db
from app.api.models import PaginatedResponse, PaginationMeta, SignalResponse
from app.storage.db import Database
from app.storage.repositories import SignalRepository

router = APIRouter(tags=["signals"])


@router.get("/signals", response_model=PaginatedResponse[SignalResponse], summary="List signals")
async def list_signals(
    db: Database = Depends(get_db),
    limit: int = Query(50, ge=1, le=100, description="Page size"),
    offset: int = Query(0, ge=0, description="Number of rows to skip"),
    market_id: str | None = Query(
        None, min_length=1, max_length=128, description="Filter by market ID"
    ),
    strategy: str | None = Query(
        None, min_length=1, max_length=64, description="Filter by strategy name"
    ),
    decision: str | None = Query(
        None, min_length=1, max_length=32, description="Filter by decision"
    ),
) -> PaginatedResponse[SignalResponse]:
    """Return a paginated list of strategy signals from the database."""
    repo = SignalRepository(db)
    items, total = await repo.list_paginated(
        limit=limit,
        offset=offset,
        market_id=market_id or None,
        strategy=strategy or None,
        decision=decision or None,
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
