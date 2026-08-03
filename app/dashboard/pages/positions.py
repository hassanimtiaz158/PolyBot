"""Positions page — open positions with risk and resolution context."""

from __future__ import annotations

import streamlit as st

from app.dashboard.common import fmt_money, get_dashboard
from app.dashboard.components.banner import render_mode_banner
from app.dashboard.components.cards import kpi_row

st.set_page_config(page_title="Positions", layout="wide", page_icon="ðŸ’¼")

session = get_dashboard()
render_mode_banner(session.mode)

st.title("Positions")

markets = session.fetch("markets") or {}
resolution = {
    m.get("market_id"): m.get("resolution_time")
    for m in (markets.get("items", []) if isinstance(markets, dict) else [])
}

positions = session.fetch("positions") or {}
items = positions.get("items", []) if isinstance(positions, dict) else []

if session.offline:
    st.error("Bot API unreachable — no position data available.")
elif not items:
    st.info("No open positions.")

unrealised = sum(
    float(p.get("unrealised_pnl") or 0.0) for p in items
)
notional = sum(
    float(p.get("size") or 0.0) * float(p.get("current_price") or 0.0) for p in items
)
kpi_row(
    [
        ("Open positions", str(len(items)), None),
        ("Unrealised P&L", fmt_money(unrealised, signed=True), None),
        ("Notional exposure", fmt_money(notional, signed=True), None),
    ],
    columns=3,
)

if items:
    table = []
    for p in items:
        mid = p.get("market_id", "—")
        size = float(p.get("size") or 0.0)
        current = float(p.get("current_price") or 0.0)
        risk = size * current if p.get("current_price") is not None else None
        table.append(
            {
                "Market": mid,
                "Side": str(p.get("side", "—")).upper(),
                "Entry": (
                    f"{float(p.get('average_entry')):.3f}"
                    if p.get("average_entry") is not None
                    else "—"
                ),
                "Current": f"{current:.3f}" if p.get("current_price") is not None else "—",
                "Size": f"{size:g}",
                "Unrealised P&L": fmt_money(p.get("unrealised_pnl"), signed=True),
                "Risk (notional)": fmt_money(risk, signed=True),
                "Resolution time": str(resolution.get(mid, "—")),
            }
        )
    st.dataframe(table, hide_index=True, width="stretch")
    st.caption(
        "Risk per position is estimated as size × current price. Position P&L is "
        "reported by the bot on a 0–1 price basis."
    )
