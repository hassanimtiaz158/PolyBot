"""Markets page — tracked markets and eligibility."""

from __future__ import annotations

import streamlit as st

from app.dashboard.common import fmt_money, get_dashboard
from app.dashboard.components.banner import render_mode_banner
from app.dashboard.components.cards import kpi_row

st.set_page_config(page_title="Markets", layout="wide", page_icon="ðŸ“ˆ")

session = get_dashboard()
render_mode_banner(session.mode)

st.title("Markets")

markets = session.fetch("markets") or {}
items = markets.get("items", []) if isinstance(markets, dict) else []

if session.offline:
    st.error("Bot API unreachable — no market data available.")
elif not items:
    st.info("No markets tracked.")

eligible = sum(1 for m in items if m.get("eligible"))
active = sum(1 for m in items if m.get("active"))
total_liquidity = sum(
    float(m.get("liquidity") or 0.0) for m in items if m.get("liquidity") is not None
)
kpi_row(
    [
        ("Tracked markets", str(len(items)), None),
        ("Active", str(active), None),
        ("Eligible", str(eligible), None),
        ("Total liquidity", fmt_money(total_liquidity), None),
    ],
    columns=4,
)

if items:
    table = [
        {
            "Market ID": m.get("market_id", "—"),
            "Question": str(m.get("question", "—"))[:80],
            "Status": str(m.get("status", "—")),
            "Yes price": (
                f"{float(m.get('yes_price')):.3f}" if m.get("yes_price") is not None else "—"
            ),
            "No price": (
                f"{float(m.get('no_price')):.3f}" if m.get("no_price") is not None else "—"
            ),
            "Liquidity": (
                f"${float(m.get('liquidity')):,.0f}"
                if m.get("liquidity") is not None and not isinstance(m.get("liquidity"), str)
                else "—"
            ),
            "Eligible": "Yes" if m.get("eligible") else "No",
            "Updated": str(m.get("updated_at", "—"))[:19],
        }
        for m in items
    ]
    st.dataframe(table, hide_index=True, width="stretch")
