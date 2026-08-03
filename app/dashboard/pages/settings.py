"""Settings page — read-only view of bot configuration."""

from __future__ import annotations

import streamlit as st

from app.dashboard.common import get_dashboard
from app.dashboard.components.banner import render_mode_banner
from app.dashboard.config import dashboard_settings

st.set_page_config(page_title="Settings", layout="wide", page_icon="âš™ï¸")

session = get_dashboard()
render_mode_banner(session.mode)

st.title("Settings")

st.warning(
    "This dashboard is strictly read-only. No settings can be changed and no "
    "orders can be submitted from here. Mode changes and halts are performed by "
    "the operator on the bot host."
)

st.subheader("Dashboard connection")

st.dataframe(
    [
        {"Setting": "Bot API URL", "Value": dashboard_settings.api_url},
        {
            "Setting": "Demo mode",
            "Value": "Yes (synthetic data)" if dashboard_settings.demo else "No",
        },
        {
            "Setting": "Data source",
            "Value": "DemoProvider (fabricated)" if dashboard_settings.demo else "Read-only API",
        },
        {
            "Setting": "Request timeout (s)",
            "Value": str(dashboard_settings.request_timeout_seconds),
        },
    ],
    hide_index=True,
    width="stretch",
)

st.subheader("Bot status")

status = session.fetch("status") or {}
breaker = status.get("circuit_breaker") if isinstance(status, dict) else None
st.dataframe(
    [
        {"Item": "Mode", "Value": str(status.get("mode", "—"))},
        {"Item": "Trading enabled", "Value": "Yes" if status.get("trading_enabled") else "No"},
        {"Item": "Live trading enabled", "Value": "Yes" if status.get("live_enabled") else "No"},
        {
            "Item": "Database connected",
            "Value": "Yes" if status.get("database_connected") else "No",
        },
        {"Item": "Schema version", "Value": str(status.get("schema_version", "—"))},
        {"Item": "Bot version", "Value": str(status.get("version", "—"))},
        {
            "Item": "Circuit breaker",
            "Value": str(breaker.get("state", "—")) if breaker else "Not tripped",
        },
        {"Item": "Started at", "Value": str(status.get("started_at", "—"))[:19]},
    ],
    hide_index=True,
    width="stretch",
)

st.subheader("Risk limits")

risk = session.fetch("risk") or {}
limits = risk.get("limits", {}) if isinstance(risk, dict) else {}
if limits:
    st.dataframe(
        [{"Limit": name, "Value": f"{float(value):g}"} for name, value in limits.items()],
        hide_index=True,
        width="stretch",
    )
else:
    st.info("No risk limits reported.")
