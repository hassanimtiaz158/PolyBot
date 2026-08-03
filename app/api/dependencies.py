"""Shared FastAPI dependencies."""

from fastapi import Request

from app.storage.db import Database


def get_db(request: Request) -> Database:
    """Return the database instance bound to the application."""
    return request.app.state.db
