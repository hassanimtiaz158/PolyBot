"""Async SQLite database — connection, schema, migrations, transactions."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from app.config.settings import settings

logger = logging.getLogger(__name__)

# ── Schema version tracking ────────────────────────────────────────
SCHEMA_VERSION = 2

SCHEMA_SQL = """
-- Version 1: core tables
CREATE TABLE IF NOT EXISTS markets (
    market_id TEXT PRIMARY KEY,
    question TEXT NOT NULL,
    condition_id TEXT,
    status TEXT,
    resolution_time TEXT,
    liquidity REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    bid REAL,
    ask REAL,
    midpoint REAL,
    spread REAL,
    bid_depth REAL,
    ask_depth REAL,
    volume REAL,
    time_to_resolution REAL,
    FOREIGN KEY (market_id) REFERENCES markets(market_id)
);

CREATE TABLE IF NOT EXISTS signals (
    signal_id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL,
    strategy TEXT NOT NULL,
    side TEXT NOT NULL,
    model_probability REAL,
    implied_probability REAL,
    gross_edge REAL,
    estimated_cost REAL,
    net_edge REAL,
    confidence REAL,
    decision TEXT NOT NULL,
    rejection_reason TEXT,
    timestamp TEXT NOT NULL,
    FOREIGN KEY (market_id) REFERENCES markets(market_id)
);

CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL,
    side TEXT NOT NULL,
    requested_price REAL,
    requested_size REAL,
    status TEXT NOT NULL,
    filled_size REAL,
    average_fill REAL,
    submitted_at TEXT,
    completed_at TEXT,
    FOREIGN KEY (market_id) REFERENCES markets(market_id)
);

CREATE TABLE IF NOT EXISTS positions (
    position_id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL,
    side TEXT NOT NULL,
    size REAL,
    average_entry REAL,
    current_price REAL,
    realised_pnl REAL,
    unrealised_pnl REAL,
    FOREIGN KEY (market_id) REFERENCES markets(market_id)
);

CREATE TABLE IF NOT EXISTS risk_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    details TEXT,
    timestamp TEXT NOT NULL,
    market_id TEXT,
    strategy TEXT,
    decision TEXT,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    market_id TEXT,
    strategy TEXT,
    decision TEXT,
    reason TEXT,
    details TEXT,
    source TEXT
);

