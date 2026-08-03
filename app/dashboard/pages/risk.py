"""Risk page — exposure, limits, health, and circuit breaker."""

from __future__ import annotations

import streamlit as st

from app.dashboard.common import fmt_money, get_dashboard
from app.dashboard.components.banner import render_mode_banner
from app.dashboard.components.cards import kpi_row

st.set_page_config(page_title="Risk", layout="wide", page_icon="ðŸ›¡ï¸")

session = get_dashboard()
render_mode_banner(session.mode)

st.title("Risk")

risk = session.fetch("risk") or {}
exposure_summary = risk.get("exposure", {}) if isinstance(risk, dict) else {}
exposure = exposure_summary.get("total_exposure")
limits = risk.get("limits", {}) if isinstance(risk, dict) else {}
status = session.fetch("status") or {}
breaker = status.get("circuit_breaker") if isinstance(status, dict) else None
health = session.fetch("health") or {}
checks = health.get("checks", {}) if isinstance(health, dict) else {}

kpi_row(
    [
        ("Daily loss", "—", None),
        ("Total exposure", fmt_money(exposure, signed=True), None),
        ("Market exposure (max)", "—", None),
        ("Consecutive losses", "—", None),
        (
            "Data freshness",
            "Fresh" if (checks.get("data_freshness") or {}).get("healthy") else "Stale",
            None,
        ),
        ("API health", "Up" if (checks.get("api") or {}).get("healthy") else "Down", None),
        (
            "Circuit breaker",
            str(breaker.get("state", "NOT_TRIPPED")) if breaker else "NOT_TRIPPED",
            None,
        ),
        ("Open positions", str(exposure_summary.get("open_positions", "—")), None),
    ],
    columns=4,
)

breaker_state = str(breaker.get("state", "NOT_TRIPPED")) if breaker else "NOT_TRIPPED"
breaker_reasons = ", ".join(breaker.get("reasons", []) or ["—"]) if breaker else "—"
breaker_triggered = str(breaker.get("triggered_at", "—")) if breaker else "—"

st.divider()
st.subheader("Circuit breaker")

if breaker_state != "NOT_TRIPPED":
    st.error("Circuit breaker is tripped — no new orders will be submitted.")
else:
    st.success("Circuit breaker not tripped.")
st.dataframe(
    [
        {
            "State": breaker_state,
            "Reasons": breaker_reasons,
            "Triggered at": breaker_triggered,
        }
    ],
    hide_index=True,
    width="stretch",
)

st.divider()
st.subheader("Configured limits")

if limits:
    st.dataframe(
        [
            {"Limit": name, "Value": f"{float(value):g}"}
            for name, value in limits.items()
        ],
        hide_index=True,
        width="stretch",
    )
else:
    st.info("No risk limits reported.")

st.divider()
st.subheader("Recent risk events")

events = risk.get("events", {}) if isinstance(risk, dict) else {}
event_items = events.get("items", []) if isinstance(events, dict) else []
if event_items:
    st.dataframe(
        [
            {
                "Time": str(e.get("created_at") or e.get("timestamp", "—"))[:19],
                "Event type": str(e.get("event_type", "—")),
                "Severity": str(e.get("severity", "—")),
                "Details": str(e.get("details", ""))[:120],
            }
            for e in event_items
        ],
        hide_index=True,
        width="stretch",
    )
else:
    st.info("No risk events recorded.")
