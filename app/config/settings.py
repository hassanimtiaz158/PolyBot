"""Application settings loaded from environment variables and .env file."""


from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Global application configuration.

    All values can be overridden via environment variables or a .env file.
    No credentials are hard-coded; all API keys and secrets come from the
    environment.
    """

    # ── Operating mode ──────────────────────────────────────────────
    mode: str = "RESEARCH"

    # ── Portfolio / account ─────────────────────────────────────────
    # Starting equity used to derive account balance from persisted P&L.
    initial_equity: float = Field(default=10_000.0, ge=0.0)

    # ── Live trading gate (default OFF) ─────────────────────────────
    live_trading_enabled: bool = False

    # ── Polymarket API (optional — may be empty for research/demo) ──
    poly_api_key: str | None = None
    poly_secret: str | None = None
    poly_passphrase: str | None = None

    # ── Polymarket wallet (required only for LIVE_GUARDED order signing) ──
    # The private key that signs order EIP-712 payloads. Never logged or
    # persisted; loaded from the environment only. Leave unset outside of
    # live trading — PolymarketExecution refuses to submit orders without it.
    poly_private_key: str | None = None
    # Funder/proxy wallet address holding trading funds. Required when
    # poly_signature_type != 0 (email/Magic or browser-proxy wallets).
    poly_funder_address: str | None = None
    # EOA=0, POLY_PROXY (email/Magic wallet)=1, POLY_GNOSIS_SAFE (browser proxy)=2
    poly_signature_type: int = Field(default=0, ge=0, le=2)

    # ── Emergency control interface ─────────────────────────────────
    # Dedicated secret for the writable /api/control/* endpoints
    # (kill switch + resume).  Required even when POLY_API_KEY is unset.
    # When this is empty, the control interface returns 503 (disabled)
    # — it is never left open.
    poly_control_key: str | None = None

    # ── Database ────────────────────────────────────────────────────
    database_url: str = "sqlite:///data/polymarket.db"

    # ── Logging ─────────────────────────────────────────────────────
    log_level: str = "INFO"
    log_format: str = "structured"

    # ── Risk limits (conservative defaults — all non-negative) ─────
    max_position_pct: float = Field(default=0.01, ge=0.0, le=1.0)
    max_market_exposure_pct: float = Field(default=0.02, ge=0.0, le=1.0)
    max_total_exposure_pct: float = Field(default=0.05, ge=0.0, le=1.0)
    max_daily_loss_pct: float = Field(default=0.02, ge=0.0, le=1.0)
    max_consecutive_losses: int = Field(default=5, ge=0)
    max_open_positions: int = Field(default=10, ge=0)
    max_spread: float = Field(default=0.03, ge=0.0, le=1.0)
    min_liquidity: float = Field(default=1000.0, ge=0.0)
    min_net_edge: float = Field(default=0.05, ge=0.0)
    min_confidence: float = Field(default=0.70, ge=0.0, le=1.0)
    data_max_age_seconds: int = Field(default=5, ge=1)

    # ── Portfolio-level limits (concentration) ──────────────────────
    max_event_exposure_pct: float = Field(default=0.03, ge=0.0, le=1.0)
    max_strategy_exposure_pct: float = Field(default=0.04, ge=0.0, le=1.0)
    max_directional_exposure_pct: float = Field(default=0.03, ge=0.0, le=1.0)
    max_resolution_exposure_pct: float = Field(default=0.05, ge=0.0, le=1.0)

    # ── Data collection ─────────────────────────────────────────────
    market_scan_interval_seconds: int = Field(default=300, ge=1)

    # ── Walk-forward validation ───────────────────────────────────
    walk_forward_enabled: bool = True
    walk_forward_interval_seconds: int = Field(default=86_400, ge=60)
    walk_forward_train_size: int = Field(default=1500, ge=1)
    walk_forward_val_size: int = Field(default=500, ge=1)
    walk_forward_windows: int = Field(default=10, ge=1)
    walk_forward_fallback_synthetic: bool = True

    # ── Monitoring ──────────────────────────────────────────────────
    health_check_interval_seconds: int = Field(default=30, ge=1)

    # ── Real-time dashboard ─────────────────────────────────────────
    # How often the server-side change detector probes the database for
    # dashboard updates (WebSocket push).  Lower values mean fresher
    # events; the frontend itself never polls aggressively.
    dashboard_poll_interval_seconds: float = Field(default=1.0, ge=0.1)

    # ── Alerting (optional adapter) ─────────────────────────────────
    alert_enabled: bool = False
    alert_webhook_url: str | None = None
    alert_min_interval_seconds: float = Field(default=60.0, ge=0.0)
    alert_repeat_threshold: int = Field(default=5, ge=1)
    alert_repeat_window_seconds: float = Field(default=300.0, ge=1.0)

    # ── API Security ─────────────────────────────────────────────────
    cors_allow_origins: str = ""

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
    }


settings = Settings()
