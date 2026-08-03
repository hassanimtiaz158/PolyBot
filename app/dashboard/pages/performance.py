"""Performance page — P&L, activity counts, and equity history."""

from __future__ import annotations

import streamlit as st

from app.dashboard.common import fmt_money, get_dashboard
from app.dashboard.components.banner import render_mode_banner
from app.dashboard.components.cards import kpi_row

st.set_page_config(page_title="Performance", layout="wide", page_icon="📉")

session = get_dashboard()
render_mode_banner(session.mode)

st.title("Performance")

perf = session.fetch("performance") or {}

kpi_row(
    [
        ("Total P&L", fmt_money(perf.get("total_pnl"), signed=True), None),
        ("Realised P&L", fmt_money(perf.get("total_realised_pnl"), signed=True), None),
        ("Unrealised P&L", fmt_money(perf.get("total_unrealised_pnl"), signed=True), None),
        ("Open positions", str(perf.get("open_positions", "—")), None),
        ("Markets tracked", str(perf.get("total_markets", "—")), None),
        ("Signals evaluated", str(perf.get("total_signals", "—")), None),
        ("Orders submitted", str(perf.get("total_orders", "—")), None),
        ("Filled orders", str(perf.get("filled_orders", "—")), None),
    ],
    columns=4,
)

st.divider()
st.subheader("Equity history")

equity = session.fetch("equity_history") or {}
points = equity.get("points", []) if isinstance(equity, dict) else []
if points:
    st.line_chart(points)
else:
    st.info(
        "Equity history is not persisted by the bot. Once an equity tracker is "
        "added, the curve will appear here."
    )
