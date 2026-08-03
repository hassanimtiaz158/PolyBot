"""Tests for the dashboard API client and the demo data provider."""

from __future__ import annotations

import httpx
import pytest

from app.dashboard.client import ApiClient, ApiError, DemoProvider

BASE_URL = "http://api.test"


def _client(handler: object) -> ApiClient:
    return ApiClient(BASE_URL, transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


class TestApiClient:
    def test_health_returns_payload(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/health"
            assert not request.url.params
            return httpx.Response(200, json={"healthy": True, "checks": {}})

        assert _client(handler).health() == {"healthy": True, "checks": {}}

    def test_status_hits_system_status(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/system/status"
            return httpx.Response(200, json={"mode": "PAPER", "trading_enabled": False})

        client = _client(handler)
        assert client.status()["mode"] == "PAPER"

    def test_markets_sends_query_params(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/markets"
            assert request.url.params["limit"] == "10"
            assert request.url.params["offset"] == "5"
            assert request.url.params["status"] == "active"
            return httpx.Response(200, json={"items": [], "pagination": {}})

        _client(handler).markets(limit=10, offset=5, status="active")

    def test_positions_sends_open_only_and_side(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/positions"
            assert request.url.params["open_only"] == "true"
            assert request.url.params["side"] == "YES"
            return httpx.Response(200, json={"items": [], "pagination": {}})

        _client(handler).positions(side="YES", open_only=True)

    def test_raises_api_error_on_http_error_status(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="service unavailable")

        with pytest.raises(ApiError, match="503"):
            _client(handler).performance()

    def test_raises_api_error_on_connection_failure(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        with pytest.raises(ApiError, match="unreachable"):
            _client(handler).risk()

    def test_audit_sends_event_type_and_severity(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.params["event_type"] == "RISK_APPROVED"
            assert request.url.params["severity"] == "HIGH"
            return httpx.Response(200, json={"items": [], "pagination": {}})

        _client(handler).audit(event_type="RISK_APPROVED", severity="HIGH")


class TestDemoProvider:
    def test_mode_is_demo_and_trading_disabled(self) -> None:
        status = DemoProvider().status()
        assert status["mode"] == "DEMO"
        assert status["trading_enabled"] is False
        assert status["live_enabled"] is False

    def test_health_reports_all_checks_healthy(self) -> None:
        checks = DemoProvider().health()["checks"]
        assert set(checks) == {"database", "data_freshness", "api", "model_availability"}
        assert all(c["healthy"] for c in checks.values())

    def test_markets_are_paginated_and_filterable(self) -> None:
        provider = DemoProvider()
        all_markets = provider.markets()
        assert len(all_markets["items"]) == 5
        assert all_markets["pagination"]["total"] == 5
        closed = provider.markets(status="closed")
        assert len(closed["items"]) == 1
        assert closed["items"][0]["market_id"] == "DEMO-MACRO-005"

    def test_signals_include_edges_and_decisions(self) -> None:
        provider = DemoProvider()
        signals = provider.signals()
        first = signals["items"][0]
        for key in (
            "model_probability",
            "implied_probability",
            "gross_edge",
            "net_edge",
            "confidence",
        ):
            assert key in first
        assert provider.signals(decision="NO_SIGNAL")["pagination"]["total"] == 2

    def test_positions_open_only_excludes_closed(self) -> None:
        provider = DemoProvider()
        open_positions = provider.positions()
        assert open_positions["pagination"]["total"] == 3
        assert all(p["size"] > 0 for p in open_positions["items"])
        assert provider.positions(open_only=False)["pagination"]["total"] == 4

    def test_orders_filter_by_status(self) -> None:
        provider = DemoProvider()
        assert provider.orders(status="FILLED")["pagination"]["total"] == 2
        assert provider.orders(status="REJECTED")["pagination"]["total"] == 1

    def test_risk_shape_matches_api(self) -> None:
        risk = DemoProvider().risk()
        assert risk["exposure"] == {"total_exposure": 38.6, "open_positions": 3}
        assert "max_total_exposure_pct" in risk["limits"]
        assert risk["events"]["pagination"]["total"] == 4

    def test_performance_totals(self) -> None:
        perf = DemoProvider().performance()
        assert perf["total_pnl"] == 1.529
        assert perf["total_realised_pnl"] == 1.42
        assert perf["total_unrealised_pnl"] == 0.109

    def test_equity_history_is_synthetic_curve(self) -> None:
        points = DemoProvider().equity_history()["points"]
        assert points[0]["equity"] == 10000.0
        assert points[-1]["equity"] == 10124.8

    def test_deterministic_across_instances(self) -> None:
        def without_timestamps(items: list[dict]) -> list[dict]:
            return [
                {k: v for k, v in item.items() if k not in {"timestamp", "created_at"}}
                for item in items
            ]

        assert without_timestamps(DemoProvider().signals()["items"]) == without_timestamps(
            DemoProvider().signals()["items"]
        )
        assert without_timestamps(DemoProvider().positions()["items"]) == without_timestamps(
            DemoProvider().positions()["items"]
        )
