"""Dashboard configuration loaded from environment variables.

The dashboard is a separate process from the bot.  It talks to the
read-only FastAPI backend, never to the database directly.
"""

from pydantic_settings import BaseSettings


class DashboardSettings(BaseSettings):
    """Dashboard connection and display settings.

    ``demo`` switches the dashboard to synthetic data.  When enabled
    every page is labelled DEMO and no real data is fetched.
    """

    api_url: str = "http://localhost:8000"
    demo: bool = False
    equity_base: float = 10_000.0
    request_timeout_seconds: float = 10.0

    model_config = {
        "env_prefix": "DASHBOARD_",
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
    }


dashboard_settings = DashboardSettings()
