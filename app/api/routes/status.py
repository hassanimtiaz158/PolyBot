"""System status endpoint."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version

from fastapi import APIRouter, Depends, Request

from app.api.dependencies import get_db
from app.api.models import (
    CircuitBreakerInfo,
    KillSwitchInfo,
    SystemStatusResponse,
)
from app.config.settings import settings
from app.modes.state import ModeState, OperatingMode
from app.storage.db import Database

logger = logging.getLogger(__name__)

router = APIRouter(tags=["system"])

_BREAKER_STATE_KEY = "circuit_breaker"
_KILL_SWITCH_KEY = "kill_switch"


def _package_version() -> str:
    try:
        return version("polymarket-quant-bot")
    except PackageNotFoundError:
        return "0.1.0"


def _operating_mode(raw: str) -> OperatingMode:
    try:
        return OperatingMode(raw)
    except ValueError:
        return OperatingMode.HALTED


@router.get("/system/status", response_model=SystemStatusResponse, summary="System status")
async def get_system_status(
    request: Request, db: Database = Depends(get_db)
) -> SystemStatusResponse:
    """Return operating mode, database state, and circuit breaker status.

    No credentials or private keys are ever included in the response.
    """
    db_ok = await db.health()
    schema_version = 0
    breaker: CircuitBreakerInfo | None = None
    kill_switch: KillSwitchInfo | None = None
    if db_ok:
        try:
            cursor = await db.conn.execute(
                "SELECT MAX(version) FROM _schema_version"
            )
            row = await cursor.fetchone()
            schema_version = int(row[0]) if row and row[0] is not None else 0
            cursor = await db.conn.execute(
                "SELECT value FROM circuit_breaker_state WHERE key = ?",
                (_BREAKER_STATE_KEY,),
            )
            row = await cursor.fetchone()
            if row:
                data = json.loads(row["value"])
                breaker = CircuitBreakerInfo(
                    state=data.get("state", "NORMAL"),
                    reasons=data.get("reasons", []),
                    triggered_at=data.get("triggered_at"),
                )
            cursor = await db.conn.execute(
                "SELECT value FROM circuit_breaker_state WHERE key = ?",
                (_KILL_SWITCH_KEY,),
            )
            row = await cursor.fetchone()
            if row:
                data = json.loads(row["value"])
                kill_switch = KillSwitchInfo(
                    state=data.get("state", "ACTIVE"),
                    reason=data.get("reason"),
                    killed_at=data.get("killed_at"),
                    killed_by=data.get("killed_by"),
                )
        except Exception:
            logger.warning(
                "system_status: failed to read database state", exc_info=True
            )
    mode = _operating_mode(settings.mode)
    mode_state = ModeState(initial_mode=mode)
    started_at = request.app.state.started_at
    now = datetime.now(UTC)
    uptime = (now - started_at).total_seconds() if started_at else 0.0
    return SystemStatusResponse(
        mode=mode.value,
        trading_enabled=mode_state.is_trading(),
        live_enabled=mode_state.is_live(),
        database_connected=db_ok,
        schema_version=schema_version,
        circuit_breaker=breaker,
        kill_switch=kill_switch,
        version=_package_version(),
        uptime_seconds=max(uptime, 0.0),
        started_at=started_at.isoformat() if started_at else None,
        timestamp=now.isoformat(),
    )
