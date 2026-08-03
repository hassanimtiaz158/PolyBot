"""Performance summary endpoint."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends

from app.api.dependencies import get_db
from app.api.models import PerformanceResponse
from app.storage.db import Database
from app.storage.repositories import (
    MarketRepository,
    OrderRepository,
    PositionRepository,
    SignalRepository,
)

router = APIRouter(tags=["performance"])


@router.get("/performance", response_model=PerformanceResponse, summary="Performance summary")
async def get_performance(db: Database = Depends(get_db)) -> PerformanceResponse:
    """Return P&L totals and activity counts derived from the database."""
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
