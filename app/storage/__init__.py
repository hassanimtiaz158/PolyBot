"""Database connection management, typed models, and repository layer."""

from app.storage.db import Database, DatabaseError, db
from app.storage.models import (
    Market,
    MarketSnapshot,
    Order,
    Position,
    RiskEvent,
    Signal,
)
from app.storage.repositories import (
    MarketRepository,
    OrderRepository,
    PositionRepository,
    RiskEventRepository,
    SignalRepository,
    SnapshotRepository,
    market_repo,
    order_repo,
    position_repo,
    risk_event_repo,
    signal_repo,
    snapshot_repo,
)

__all__ = [
    # Database
    "Database", "DatabaseError", "db",
    # Models
    "Market", "MarketSnapshot", "Signal", "Order", "Position", "RiskEvent",
    # Repositories
    "MarketRepository", "SnapshotRepository", "SignalRepository",
    "OrderRepository", "PositionRepository", "RiskEventRepository",
    "market_repo", "snapshot_repo", "signal_repo",
    "order_repo", "position_repo", "risk_event_repo",
]
