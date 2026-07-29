"""Application settings loaded from environment variables and .env file."""


from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Global application configuration.

    All values can be overridden via environment variables or a .env file.
    No credentials are hard-coded; all API keys and secrets come from the
    environment.
    """

    # ── Operating mode ──────────────────────────────────────────────
    mode: str = "RESEARCH"

    # ── Polymarket API (optional — may be empty for research/demo) ──
    poly_api_key: str | None = None
    poly_secret: str | None = None
    poly_passphrase: str | None = None
    poly_rpc_url: str | None = None

    # ── Database ────────────────────────────────────────────────────
    database_url: str = "sqlite:///data/polymarket.db"

    # ── Logging ─────────────────────────────────────────────────────
    log_level: str = "INFO"
    log_format: str = "structured"

    # ── Risk limits (conservative defaults) ────────────────────────
    max_position_pct: float = 0.01
    max_market_exposure_pct: float = 0.02
    max_total_exposure_pct: float = 0.05
    max_daily_loss_pct: float = 0.02
    max_consecutive_losses: int = 5
    max_open_positions: int = 10
    max_spread: float = 0.03
    min_liquidity: float = 1000.0
    min_net_edge: float = 0.05
    min_confidence: float = 0.70
    data_max_age_seconds: int = 5

    # ── Data collection ─────────────────────────────────────────────
    market_scan_interval_seconds: int = 300

    # ── Monitoring ──────────────────────────────────────────────────
    health_check_interval_seconds: int = 30

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
    }


settings = Settings()
