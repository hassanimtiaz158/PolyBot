"""Audit page — event log with type and severity filters."""

from __future__ import annotations

import streamlit as st

from app.dashboard.common import get_dashboard
from app.dashboard.components.banner import render_mode_banner

st.set_page_config(page_title="Audit", layout="wide", page_icon="ðŸ“œ")

session = get_dashboard()
render_mode_banner(session.mode)

st.title("Audit log")

event_type = st.text_input("Event type filter", value="", placeholder="e.g. RISK_APPROVED")
severity = st.selectbox("Severity filter", ["ALL", "INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"])

audit = session.fetch(
    "audit",
    event_type=event_type or None,
    severity=None if severity == "ALL" else severity,
) or {}
items = audit.get("items", []) if isinstance(audit, dict) else []
total = (
    audit.get("pagination", {}).get("total", len(items))
    if isinstance(audit, dict)
    else len(items)
)

if session.offline:
    st.error("Bot API unreachable — no audit data available.")
elif not items:
    st.info("No audit events match the filters.")

st.caption(f"{total} events in view.")

if items:
    st.dataframe(
        [
            {
                "Time": str(e.get("created_at") or e.get("timestamp", "—"))[:19],
                "Event ID": str(e.get("event_id", "—")),
                "Event type": str(e.get("event_type", "—")),
                "Severity": str(e.get("severity", "—")),
                "Details": str(e.get("details", "")),
            }
            for e in items
        ],
        hide_index=True,
        width="stretch",
    )
