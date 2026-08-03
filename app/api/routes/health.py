"""Health check endpoint."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request

from app.api.dependencies import get_db
from app.api.models import HealthCheckDetail, HealthResponse
from app.monitoring.health import checks, health_status
from app.storage.db import Database

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="System health")
async def get_health(db: Database = Depends(get_db)) -> HealthResponse:
    """Report the health of every registered check.

    A system with no recent market data, no loaded model, or an
    unreachable database reports ``healthy=False``.  Only status
    booleans are exposed — never credentials or configuration.
    """
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
