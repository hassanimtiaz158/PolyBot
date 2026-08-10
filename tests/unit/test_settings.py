"""Unit tests for configuration settings."""


from app.config.settings import Settings


class TestSettings:
    def test_default_mode(self, monkeypatch):
        monkeypatch.delenv("MODE", raising=False)
        # pydantic-settings reads .env; construct without env file to test code default
        s = Settings(_env_file=None)
        assert s.mode == "RESEARCH"

    def test_default_risk_limits(self):
        s = Settings()
        assert s.max_position_pct == 0.01
        assert s.max_total_exposure_pct == 0.05
        assert s.max_consecutive_losses == 5
        assert s.max_open_positions == 10

    def test_default_data_max_age(self):
        s = Settings()
        assert s.data_max_age_seconds == 5

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("MODE", "PAPER")
        s = Settings()
        assert s.mode == "PAPER"

    def test_poly_credentials_default_none(self):
        s = Settings()
        assert s.poly_api_key is None
        assert s.poly_secret is None
