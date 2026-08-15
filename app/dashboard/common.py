"""Shared helpers for dashboard pages."""

from __future__ import annotations

from typing import Any, cast

import streamlit as st

from app.dashboard.client import ApiClient, ApiError, DemoProvider
from app.dashboard.config import dashboard_settings

DEMO_MODE = "DEMO"
OFFLINE_MODE = "OFFLINE"


class DashboardSession:
    """Bundles the data source, mode, and resilient fetch helpers."""

    def __init__(self, client: object, mode: str, demo: bool, offline: bool) -> None:
        self.client = client
        self.mode = mode
        self.demo = demo
        self.offline = offline

    def fetch(
        self,
        method: str,
        default: dict[str, Any] | None = None,
        **kwargs: object,
    ) -> dict[str, Any] | None:
        """Call a data method, returning ``default`` when unavailable."""
        if self.offline:
            return default
        try:
            result = getattr(self.client, method)(**kwargs)
            return result if isinstance(result, dict) else default
        except (ApiError, AttributeError):
            return default


def build_session() -> DashboardSession:
    """Construct the data session for the current app run."""
    if dashboard_settings.demo:
        return DashboardSession(DemoProvider(), DEMO_MODE, demo=True, offline=False)
    client = ApiClient(
        dashboard_settings.api_url,
        timeout=dashboard_settings.request_timeout_seconds,
    )
    try:
        status = client.status()
        mode = str(status.get("mode", "UNKNOWN"))
    except ApiError:
        return DashboardSession(client, OFFLINE_MODE, demo=False, offline=True)
    return DashboardSession(client, mode, demo=False, offline=False)


def get_dashboard() -> DashboardSession:
    """Return the session stored by the entry point."""
    return cast(DashboardSession, st.session_state["dash_session"])


def fmt_money(value: object, signed: bool = False) -> str:
    """Format a number as currency; unknown values render as an em dash."""
    if value is None or not isinstance(value, (int, float)):
        return "—"
    number = float(value)
    sign = "+" if signed and number > 0 else ""
    return f"{sign}${number:,.2f}"


def fmt_pct(value: object, signed: bool = False) -> str:
    """Format a 0–1 probability or edge as a percentage."""
    if value is None or not isinstance(value, (int, float)):
        return "—"
    number = float(value)
    sign = "+" if signed and number > 0 else ""
    return f"{sign}{number * 100:.1f}%"


def fmt_price(value: object) -> str:
    """Format a 0–1 price with three decimals."""
    if value is None or not isinstance(value, (int, float)):
        return "—"
    return f"{float(value):.3f}"


def is_healthy(check: dict[str, object] | None) -> bool:
    """Return True when a health-check payload reports healthy."""
    if not isinstance(check, dict):
        return False
    return bool(check.get("healthy"))
