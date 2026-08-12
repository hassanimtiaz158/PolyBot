"""Backend-controlled emergency kill switch endpoints.

These are the **only** writable endpoints in the system.  Every request
must present an ``X-API-Key`` header matching ``POLY_CONTROL_KEY``.  When
that key is not configured, the whole control interface is disabled
(HTTP 503) — it is never left open.

Every command is audited (``KILL_SWITCH`` / ``KILL_SWITCH_RESUMED``)
with the operator identity recorded from the request body.  The kill
switch itself is held by :class:`app.risk.kill_switch.KillSwitch` and
persists to SQLite, so a ``KILLED`` state survives a restart and trading
stays stopped until an operator explicitly resumes with ``confirm=true``.

The frontend can never manipulate execution state through these routes:
they require the dedicated control key, accept no trade parameters, and
never expose credentials.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.dependencies import get_db
from app.api.models import KillSwitchControlRequest, KillSwitchResponse
from app.audit.events import EventBus
from app.config.settings import settings
from app.risk.kill_switch import KillSwitch, KillSwitchResumeError
from app.storage.db import Database
from app.storage.repositories import RiskEventRepository

router = APIRouter(prefix="/api/control", tags=["control"])


def require_control_key(request: Request) -> None:
    """Require the dedicated control API key (always authenticated).

    The control interface is enabled only when ``POLY_CONTROL_KEY`` is
    configured.  When it is not, the endpoints return ``503`` — a public
    kill switch must never exist.
    """
    control_key = settings.poly_control_key
    if not control_key:
        raise HTTPException(status_code=503, detail="control interface disabled")
    provided = request.headers.get("X-API-Key", "")
    if not provided or not secrets.compare_digest(provided, control_key):
        raise HTTPException(status_code=401, detail="invalid or missing control API key")


def _kill_switch(db: Database) -> KillSwitch:
    """Build a kill switch bound to the request's database with auditing."""
    bus = EventBus(repo=RiskEventRepository(db))
    return KillSwitch(db=db, event_bus=bus)


@router.post(
    "/kill-switch",
    response_model=KillSwitchResponse,
    summary="Engage the emergency kill switch",
    description=(
        "Stops all new order submission.  Existing positions remain "
        "visible and are unaffected.  The state is persisted and nothing "
        "automatically resumes it after a restart."
    ),
    dependencies=[Depends(require_control_key)],
)
async def engage_kill_switch(
    body: KillSwitchControlRequest,
    db: Database = Depends(get_db),
) -> KillSwitchResponse:
    """Engage the kill switch (idempotent)."""
    ks = _kill_switch(db)
    await ks.load_state()
    state = await ks.kill(operator=body.operator)
    return KillSwitchResponse(
        state=state.value,
        reason=ks.reason,
        killed_at=ks.killed_at,
        killed_by=ks.killed_by,
        trading_enabled=False,
    )


@router.post(
    "/resume",
    response_model=KillSwitchResponse,
    summary="Resume trading after an emergency stop",
    description=(
        "Resumes trading only with explicit confirmation "
        "(``confirm: true``) in the request body.  Without confirmation "
        "the request fails with 409 and the switch stays KILLED.  Never "
        "automatically resumes after a restart."
    ),
    dependencies=[Depends(require_control_key)],
)
async def resume_kill_switch(
    body: KillSwitchControlRequest,
    db: Database = Depends(get_db),
) -> KillSwitchResponse:
    """Resume trading from a stop (requires explicit confirmation)."""
    if not body.confirm:
        raise HTTPException(
            status_code=409,
            detail="resume requires explicit confirmation (confirm=true)",
        )
    ks = _kill_switch(db)
    await ks.load_state()
    try:
        state = await ks.resume(operator=body.operator, confirm=True)
    except KillSwitchResumeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return KillSwitchResponse(
        state=state.value,
        reason=None,
        killed_at=None,
        killed_by=None,
        trading_enabled=True,
    )
