"""Signals page — model signals with edges and decisions."""

from __future__ import annotations

import streamlit as st

from app.dashboard.common import fmt_pct, get_dashboard
from app.dashboard.components.banner import render_mode_banner
from app.dashboard.components.cards import kpi_row

st.set_page_config(page_title="Signals", layout="wide", page_icon="ðŸ“¡")

session = get_dashboard()
render_mode_banner(session.mode)

st.title("Signals")

markets = session.fetch("markets") or {}
liquidity = {
    m.get("market_id"): m.get("liquidity")
    for m in (markets.get("items", []) if isinstance(markets, dict) else [])
}

signals = session.fetch("signals") or {}
items = signals.get("items", []) if isinstance(signals, dict) else []

if session.offline:
    st.error("Bot API unreachable — no signal data available.")
elif not items:
    st.info("No signals recorded.")

decisions = {}
for s in items:
    decisions[str(s.get("decision", "PENDING"))] = (
        decisions.get(str(s.get("decision", "PENDING")), 0) + 1
    )
kpi_row(
    [
        ("Total signals", str(len(items)), None),
        ("Buy signals", str(decisions.get("BUY", 0)), None),
        ("Sell signals", str(decisions.get("SELL", 0)), None),
        ("Pass signals", str(decisions.get("PASS", 0)), None),
    ],
    columns=4,
)

if items:
    table = []
    for s in items:
        mid = s.get("market_id", "—")
        spread = "—"
        liq = liquidity.get(mid)
        table.append(
            {
                "Market": mid,
                "Side": str(s.get("side", "—")).upper(),
                "Price": fmt_pct(s.get("implied_probability") or s.get("model_probability")),
                "Model Probability": fmt_pct(s.get("model_probability")),
                "Implied Probability": fmt_pct(s.get("implied_probability")),
                "Gross Edge": fmt_pct(s.get("gross_edge"), signed=True),
                "Net Edge": fmt_pct(s.get("net_edge"), signed=True),
                "Confidence": fmt_pct(s.get("confidence")),
                "Liquidity": (
                    f"${float(liq):,.0f}" if liq is not None and not isinstance(liq, str) else "—"
                ),
                "Spread": spread,
                "Decision": str(s.get("decision", "PENDING")),
            }
        )
    st.dataframe(
        table,
        hide_index=True,
        width="stretch",
        column_config={
            "Price": st.column_config.NumberColumn(format="%.3f"),
        },
    )
    st.caption(
        "Spread is not persisted by the bot; it is available only in market data."
    )