CREATE TABLE IF NOT EXISTS circuit_breaker_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_snapshots_market_ts
    ON market_snapshots(market_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_signals_market_ts
    ON signals(market_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_orders_market_status
    ON orders(market_id, status);
CREATE INDEX IF NOT EXISTS idx_risk_events_ts
    ON risk_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_positions_open
    ON positions(market_id) WHERE size > 0;
"""

MIGRATIONS: dict[int, str] = {
    # Version 2: add condition_id to markets (already handled by v1 schema above)
    2: "ALTER TABLE markets ADD COLUMN condition_id TEXT;",
    # Version 3: add audit fields and index to risk_events (and audit_events new table)
    3: """
        ALTER TABLE risk_events ADD COLUMN market_id TEXT;
        ALTER TABLE risk_events ADD COLUMN strategy TEXT;
        ALTER TABLE risk_events ADD COLUMN decision TEXT;
        ALTER TABLE risk_events ADD COLUMN reason TEXT;
        CREATE INDEX IF NOT EXISTS idx_risk_events_type_ts
            ON risk_events(event_type, timestamp);
        CREATE INDEX IF NOT EXISTS idx_risk_events_event_type
            ON risk_events(event_type);
    """,
}


class DatabaseError(Exception):
    """Base exception for database operations."""


class DatabaseConnectionError(DatabaseError):
    """Raised when the database cannot be opened or connected."""


class DatabaseTransactionError(DatabaseError):
    """Raised when a transaction fails."""


class Database:
    """Async SQLite database wrapper with migration and transaction support.

    Usage::

        db = Database("path/to/db.sqlite")
        await db.connect()
        await db.init_schema()
        async with db.transaction():
            await db.conn.execute("INSERT INTO ...")
        await db.close()
    """

    def __init__(self, db_path: str | None = None) -> None:
        resolved = db_path or settings.database_url.replace("sqlite:///", "")
        self.db_path: str = resolved
        self._conn: aiosqlite.Connection | None = None

    # ── Connection lifecycle ────────────────────────────────────────

    async def connect(self) -> None:
        """Open (or create) the SQLite database file."""
        try:
            parent = Path(self.db_path).parent
            if self.db_path != ":memory:" and not parent.exists():
                parent.mkdir(parents=True, exist_ok=True)
            self._conn = await aiosqlite.connect(self.db_path)
            self._conn.row_factory = aiosqlite.Row
            await self._conn.execute("PRAGMA journal_mode=WAL")
            await self._conn.execute("PRAGMA foreign_keys=ON")
            await self._conn.execute("PRAGMA busy_timeout=5000")
        except aiosqlite.Error as exc:
            raise DatabaseConnectionError(
                f"Failed to connect to {self.db_path}: {exc}"
            ) from exc

    async def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            try:
                await self._conn.close()
            except aiosqlite.Error as exc:
                logger.warning("Error closing database: %s", exc)
            finally:
                self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        """Return the active connection or raise if not connected."""
        if self._conn is None:
            raise DatabaseConnectionError(
                "Not connected — call connect() first"
            )
        return self._conn

    @property
    def is_connected(self) -> bool:
        """Check whether the database connection is open."""
        return self._conn is not None

    # ── Schema & migrations ─────────────────────────────────────────

    async def init_schema(self) -> None:
        """Create all tables, indexes, and apply pending migrations."""
        if self._conn is None:
            await self.connect()
        conn = self.conn
        async with self.transaction():
            # Create schema version tracker
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS _schema_version ("
                "  version INTEGER PRIMARY KEY,"
                "  applied_at TEXT NOT NULL"
                ")"
            )
            # Get current version
            cursor = await conn.execute(
                "SELECT MAX(version) FROM _schema_version"
            )
            row = await cursor.fetchone()
            current_version: int = row[0] if row and row[0] else 0

            # Apply base schema (version 1)
            if current_version < 1:
                for statement in SCHEMA_SQL.split(";"):
                    stripped = statement.strip()
                    if stripped:
                        await conn.execute(stripped)
                await conn.execute(
                    "INSERT INTO _schema_version (version, applied_at) "
                    "VALUES (1, datetime('now'))"
                )
                current_version = 1

            # Apply indexes
            for statement in INDEXES_SQL.split(";"):
                stripped = statement.strip()
                if stripped:
                    await conn.execute(stripped)

            # Apply incremental migrations
            for version in range(current_version + 1, SCHEMA_VERSION + 1):
                sql = MIGRATIONS.get(version)
                if sql:
                    try:
                        await conn.execute(sql)
                    except aiosqlite.OperationalError as exc:
                        if "duplicate column" not in str(exc).lower():
                            raise
                    await conn.execute(
                        "INSERT INTO _schema_version (version, applied_at) "
                        "VALUES (?, datetime('now'))",
                        (version,),
                    )
                    logger.info("Applied schema migration v%d", version)

    # ── Transactions ────────────────────────────────────────────────

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Safe transaction context manager.

        Commits on success, rolls back on exception.
        Supports nesting (SQLite savepoints).
        """
        conn = self.conn
        try:
            await conn.execute("BEGIN")
            yield conn
            await conn.commit()
        except aiosqlite.Error as exc:
            try:
                await conn.rollback()
            except aiosqlite.Error:
                pass
            raise DatabaseTransactionError(str(exc)) from exc
        except BaseException:
            try:
                await conn.rollback()
            except aiosqlite.Error:
                pass
            raise

    # ── Quick health check ──────────────────────────────────────────

    async def health(self) -> bool:
        """Return True if the database is reachable."""
        try:
            cursor = await self.conn.execute("SELECT 1")
            await cursor.fetchone()
            return True
        except (aiosqlite.Error, DatabaseConnectionError):
            return False


db = Database()
