"""Read-only FastAPI backend for the Polymarket Quant Bot.

Exposes health, status, and database-backed listing endpoints.  The API
is strictly read-only: there are no order-submission or configuration
mutation endpoints, and no credentials are ever serialized.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.routes import (
    audit,
    health,
    markets,
    orders,
    performance,
    positions,
    risk,
    signals,
    status,
)
from app.storage.db import Database, DatabaseError

logger = logging.getLogger(__name__)

API_VERSION = "1.0.0"


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Structured request logging with a per-request correlation ID."""

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        request_id = uuid.uuid4().hex[:8]
        request.state.request_id = request_id
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "api_error method=%s path=%s request_id=%s",
                request.method,
                request.url.path,
                request_id,
            )
            raise
        duration_ms = (time.perf_counter() - started) * 1000.0
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "api_request method=%s path=%s status=%d duration_ms=%.2f "
            "request_id=%s",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
            request_id,
        )
        return response


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "unknown")


def _error_response(status_code: int, detail: str, request: Request) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "detail": detail,
            "request_id": _request_id(request),
        },
    )


def create_app(database: Database | None = None) -> FastAPI:
    """Build and configure the FastAPI application.

    ``database`` may be injected for tests; otherwise the global
    ``Database`` (backed by ``settings.database_url``) is used.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = app.state.db
        if not db.is_connected:
            await db.connect()
            await db.init_schema()
        app.state.started_at = datetime.now(UTC)
        logger.info(
            "api_startup db_path=%s schema_ready=%s",
            db.db_path,
            db.is_connected,
        )
        yield
        await db.close()
        logger.info("api_shutdown")

    app = FastAPI(
        title="Polymarket Quant Bot API",
        description=(
            "Read-only API: health, system status, markets, signals, "
            "positions, orders, risk, performance, and audit trail. "
            "No trading endpoints are exposed."
        ),
        version=API_VERSION,
        lifespan=lifespan,
    )

    app.state.db = database or Database()
    app.state.started_at = None

    app.add_middleware(RequestLoggingMiddleware)

    app.include_router(health.router)
    app.include_router(status.router)
    app.include_router(markets.router)
    app.include_router(signals.router)
    app.include_router(positions.router)
    app.include_router(orders.router)
    app.include_router(risk.router)
    app.include_router(performance.router)
    app.include_router(audit.router)

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        logger.warning(
            "api_validation_error path=%s request_id=%s errors=%s",
            request.url.path,
            _request_id(request),
            jsonable_encoder(exc.errors()),
        )
        return JSONResponse(
            status_code=422,
            content={
                "detail": "validation_error",
                "errors": jsonable_encoder(exc.errors()),
                "request_id": _request_id(request),
            },
        )

    @app.exception_handler(DatabaseError)
    async def on_database_error(request: Request, exc: DatabaseError) -> JSONResponse:
        logger.warning(
            "api_database_error path=%s request_id=%s error=%s",
            request.url.path,
            _request_id(request),
            exc,
        )
        return _error_response(503, "database_unavailable", request)

    @app.exception_handler(HTTPException)
    async def on_http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return _error_response(exc.status_code, str(exc.detail), request)

    @app.exception_handler(Exception)
    async def on_unhandled_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "api_unhandled_error path=%s request_id=%s error=%s",
            request.url.path,
            _request_id(request),
            exc,
        )
        return _error_response(500, "internal_server_error", request)

    return app


app = create_app()
