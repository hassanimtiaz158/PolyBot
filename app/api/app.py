"""Read-only FastAPI backend for the Polymarket Quant Bot.

Exposes health, status, and database-backed listing endpoints.  The API
is strictly read-only: there are no order-submission or configuration
mutation endpoints, and no credentials are ever serialized.

When ``POLY_API_KEY`` is set in the environment, all requests must include
an ``X-API-Key`` header matching that value.  When unset (research mode),
authentication is bypassed.
"""

from __future__ import annotations

import logging
import secrets
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.routes import (
    audit,
    control,
    dashboard,
    dashboard_ws,
    health,
    markets,
    orders,
    performance,
    positions,
    risk,
    signals,
    status,
)
from app.api.websocket_broadcast import DashboardBroadcaster
from app.config.settings import settings
from app.storage.db import Database, DatabaseError

logger = logging.getLogger(__name__)

API_VERSION = "1.0.0"

# Paths that are always public (no auth required).
_PUBLIC_PATHS = frozenset({"/health", "/docs", "/openapi.json", "/redoc"})


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


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to all responses."""

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple in-memory rate limiter per client IP.

    Limits to 100 requests per 60-second window per IP. Returns 429
    when exceeded. For production use, consider a distributed rate
    limiter (Redis, etc.).
    """

    def __init__(self, app: Any, max_requests: int = 100, window_seconds: int = 60) -> None:
        super().__init__(app)
        self._max_requests = max_requests
        self._window = window_seconds
        self._requests: dict[str, list[float]] = {}

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        # Skip rate limiting for health checks and WebSocket
        if request.url.path in _PUBLIC_PATHS or request.scope["type"] == "websocket":
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        now = time.time()
        cutoff = now - self._window

        # Prune old entries
        if client_ip in self._requests:
            self._requests[client_ip] = [
                t for t in self._requests[client_ip] if t > cutoff
            ]
        else:
            self._requests[client_ip] = []

        if len(self._requests[client_ip]) >= self._max_requests:
            return JSONResponse(
                status_code=429,
                content={"detail": "rate limit exceeded"},
            )

        self._requests[client_ip].append(now)
        return await call_next(request)


class APIKeyAuthMiddleware(BaseHTTPMiddleware):
    """Optional API key authentication middleware.

    When ``POLY_API_KEY`` is set, all requests (except public paths) must
    include an ``X-API-Key`` header matching the configured value.
    Timing-safe comparison is used to prevent timing attacks.
    """

    def __init__(self, app: Any, api_key: str | None = None) -> None:
        super().__init__(app)
        self._api_key = api_key

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        # WebSocket handshakes cannot carry the X-API-Key header from a
        # browser, so the /ws/dashboard endpoint enforces auth itself.
        if request.scope["type"] == "websocket":
            return await call_next(request)

        if self._api_key is None:
            return await call_next(request)

        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)

        provided_key = request.headers.get("X-API-Key", "")
        if not provided_key or not secrets.compare_digest(
            provided_key, self._api_key
        ):
            return JSONResponse(
                status_code=401,
                content={"detail": "invalid or missing API key"},
            )

        return await call_next(request)


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

        # Start the read-only change detector that pushes dashboard
        # events over /ws/dashboard.
        broadcaster: DashboardBroadcaster = app.state.broadcaster
        broadcaster.database = db
        broadcaster.start()

        logger.info(
            "api_startup db_path=%s schema_ready=%s",
            db.db_path,
            db.is_connected,
        )
        try:
            yield
        finally:
            await broadcaster.stop()
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
    app.state.broadcaster = DashboardBroadcaster()

    # CORS — restrict to known origins in production.  POST is required
    # only for the keyed /api/control/* endpoints; display is still GET.
    cors_origins = [
        origin.strip()
        for origin in settings.cors_allow_origins.split(",")
        if origin.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins if cors_origins else ["http://localhost:8501"],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    # API key authentication (bypassed when POLY_API_KEY is not set).
    # ``settings.poly_api_key`` is "" rather than None whenever .env
    # defines the key with a blank value (POLY_API_KEY=) -- treat that
    # the same as unset, matching the documented bypass behaviour.
    # However, when live trading is enabled, enforce authentication.
    api_key_value = settings.poly_api_key or None
    if api_key_value is None and settings.live_trading_enabled:
        logger.warning(
            "POLY_API_KEY is not set but LIVE_TRADING_ENABLED=true -- "
            "enforcing authentication with a generated ephemeral key"
        )
        import secrets as _secrets
        api_key_value = _secrets.token_urlsafe(32)
        logger.warning("Ephemeral API key (set POLY_API_KEY in .env): %s", api_key_value)
    app.add_middleware(
        APIKeyAuthMiddleware,
        api_key=api_key_value,
    )

    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestLoggingMiddleware)

    app.include_router(health.router)
    app.include_router(status.router)
    app.include_router(control.router)
    app.include_router(dashboard.router)
    app.include_router(dashboard_ws.router)
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
