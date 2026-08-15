"""Mode banner — the always-visible trading-mode indicator."""

from __future__ import annotations

import html

import streamlit as st

from app.dashboard.common import OFFLINE_MODE

_CSS_KEY = "_dash_banner_css_rendered"

# accent, badge text, explanatory message
_BANNERS: dict[str, tuple[str, str, str]] = {
    "DEMO": (
        "#9ca3af",
        "DEMO — SYNTHETIC DATA",
        "Every number on this dashboard is fabricated sample data. The bot is not "
        "connected and no real orders can exist.",
    ),
    "PAPER": (
        "#2563eb",
        "PAPER TRADING",
        "Simulated fills against live data. No real money and no real Polymarket "
        "orders are involved.",
    ),
    "LIVE_GUARDED": (
        "#dc2626",
        "LIVE GUARDED — REAL ORDERS",
        "The bot submits REAL orders under reduced position limits. Treat every "
        "number here as real. Trading can be stopped at any time by halting the bot.",
    ),
    "HALTED": (
        "#7f1d1d",
        "HALTED",
        "No new orders. Monitoring only. The bot cannot trade until an operator "
        "clears the halt.",
    ),
    "RESEARCH": (
        "#059669",
        "RESEARCH",
        "No trading. Data collection and analysis only.",
    ),
    "BACKTEST": (
        "#059669",
        "BACKTEST",
        "Historical replay of signals. No live orders.",
    ),
    OFFLINE_MODE: (
        "#475569",
        "BOT OFFLINE",
        "Cannot reach the bot API. No live data is being shown — only cached or "
        "empty states.",
    ),
    "UNKNOWN": (
        "#475569",
        "STATUS UNKNOWN",
        "The bot did not report a valid mode.",
    ),
}

_CSS = """
<style>
.dash-banner {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    border: 1px solid var(--accent);
    background: color-mix(in srgb, var(--accent) 12%, transparent);
    border-radius: 0.5rem;
    padding: 0.5rem 0.9rem;
    margin-bottom: 1rem;
}
.dash-badge {
    flex: 0 0 auto;
    font-weight: 700;
    letter-spacing: 0.08em;
    font-size: 0.78rem;
    color: var(--accent);
    border: 1px solid var(--accent);
    border-radius: 0.35rem;
    padding: 0.15rem 0.55rem;
    text-transform: uppercase;
    white-space: nowrap;
}
.dash-banner.alert .dash-badge {
    background: var(--accent);
    color: #fff;
}
.dash-msg {
    color: var(--accent);
    font-size: 0.88rem;
}
</style>
"""


def _inject_css() -> None:
    if st.session_state.get(_CSS_KEY):
        return
    st.markdown(_CSS, unsafe_allow_html=True)
    st.session_state[_CSS_KEY] = True


def render_mode_banner(mode: str, alert: bool = False) -> None:
    """Render the full-width mode banner at the top of a page."""
    _inject_css()
    accent, badge, message = _BANNERS.get(mode, _BANNERS["UNKNOWN"])
    if not alert and mode in {"LIVE_GUARDED", OFFLINE_MODE}:
        alert = True
    cls = "dash-banner alert" if alert else "dash-banner"
    st.markdown(
        f'<div class="{cls}" style="--accent:{accent}">'
        f'<span class="dash-badge">{badge}</span>'
        f'<span class="dash-msg">{message}</span></div>',
        unsafe_allow_html=True,
    )


def render_sidebar_mode(mode: str) -> None:
    """Render a compact mode indicator in the sidebar."""
    _inject_css()
    accent, badge, _ = _BANNERS.get(mode, _BANNERS["UNKNOWN"])
    st.markdown(
        f'<div class="dash-banner" style="--accent:{accent};margin-bottom:0.4rem">'
        f'<span class="dash-badge">{badge}</span></div>',
        unsafe_allow_html=True,
    )


def render_kill_switch(status: object) -> None:
    """Render the emergency-stop banner when the kill switch is engaged.

    ``status`` is the ``/system/status`` payload.  When the kill switch
    is KILLED a prominent alert banner is shown with the canonical
    reason.  The dashboard only *displays* this state — it can never
    change it (control commands require the dedicated control key).
    """
    if not isinstance(status, dict):
        return
    ks = status.get("kill_switch")
    if not isinstance(ks, dict):
        return
    if str(ks.get("state")).upper() != "KILLED":
        return
    raw_reason = str(ks.get("reason") or "MANUAL EMERGENCY STOP")
    # Sanitize to prevent XSS — the reason comes from the kill switch
    # state which is set by an operator with the control key.
    reason = html.escape(raw_reason)
    _inject_css()
    st.markdown(
        '<div class="dash-banner alert" style="--accent:#7f1d1d">'
        '<span class="dash-badge">TRADING HALTED</span>'
        f'<span class="dash-msg"><strong>Reason:</strong> {reason}</span>'
        "</div>",
        unsafe_allow_html=True,
    )
