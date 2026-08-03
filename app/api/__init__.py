"""Read-only FastAPI backend."""

from app.api.app import API_VERSION, create_app

__all__ = ["API_VERSION", "create_app"]
