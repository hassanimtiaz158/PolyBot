"""Health check endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api import dashboard_service
from app.api.dependencies import get_db
from app.api.models import HealthResponse
from app.storage.db import Database

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="System health")
async def get_health(db: Database = Depends(get_db)) -> HealthResponse:
    """Report the health of every registered check.

    A system with no recent market data, no loaded model, or an
    unreachable database reports ``healthy=False``.  Only status
    booleans are exposed — never credentials or configuration.

    Delegates to ``dashboard_service.build_health()`` -- the same
    helper used by ``/api/dashboard/health`` -- rather than duplicating
    the check logic here. This API process is a separate OS process
    from the bot (``app.main``), so any in-memory check state would
    never reflect what the bot is actually doing; ``build_health()``
    derives everything from the shared, persisted database instead.
    """
    return await dashboard_service.build_health(db)
