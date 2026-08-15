"""Shared FastAPI dependencies."""

from typing import cast

from fastapi import Request

from app.storage.db import Database


def get_db(request: Request) -> Database:
    """Return the database instance bound to the application."""
    return cast(Database, request.app.state.db)
