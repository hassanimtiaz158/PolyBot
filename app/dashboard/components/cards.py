"""Reusable dashboard widgets."""

from __future__ import annotations

from collections.abc import Sequence

import streamlit as st

from app.dashboard.common import fmt_money, fmt_pct


def kpi_row(
    items: Sequence[tuple[str, str, str | None]],
    columns: int = 4,
) -> None:
    """Render ``(label, value, delta_or_None)`` items as a row of metric cards."""
    cols = st.columns(columns)
    for index, (label, value, delta) in enumerate(items):
        with cols[index % columns]:
            if delta is not None:
                st.metric(label, value, delta=delta)
            else:
                st.metric(label, value)


def money_card(label: str, value: object, delta: str | None = None) -> None:
    st.metric(label, fmt_money(value, signed=True), delta=delta)


def pct_card(label: str, value: object, delta: str | None = None) -> None:
    st.metric(label, fmt_pct(value, signed=True), delta=delta)
