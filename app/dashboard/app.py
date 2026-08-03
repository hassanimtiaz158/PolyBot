"""Streamlit dashboard entry point.

Runs ``streamlit run app/dashboard/app.py``. Pages are loaded via
``st.navigation`` and share one data session (read-only API or demo provider)
stored in ``st.session_state``.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import streamlit as st  # noqa: E402

from app.dashboard.common import build_session  # noqa: E402
from app.dashboard.components.banner import render_sidebar_mode  # noqa: E402

if "dash_session" not in st.session_state:
    st.session_state["dash_session"] = build_session()

session = st.session_state["dash_session"]

render_sidebar_mode(session.mode)
st.sidebar.caption(
    "Read-only dashboard. No orders can be submitted from here."
    if not session.demo
    else "Demo mode: all data is fabricated."
)

pages = [
    st.Page("pages/overview.py", title="Overview", icon="📊", default=True),
    st.Page("pages/signals.py", title="Signals", icon="📡"),
    st.Page("pages/markets.py", title="Markets", icon="📈"),
    st.Page("pages/positions.py", title="Positions", icon="💼"),
    st.Page("pages/risk.py", title="Risk", icon="🛡️"),
    st.Page("pages/performance.py", title="Performance", icon="📉"),
    st.Page("pages/execution.py", title="Execution", icon="⚡"),
    st.Page("pages/audit.py", title="Audit", icon="📜"),
    st.Page("pages/settings.py", title="Settings", icon="⚙️"),
]

nav = st.navigation(pages)
nav.run()
