"""Overview page — system health, headline KPIs, and recent activity."""

from __future__ import annotations

import streamlit as st

from app.dashboard.common import (
    fmt_money,
    get_dashboard,
    is_healthy,
)
from app.dashboard.components.banner import render_mode_banner
from app.dashboard.components.cards import kpi_row
from app.dashboard.config import dashboard_settings

st.set_page_config(page_title="Overview", layout="wide", page_icon="ðŸ“Š")

session = get_dashboard()
render_mode_banner(session.mode)

st.title("Overview")

health = session.fetch("health") or {}
checks = health.get("checks", {}) if isinstance(health, dict) else {}
kpi_row(
    [
        ("API", "Up" if is_healthy(checks.get("api")) else "Down", None),
        ("Database", "Up" if is_healthy(checks.get("database")) else "Down", None),
        ("Data freshness", "Fresh" if is_healthy(checks.get("data_freshness")) else "Stale", None),
        (
            "Model",
            "Available" if is_healthy(checks.get("model_availability")) else "Unavailable",
            None,
        ),
    ],
    columns=4,
)

perf = session.fetch("performance") or {}
equity = dashboard_settings.equity_base + float(perf.get("total_pnl") or 0.0)
demo_extra = session.demo
today_pnl = "+$12.40" if demo_extra else "—"
max_drawdown = "1.2%" if demo_extra else "—"
kpi_row(
    [
        ("Equity (est.)", fmt_money(equity), None),
        ("Today P&L", today_pnl, None),
        ("Total P&L", fmt_money(perf.get("total_pnl"), signed=True), None),
        ("Max drawdown", max_drawdown, None),
    ],
    columns=4,
)

risk = session.fetch("risk") or {}
limits = risk.get("limits", {}) if isinstance(risk, dict) else {}
risk_limit = limits.get("max_total_exposure_pct")
exposure = risk.get("exposure", {}).get("total_exposure") if isinstance(risk, dict) else None
risk_utilisation = (
    f"{exposure / float(risk_limit) * 100:.0f}%" if risk_limit and exposure is not None else "—"
)
signals = session.fetch("signals") or {}
positions = session.fetch("positions") or {}
markets = session.fetch("markets") or {}
active_signals = signals.get("items", []) if isinstance(signals, dict) else []
open_positions = positions.get("items", []) if isinstance(positions, dict) else []
eligible_markets = (
    sum(1 for m in markets.get("items", []) if m.get("eligible"))
    if isinstance(markets, dict)
    else 0
)
kpi_row(
    [
        ("Open exposure", fmt_money(exposure, signed=True), None),
        ("Active signals", str(len(active_signals)), None),
        ("Eligible markets", str(eligible_markets), None),
        ("Risk utilisation", risk_utilisation, None),
    ],
    columns=4,
)

st.divider()
st.subheader("System status")

status = session.fetch("status") or {}
breaker = status.get("circuit_breaker") if isinstance(status, dict) else None
st.dataframe(
    [
        {"Item": "Mode", "Value": str(status.get("mode", "—"))},
        {"Item": "Trading enabled", "Value": "Yes" if status.get("trading_enabled") else "No"},
        {
            "Item": "Database connected",
            "Value": "Yes" if status.get("database_connected") else "No",
        },
        {"Item": "Schema version", "Value": str(status.get("schema_version", "—"))},
        {
            "Item": "Circuit breaker",
            "Value": str(breaker.get("state", "—")) if breaker else "Not tripped",
        },
        {"Item": "API version", "Value": str(status.get("version", "—"))},
        {"Item": "Uptime", "Value": f"{float(status.get('uptime_seconds') or 0):,.0f}s"},
        {"Item": "API URL", "Value": dashboard_settings.api_url},
    ],
    hide_index=True,
    width="stretch",
)

st.divider()
st.subheader("Recent audit events")

audit = session.fetch("audit") or {}
audit_items = audit.get("items", []) if isinstance(audit, dict) else []
if audit_items:
    table = [
        {
            "Time": str(item.get("created_at", "—"))[:19],
            "Event": str(item.get("event_type", "—")),
            "Severity": str(item.get("severity", "—")),
            "Details": str(item.get("details", ""))[:120],
        }
        for item in audit_items[:8]
    ]
    st.dataframe(table, hide_index=True, width="stretch")
else:
    st.info("No audit events recorded.")

st.divider()
st.subheader("Active signals")

if active_signals:
    table = [
        {
            "Market": s.get("market_id", "—"),
            "Side": str(s.get("side", "—")).upper(),
            "Price": (
                f"{float(s.get('implied_probability') or s.get('model_probability') or 0):.3f}"
            ),
            "Decision": str(s.get("decision", "—")),
        }
        for s in active_signals[:5]
    ]
    st.dataframe(table, hide_index=True, width="stretch")
else:
    st.info("No active signals.")
