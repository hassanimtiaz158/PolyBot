"""WebSocket endpoint for real-time dashboard updates.

Serves ``/ws/dashboard``.  Clients receive a stream of typed events
(``MARKET_UPDATE``, ``SIGNAL_UPDATE``, ``POSITION_UPDATE``,
``ORDER_UPDATE``, ``P&L_UPDATE``, ``RISK_UPDATE``, ``HEALTH_UPDATE``,
``CIRCUIT_BREAKER``) pushed by the read-only ``DashboardBroadcaster``.

Authentication is enforced here rather than in the HTTP middleware,
because browsers cannot set custom headers on WebSocket handshakes.  When
``POLY_API_KEY`` is configured, the key must be supplied as the
``apiKey`` query parameter.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.config.settings import settings

logger = logging.getLogger(__name__)

router = APIRouter()

HEARTBEAT_SECONDS = 20.0

# 4401 is a private close code used here to signal authentication failure
# without leaking implementation detail into the client handshake.
AUTH_CLOSE_CODE = 4401
INTERNAL_ERROR_CLOSE_CODE = 1011


def _now() -> str:
    return datetime.now(UTC).isoformat()


@router.websocket("/ws/dashboard")
async def dashboard_ws(websocket: WebSocket) -> None:
    """Stream dashboard events to a single connected client."""
    if settings.poly_api_key:
        provided = websocket.query_params.get("apiKey", "")
        if not provided or not secrets.compare_digest(provided, settings.poly_api_key):
            await websocket.close(code=AUTH_CLOSE_CODE)
            return

    broadcaster = websocket.app.state.broadcaster
    await websocket.accept()
    await websocket.send_text(json.dumps({"type": "CONNECTED", "timestamp": _now(), "data": None}))

    queue = await broadcaster.subscribe()

    async def _watch_disconnect() -> None:
        # Clients never send data; awaiting receive lets us notice a
        # closed connection immediately instead of on the next ping.
        try:
            await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception:
            pass

    watcher = asyncio.create_task(_watch_disconnect())
    try:
        while not watcher.done():
            get_task = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait(
                {get_task, watcher},
                return_when=asyncio.FIRST_COMPLETED,
                timeout=HEARTBEAT_SECONDS,
            )
            if not get_task.done():
                # Discard the stale queue-wait; a cancelled get raises
                # only CancelledError, so nothing to retrieve.
                get_task.cancel()
            if watcher in done:
                break
            if get_task in done:
                await websocket.send_text(get_task.result())
            else:
                # Keep the connection alive while no data changed.
                await websocket.send_text(
                    json.dumps({"type": "PING", "timestamp": _now(), "data": None})
                )
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("dashboard websocket error")
        try:
            await websocket.close(code=INTERNAL_ERROR_CLOSE_CODE)
        except Exception:
            pass
    finally:
        watcher.cancel()
        await broadcaster.unsubscribe(queue)
