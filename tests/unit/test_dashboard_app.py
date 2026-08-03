"""Smoke tests for the Streamlit dashboard (demo and offline modes)."""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from app.dashboard import config as dashboard_config

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_PATH = str(REPO_ROOT / "app" / "dashboard" / "app.py")


@pytest.fixture
def demo_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dashboard_config.dashboard_settings, "demo", True)
    monkeypatch.setattr(
        dashboard_config.dashboard_settings,
        "api_url",
        "http://127.0.0.1:1",
    )


@pytest.fixture
def offline_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dashboard_config.dashboard_settings, "demo", False)
    monkeypatch.setattr(
        dashboard_config.dashboard_settings,
        "api_url",
        "http://127.0.0.1:1",
    )


def _run() -> AppTest:
    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.run()
    assert not at.exception, at.exception
    return at


class TestDemoDashboard:
    def test_overview_renders_with_demo_banner(self, demo_settings: None) -> None:
        at = _run()
        assert at.title[0].value == "Overview"
        joined = "\n".join(m.value for m in at.markdown)
        assert "DEMO — SYNTHETIC DATA" in joined

    def test_overview_shows_kpis(self, demo_settings: None) -> None:
        at = _run()
        labels = [m.label for m in at.metric]
        for expected in (
            "Equity (est.)",
            "Today P&L",
            "Total P&L",
            "Max drawdown",
            "Open exposure",
            "Active signals",
            "Eligible markets",
            "Risk utilisation",
        ):
            assert expected in labels

    def test_overview_shows_system_status_table(self, demo_settings: None) -> None:
        at = _run()
        assert len(at.dataframe) >= 1

    @pytest.mark.parametrize(
        ("page_path", "title"),
        [
            ("pages/signals.py", "Signals"),
            ("pages/markets.py", "Markets"),
            ("pages/positions.py", "Positions"),
            ("pages/risk.py", "Risk"),
            ("pages/performance.py", "Performance"),
            ("pages/execution.py", "Execution"),
            ("pages/audit.py", "Audit log"),
            ("pages/settings.py", "Settings"),
        ],
    )
    def test_each_page_renders(self, demo_settings: None, page_path: str, title: str) -> None:
        at = _run()
        at.switch_page(page_path)
        at.run()
        assert not at.exception, at.exception
        assert at.title[0].value == title
        joined = "\n".join(m.value for m in at.markdown)
        assert "DEMO — SYNTHETIC DATA" in joined

    def test_signals_page_lists_demo_signals(self, demo_settings: None) -> None:
        at = _run()
        at.switch_page("pages/signals.py")
        at.run()
        assert not at.exception
        assert len(at.dataframe) >= 1

    def test_execution_page_is_read_only(self, demo_settings: None) -> None:
        at = _run()
        at.switch_page("pages/execution.py")
        at.run()
        assert not at.exception
        assert not at.button
        caption = " ".join(c.value for c in at.caption)
        assert "read-only" in caption.lower()


class TestOfflineDashboard:
    def test_offline_banner_when_api_unreachable(self, offline_settings: None) -> None:
        at = _run()
        joined = "\n".join(m.value for m in at.markdown)
        assert "BOT OFFLINE" in joined
        assert any(m.value == "Down" for m in at.metric)

    def test_data_pages_show_unreachable_error(self, offline_settings: None) -> None:
        at = _run()
        at.switch_page("pages/signals.py")
        at.run()
        assert not at.exception
        errors = " ".join(e.value for e in at.error)
        assert "unreachable" in errors.lower()
