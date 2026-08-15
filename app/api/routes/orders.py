"""Order listing endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_db
from app.api.models import OrderResponse, PaginatedResponse, PaginationMeta
from app.storage.db import Database
from app.storage.repositories import OrderRepository

router = APIRouter(tags=["orders"])


@router.get("/orders", response_model=PaginatedResponse[OrderResponse], summary="List orders")
async def list_orders(
    db: Database = Depends(get_db),
    limit: int = Query(50, ge=1, le=100, description="Page size"),
    offset: int = Query(0, ge=0, description="Number of rows to skip"),
    market_id: str | None = Query(
        None, min_length=1, max_length=128, description="Filter by market ID"
    ),
    status: str | None = Query(
        None, min_length=1, max_length=32, description="Filter by order status"
    ),
) -> PaginatedResponse[OrderResponse]:
    """Return a paginated list of orders from the database."""
    repo = OrderRepository(db)
    rows, total = await repo.list_paginated(
        limit=limit,
        offset=offset,
        market_id=market_id or None,
        status=status or None,
    )
    items = [OrderResponse.model_validate(r) for r in rows]
    return PaginatedResponse(
        items=items,
        pagination=PaginationMeta(
            total=total,
            limit=limit,
            offset=offset,
            has_more=offset + len(items) < total,
        ),
    )
