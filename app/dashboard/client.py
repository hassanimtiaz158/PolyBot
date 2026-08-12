"""Read-only API client and deterministic demo data provider.

The dashboard never writes anything: it consumes the read-only FastAPI
backend (``app.api``).  In DEMO mode a synthetic data provider produces
payloads with the exact same shape, so every page shares one code path.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import httpx

from app.config.settings import settings

API_TIMEOUT = 10.0


class ApiError(RuntimeError):
    """Raised when the read-only API cannot be reached or returns an error."""


class ApiClient:
    """Thin synchronous wrapper around the read-only bot API."""

    def __init__(
        self,
        base_url: str,
        timeout: float = API_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport

    def _get(self, path: str, params: dict[str, object] | None = None) -> dict:
        url = f"{self.base_url}{path}"
        try:
            with httpx.Client(transport=self._transport) as client:
                resp = client.get(url, params=params, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise ApiError(f"API unreachable at {url}: {exc}") from exc
        if resp.status_code != 200:
            raise ApiError(
                f"API error {resp.status_code} at {path}: {resp.text[:200]}"
            )
        return resp.json()

    def health(self) -> dict:
        return self._get("/health")

    def status(self) -> dict:
        return self._get("/system/status")

    def markets(
        self, limit: int = 100, offset: int = 0, status: str | None = None
    ) -> dict:
        params: dict[str, object] = {"limit": limit, "offset": offset}
        if status:
            params["status"] = status
        return self._get("/markets", params)

    def signals(
        self,
        limit: int = 100,
        offset: int = 0,
        market_id: str | None = None,
        strategy: str | None = None,
        decision: str | None = None,
    ) -> dict:
        params: dict[str, object] = {"limit": limit, "offset": offset}
        if market_id:
            params["market_id"] = market_id
        if strategy:
            params["strategy"] = strategy
        if decision:
            params["decision"] = decision
        return self._get("/signals", params)

    def positions(
        self,
        limit: int = 100,
        offset: int = 0,
        side: str | None = None,
        open_only: bool = True,
    ) -> dict:
        params: dict[str, object] = {
            "limit": limit,
            "offset": offset,
            "open_only": open_only,
        }
        if side:
            params["side"] = side
        return self._get("/positions", params)

    def orders(
        self,
        limit: int = 100,
        offset: int = 0,
        market_id: str | None = None,
        status: str | None = None,
    ) -> dict:
        params: dict[str, object] = {"limit": limit, "offset": offset}
        if market_id:
            params["market_id"] = market_id
        if status:
            params["status"] = status
        return self._get("/orders", params)

    def risk(self, limit: int = 20, offset: int = 0) -> dict:
        return self._get("/risk", {"limit": limit, "offset": offset})

    def performance(self) -> dict:
        return self._get("/performance")

    def audit(
        self,
        limit: int = 100,
        offset: int = 0,
        event_type: str | None = None,
        severity: str | None = None,
    ) -> dict:
        params: dict[str, object] = {"limit": limit, "offset": offset}
        if event_type:
            params["event_type"] = event_type
        if severity:
            params["severity"] = severity
        return self._get("/audit", params)


# ── Demo data provider ──────────────────────────────────────────────


class DemoProvider:
    """Deterministic synthetic data matching the read-only API shapes.

    Used only when ``DASHBOARD_DEMO=true``; every page is then clearly
    labelled DEMO and no request is sent to the bot.
    """

    def __init__(self, seed: int = 42) -> None:
        self._rng = random.Random(seed)

    @staticmethod
    def _ts(days_ago: float = 0.0) -> str:
        return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()

    def markets(self, limit: int = 100, offset: int = 0, status: str | None = None) -> dict:
        rows = [
            {
                "market_id": "DEMO-ELEC-001",
                "question": "Will the incumbent win the 2026 election?",
                "condition_id": "cond_demo_001",
                "status": "active",
                "resolution_time": "2026-11-03T23:00:00Z",
                "liquidity": 42000.0,
                "created_at": self._ts(30),
                "updated_at": self._ts(0.01),
            },
            {
                "market_id": "DEMO-RATES-002",
                "question": "Will the Fed cut rates before December 2026?",
                "condition_id": "cond_demo_002",
                "status": "active",
                "resolution_time": "2026-12-16T19:00:00Z",
                "liquidity": 38000.0,
                "created_at": self._ts(25),
                "updated_at": self._ts(0.02),
            },
            {
                "market_id": "DEMO-SPORT-003",
                "question": "Will Team A win the championship?",
                "condition_id": "cond_demo_003",
                "status": "active",
                "resolution_time": "2026-09-30T20:00:00Z",
                "liquidity": 15000.0,
                "created_at": self._ts(20),
                "updated_at": self._ts(0.03),
            },
            {
                "market_id": "DEMO-ECON-004",
                "question": "Will inflation print above 3.0% next quarter?",
                "condition_id": "cond_demo_004",
                "status": "active",
                "resolution_time": "2026-10-31T12:30:00Z",
                "liquidity": 8000.0,
                "created_at": self._ts(15),
                "updated_at": self._ts(0.04),
            },
            {
                "market_id": "DEMO-MACRO-005",
                "question": "Will GDP growth exceed 2% this year?",
                "condition_id": "cond_demo_005",
                "status": "closed",
                "resolution_time": "2026-01-15T00:00:00Z",
                "liquidity": 5000.0,
                "created_at": self._ts(60),
                "updated_at": self._ts(5),
            },
        ]
        filtered = [m for m in rows if not status or m["status"] == status]
        page = filtered[offset : offset + limit]
        return {
            "items": page,
            "pagination": {
                "total": len(filtered),
                "limit": limit,
                "offset": offset,
                "has_more": offset + len(page) < len(filtered),
            },
        }

    def signals(
        self,
        limit: int = 100,
        offset: int = 0,
        market_id: str | None = None,
        strategy: str | None = None,
        decision: str | None = None,
    ) -> dict:
        rows = [
            {
                "signal_id": "sig_demo_001",
                "market_id": "DEMO-ELEC-001",
                "strategy": "microstructure",
                "side": "YES",
                "model_probability": 0.518,
                "implied_probability": 0.440,
                "gross_edge": 0.078,
                "estimated_cost": 0.016,
                "net_edge": 0.062,
                "confidence": 0.78,
                "decision": "CANDIDATE",
                "rejection_reason": None,
                "timestamp": self._ts(0.01),
            },
            {
                "signal_id": "sig_demo_002",
                "market_id": "DEMO-RATES-002",
                "strategy": "microstructure",
                "side": "NO",
                "model_probability": 0.390,
                "implied_probability": 0.370,
                "gross_edge": 0.020,
                "estimated_cost": 0.012,
                "net_edge": 0.008,
                "confidence": 0.71,
                "decision": "CANDIDATE",
                "rejection_reason": None,
                "timestamp": self._ts(0.02),
            },
            {
                "signal_id": "sig_demo_003",
                "market_id": "DEMO-SPORT-003",
                "strategy": "microstructure",
                "side": "YES",
                "model_probability": 0.640,
                "implied_probability": 0.610,
                "gross_edge": 0.030,
                "estimated_cost": 0.014,
                "net_edge": 0.016,
                "confidence": 0.66,
                "decision": "CANDIDATE",
                "rejection_reason": None,
                "timestamp": self._ts(0.03),
            },
            {
                "signal_id": "sig_demo_004",
                "market_id": "DEMO-ECON-004",
                "strategy": "probability",
                "side": "YES",
                "model_probability": 0.290,
                "implied_probability": 0.310,
                "gross_edge": -0.020,
                "estimated_cost": 0.014,
                "net_edge": -0.034,
                "confidence": 0.60,
                "decision": "NO_SIGNAL",
                "rejection_reason": "NET_EDGE_TOO_LOW",
                "timestamp": self._ts(0.04),
            },
            {
                "signal_id": "sig_demo_005",
                "market_id": "DEMO-MACRO-005",
                "strategy": "probability",
                "side": "NO",
                "model_probability": 0.420,
                "implied_probability": 0.450,
                "gross_edge": -0.030,
                "estimated_cost": 0.013,
                "net_edge": -0.043,
                "confidence": 0.55,
                "decision": "NO_SIGNAL",
                "rejection_reason": "CONFIDENCE_TOO_LOW",
                "timestamp": self._ts(0.05),
            },
        ]
        filtered = [
            s
            for s in rows
            if (not market_id or s["market_id"] == market_id)
            and (not strategy or s["strategy"] == strategy)
            and (not decision or s["decision"] == decision)
        ]
        page = filtered[offset : offset + limit]
        return {
            "items": page,
            "pagination": {
                "total": len(filtered),
                "limit": limit,
                "offset": offset,
                "has_more": offset + len(page) < len(filtered),
            },
        }

    def positions(
        self,
        limit: int = 100,
        offset: int = 0,
        side: str | None = None,
        open_only: bool = True,
    ) -> dict:
        rows = [
            {
                "position_id": "pos_demo_001",
                "market_id": "DEMO-ELEC-001",
                "side": "YES",
                "size": 12.0,
                "average_entry": 0.438,
                "current_price": 0.451,
                "realised_pnl": 0.0,
                "unrealised_pnl": 0.156,
            },
            {
                "position_id": "pos_demo_002",
                "market_id": "DEMO-RATES-002",
                "side": "NO",
                "size": 8.6,
                "average_entry": 0.608,
                "current_price": 0.603,
                "realised_pnl": 0.0,
                "unrealised_pnl": 0.043,
            },
            {
                "position_id": "pos_demo_003",
                "market_id": "DEMO-SPORT-003",
                "side": "YES",
                "size": 18.0,
                "average_entry": 0.520,
                "current_price": 0.515,
                "realised_pnl": 0.0,
                "unrealised_pnl": -0.090,
            },
            {
                "position_id": "pos_demo_004",
                "market_id": "DEMO-MACRO-005",
                "side": "YES",
                "size": 0.0,
                "average_entry": 0.540,
                "current_price": 0.000,
                "realised_pnl": 1.420,
                "unrealised_pnl": 0.0,
            },
        ]
        filtered = [
            p
            for p in rows
            if (not side or p["side"] == side)
            and (not open_only or p["size"] > 0)
        ]
        page = filtered[offset : offset + limit]
        return {
            "items": page,
            "pagination": {
                "total": len(filtered),
                "limit": limit,
                "offset": offset,
                "has_more": offset + len(page) < len(filtered),
            },
        }

    def orders(
        self,
        limit: int = 100,
        offset: int = 0,
        market_id: str | None = None,
        status: str | None = None,
    ) -> dict:
        rows = [
            {
                "order_id": "ord_demo_001",
                "market_id": "DEMO-ELEC-001",
                "side": "YES",
                "status": "FILLED",
                "requested_price": 0.440,
                "requested_size": 12.0,
                "filled_size": 12.0,
                "average_fill": 0.446,
                "submitted_at": self._ts(0.02),
                "completed_at": self._ts(0.019),
            },
            {
                "order_id": "ord_demo_002",
                "market_id": "DEMO-RATES-002",
                "side": "NO",
                "status": "FILLED",
                "requested_price": 0.608,
                "requested_size": 8.6,
                "filled_size": 8.6,
                "average_fill": 0.604,
                "submitted_at": self._ts(0.04),
                "completed_at": self._ts(0.039),
            },
            {
                "order_id": "ord_demo_003",
                "market_id": "DEMO-SPORT-003",
                "side": "YES",
                "status": "CREATED",
                "requested_price": 0.520,
                "requested_size": 18.0,
                "filled_size": None,
                "average_fill": None,
                "submitted_at": self._ts(0.01),
                "completed_at": None,
            },
            {
                "order_id": "ord_demo_004",
                "market_id": "DEMO-ECON-004",
                "side": "YES",
                "status": "REJECTED",
                "requested_price": 0.310,
                "requested_size": 5.0,
                "filled_size": None,
                "average_fill": None,
                "submitted_at": self._ts(0.05),
                "completed_at": self._ts(0.049),
            },
        ]
        filtered = [
            o
            for o in rows
            if (not market_id or o["market_id"] == market_id)
            and (not status or o["status"] == status)
        ]
        page = filtered[offset : offset + limit]
        return {
            "items": page,
            "pagination": {
                "total": len(filtered),
                "limit": limit,
                "offset": offset,
                "has_more": offset + len(page) < len(filtered),
            },
        }

    def risk(self, limit: int = 20, offset: int = 0) -> dict:
        events = self.audit(limit=limit, offset=offset)
        return {
            "exposure": {
                "total_exposure": 38.6,
                "open_positions": 3,
            },
            "limits": {
                "max_position_pct": settings.max_position_pct,
                "max_market_exposure_pct": settings.max_market_exposure_pct,
                "max_total_exposure_pct": settings.max_total_exposure_pct,
                "max_daily_loss_pct": settings.max_daily_loss_pct,
                "max_consecutive_losses": settings.max_consecutive_losses,
                "max_open_positions": settings.max_open_positions,
                "max_spread": settings.max_spread,
                "min_liquidity": settings.min_liquidity,
                "min_net_edge": settings.min_net_edge,
                "min_confidence": settings.min_confidence,
                "data_max_age_seconds": settings.data_max_age_seconds,
            },
            "events": events,
        }

    def performance(self) -> dict:
        return {
            "total_realised_pnl": 1.42,
            "total_unrealised_pnl": 0.109,
            "total_pnl": 1.529,
            "open_positions": 3,
            "total_markets": 5,
            "total_signals": 5,
            "total_orders": 4,
            "filled_orders": 2,
            "timestamp": self._ts(0),
        }

    def equity_history(self) -> dict:
        """Synthetic equity curve for demo mode only (not provided by the API)."""
        return {
            "points": [
                {"step": 1, "equity": 10000.0},
                {"step": 2, "equity": 10048.0},
                {"step": 3, "equity": 10031.0},
                {"step": 4, "equity": 10087.0},
                {"step": 5, "equity": 10079.0},
                {"step": 6, "equity": 10124.8},
            ]
        }

    def audit(
        self,
        limit: int = 100,
        offset: int = 0,
        event_type: str | None = None,
        severity: str | None = None,
    ) -> dict:
        rows = [
            {
                "event_id": "evt_demo_001",
                "event_type": "RISK_APPROVED",
                "severity": "INFO",
                "details": "All guardrails passed for DEMO-ELEC-001",
                "timestamp": self._ts(0.01),
            },
            {
                "event_id": "evt_demo_002",
                "event_type": "SIGNAL_REJECTED",
                "severity": "LOW",
                "details": "Net edge below threshold for DEMO-ECON-004",
                "timestamp": self._ts(0.04),
            },
            {
                "event_id": "evt_demo_003",
                "event_type": "DATA_RECEIVED",
                "severity": "INFO",
                "details": "Fresh snapshot for DEMO-RATES-002 (0.7s)",
                "timestamp": self._ts(0.05),
            },
            {
                "event_id": "evt_demo_004",
                "event_type": "DAILY_LOSS",
                "severity": "HIGH",
                "details": "Daily loss guardrail monitored — no breach",
                "timestamp": self._ts(0.5),
            },
        ]
        filtered = [
            e
            for e in rows
            if (not event_type or e["event_type"] == event_type)
            and (not severity or e["severity"] == severity)
        ]
        page = filtered[offset : offset + limit]
        return {
            "items": page,
            "pagination": {
                "total": len(filtered),
                "limit": limit,
                "offset": offset,
                "has_more": offset + len(page) < len(filtered),
            },
        }

    def health(self) -> dict:
        now = self._ts(0)
        return {
            "healthy": True,
            "checks": {
                "database": {"healthy": True, "last_updated": now},
                "data_freshness": {"healthy": True, "last_updated": now},
                "api": {"healthy": True, "last_updated": now},
                "model_availability": {"healthy": True, "last_updated": now},
            },
            "timestamp": now,
        }

    def status(self) -> dict:
        return {
            "mode": "DEMO",
            "trading_enabled": False,
            "live_enabled": False,
            "database_connected": True,
            "schema_version": 2,
            "circuit_breaker": {
                "state": "NORMAL",
                "reasons": [],
                "triggered_at": None,
            },
            "kill_switch": {
                "state": "ACTIVE",
                "reason": None,
                "killed_at": None,
                "killed_by": None,
            },
            "version": "0.1.0",
            "uptime_seconds": 42.0,
            "started_at": self._ts(0.0005),
            "timestamp": self._ts(0),
        }
