"""Execution page — order history and fills."""

from __future__ import annotations

import streamlit as st

from app.dashboard.common import fmt_money, get_dashboard
from app.dashboard.components.banner import render_mode_banner
from app.dashboard.components.cards import kpi_row

st.set_page_config(page_title="Execution", layout="wide", page_icon="âš¡")

session = get_dashboard()
render_mode_banner(session.mode)

st.title("Execution")

orders = session.fetch("orders") or {}
items = orders.get("items", []) if isinstance(orders, dict) else []
total = (
    orders.get("pagination", {}).get("total", len(items))
    if isinstance(orders, dict)
    else len(items)
)

if session.offline:
    st.error("Bot API unreachable — no order data available.")
elif not items:
    st.info("No orders recorded.")

status_filter = st.selectbox(
    "Status filter",
    ["ALL", "OPEN", "FILLED", "CANCELLED", "REJECTED", "PARTIALLY_FILLED"],
)
filtered = [o for o in items if status_filter == "ALL" or o.get("status") == status_filter]

filled = sum(1 for o in items if o.get("status") == "FILLED")
open_orders = sum(1 for o in items if o.get("status") in {"OPEN", "PARTIALLY_FILLED"})
rejected = sum(1 for o in items if o.get("status") == "REJECTED")
cancelled = sum(1 for o in items if o.get("status") == "CANCELLED")
kpi_row(
    [
        ("Total orders", str(total), None),
        ("Open", str(open_orders), None),
        ("Filled", str(filled), None),
        ("Rejected", str(rejected), None),
        ("Cancelled", str(cancelled), None),
        ("Showing", f"{len(filtered)}", None),
    ],
    columns=3,
)

if filtered:
    table = [
        {
            "Order ID": o.get("order_id", "—"),
            "Market": o.get("market_id", "—"),
            "Side": str(o.get("side", "—")).upper(),
            "Status": str(o.get("status", "—")),
            "Size": f"{float(o.get('size') or 0):g}",
            "Price": f"{float(o.get('price')):.3f}" if o.get("price") is not None else "—",
            "Filled size": f"{float(o.get('filled_size') or 0):g}",
            "Avg fill": (
                f"{float(o.get('average_fill')):.3f}"
                if o.get("average_fill") is not None
                else "—"
            ),
            "P&L (est.)": fmt_money(o.get("realised_pnl"), signed=True),
            "Created": str(o.get("created_at", "—"))[:19],
        }
        for o in filtered
    ]
    st.dataframe(table, hide_index=True, width="stretch")
    st.caption(
        "This page is read-only: no order can be created, cancelled, or modified "
        "from the dashboard."
    )
