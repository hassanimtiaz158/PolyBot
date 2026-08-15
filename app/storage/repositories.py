"""Type-safe repository layer for all database entities.

Each repository accepts an optional ``Database`` instance (defaults to
the global ``db`` singleton) and returns typed dataclass models.
Every public method is independently testable.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from app.storage.db import Database, DatabaseError
from app.storage.db import db as _db
from app.storage.models import (
    Market,
    MarketSnapshot,
    Order,
    Position,
    RiskEvent,
    Signal,
)

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class _QueryMixin:
    """Shared helpers for parameterised SELECT and COUNT queries."""

    _db: Database

    async def _fetch_rows(
        self, sql: str, params: tuple[object, ...] = ()
    ) -> list[dict[str, object]]:
        cursor = await self._db.conn.execute(sql, params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def _fetch_count(
        self, sql: str, params: tuple[object, ...] = ()
    ) -> int:
        cursor = await self._db.conn.execute(sql, params)
        row = await cursor.fetchone()
        return int(row[0]) if row and row[0] is not None else 0


# ── MarketRepository ────────────────────────────────────────────────


class MarketRepository(_QueryMixin):
    """CRUD for the ``markets`` table."""

    def __init__(self, db: Database | None = None) -> None:
        self._db = db or _db

    async def upsert(self, market: Market) -> Market:
        """Insert a market or update it if it already exists."""
        now = _now()
        created = market.created_at or now
        await self._db.conn.execute(
            """INSERT INTO markets
               (market_id, question, condition_id, status, resolution_time,
                liquidity, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(market_id) DO UPDATE SET
                   question=excluded.question,
                   condition_id=excluded.condition_id,
                   status=excluded.status,
                   resolution_time=excluded.resolution_time,
                   liquidity=excluded.liquidity,
                   updated_at=excluded.updated_at""",
            (
                market.market_id,
                market.question,
                market.condition_id,
                market.status,
                market.resolution_time,
                market.liquidity,
                created,
                now,
            ),
        )
        await self._db.conn.commit()
        return Market(
            market_id=market.market_id,
            question=market.question,
            condition_id=market.condition_id,
            status=market.status,
            resolution_time=market.resolution_time,
            liquidity=market.liquidity,
            created_at=created,
            updated_at=now,
        )

    async def get(self, market_id: str) -> Market | None:
        """Fetch a single market by ID."""
        cursor = await self._db.conn.execute(
            "SELECT * FROM markets WHERE market_id = ?", (market_id,)
        )
        row = await cursor.fetchone()
        return Market.from_row(dict(row)) if row else None

    async def list_all(self) -> list[Market]:
        """Return every known market."""
        cursor = await self._db.conn.execute(
            "SELECT * FROM markets ORDER BY updated_at DESC"
        )
        rows = await cursor.fetchall()
        return [Market.from_row(dict(r)) for r in rows]

    async def delete(self, market_id: str) -> bool:
        """Remove a market by ID. Returns True if a row was deleted."""
        cursor = await self._db.conn.execute(
            "DELETE FROM markets WHERE market_id = ?", (market_id,)
        )
        await self._db.conn.commit()
        return cursor.rowcount > 0

    async def count(self) -> int:
        """Return the total number of markets."""
        cursor = await self._db.conn.execute("SELECT COUNT(*) FROM markets")
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def list_paginated(
        self,
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
    ) -> tuple[list[Market], int]:
        """Return a page of markets and the total number of matches.

        ``status`` filters on the market ``status`` column when provided.
        """
        if status:
            sql = (
                "SELECT * FROM markets WHERE status = ? "
                "ORDER BY updated_at DESC LIMIT ? OFFSET ?"
            )
            rows = await self._fetch_rows(sql, (status, limit, offset))
            total = await self._fetch_count(
                "SELECT COUNT(*) FROM markets WHERE status = ?", (status,)
            )
        else:
            rows = await self._fetch_rows(
                "SELECT * FROM markets ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
            total = await self._fetch_count("SELECT COUNT(*) FROM markets")
        return [Market.from_row(r) for r in rows], total


# ── SnapshotRepository ──────────────────────────────────────────────


class SnapshotRepository:
    """CRUD for the ``market_snapshots`` table."""

    def __init__(self, db: Database | None = None) -> None:
        self._db = db or _db

    async def insert(self, snapshot: MarketSnapshot) -> int:
        """Insert a snapshot row and return its auto-generated id."""
        timestamp = snapshot.timestamp or _now()
        cursor = await self._db.conn.execute(
            """INSERT INTO market_snapshots
               (market_id, timestamp, bid, ask, midpoint, spread,
                bid_depth, ask_depth, volume, time_to_resolution)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                snapshot.market_id,
                timestamp,
                snapshot.bid,
                snapshot.ask,
                snapshot.midpoint,
                snapshot.spread,
                snapshot.bid_depth,
                snapshot.ask_depth,
                snapshot.volume,
                snapshot.time_to_resolution,
            ),
        )
        await self._db.conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def get(self, snapshot_id: int) -> MarketSnapshot | None:
        """Fetch a single snapshot by its auto-increment id."""
        cursor = await self._db.conn.execute(
            "SELECT * FROM market_snapshots WHERE id = ?", (snapshot_id,)
        )
        row = await cursor.fetchone()
        return MarketSnapshot.from_row(dict(row)) if row else None

    async def list_by_market(
        self, market_id: str, limit: int = 100, offset: int = 0
    ) -> list[MarketSnapshot]:
        """Return the most recent snapshots for a given market."""
        cursor = await self._db.conn.execute(
            "SELECT * FROM market_snapshots WHERE market_id = ? "
            "ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            (market_id, limit, offset),
        )
        rows = await cursor.fetchall()
        return [MarketSnapshot.from_row(dict(r)) for r in rows]

    async def count_by_market(self, market_id: str) -> int:
        """Count snapshots for a given market."""
        cursor = await self._db.conn.execute(
            "SELECT COUNT(*) FROM market_snapshots WHERE market_id = ?",
            (market_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def delete_older_than(self, market_id: str, before: str) -> int:
        """Delete snapshots older than a given timestamp. Returns count."""
        cursor = await self._db.conn.execute(
            "DELETE FROM market_snapshots WHERE market_id = ? AND timestamp < ?",
            (market_id, before),
        )
        await self._db.conn.commit()
        return cursor.rowcount

    async def _fetch_rows(
        self, sql: str, params: tuple[object, ...] = ()
    ) -> list[dict[str, object]]:
        """Execute a parameterised SELECT and return rows as dicts."""
        cursor = await self._db.conn.execute(sql, params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def latest_spreads(self) -> list[dict[str, object]]:
        """Return the most recent spread per market that has snapshots."""
        return await self._fetch_rows(
            "SELECT s.market_id, s.spread FROM market_snapshots s "
            "WHERE s.timestamp = ("
            "  SELECT MAX(m.timestamp) FROM market_snapshots m "
            "  WHERE m.market_id = s.market_id"
            ")"
        )

    async def latest_timestamp(self) -> str | None:
        """Return the timestamp of the most recent snapshot across markets."""
        cursor = await self._db.conn.execute(
            "SELECT MAX(timestamp) FROM market_snapshots"
        )
        row = await cursor.fetchone()
        return str(row[0]) if row and row[0] is not None else None

    async def list_recent(self, limit: int, offset: int = 0) -> list[MarketSnapshot]:
        """Return the most recent snapshots across all markets, sorted by timestamp descending."""
        cursor = await self._db.conn.execute(
            "SELECT * FROM market_snapshots ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        rows = await cursor.fetchall()
        return [MarketSnapshot.from_row(dict(r)) for r in rows]


# ── SignalRepository ────────────────────────────────────────────────


class SignalRepository(_QueryMixin):
    """CRUD for the ``signals`` table."""

    def __init__(self, db: Database | None = None) -> None:
        self._db = db or _db

    async def insert(self, signal: Signal) -> None:
        """Insert a signal record.

        If the signal_id already exists, the operation is silently ignored
        (duplicate signals from the same evaluation are idempotent).
        """
        timestamp = signal.timestamp or _now()
        try:
            await self._db.conn.execute(
                """INSERT OR IGNORE INTO signals
                   (signal_id, market_id, strategy, side, model_probability,
                    implied_probability, gross_edge, estimated_cost, net_edge,
                    confidence, decision, rejection_reason, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    signal.signal_id,
                    signal.market_id,
                    signal.strategy,
                    signal.side,
                    signal.model_probability,
                    signal.implied_probability,
                    signal.gross_edge,
                    signal.estimated_cost,
                    signal.net_edge,
                    signal.confidence,
                    signal.decision,
                    signal.rejection_reason,
                    timestamp,
                ),
            )
            await self._db.conn.commit()
        except DatabaseError:
            raise

    async def get(self, signal_id: str) -> Signal | None:
        """Fetch a signal by ID."""
        cursor = await self._db.conn.execute(
            "SELECT * FROM signals WHERE signal_id = ?", (signal_id,)
        )
        row = await cursor.fetchone()
        return Signal.from_row(dict(row)) if row else None

    async def list_recent(
        self, limit: int = 50, market_id: str | None = None
    ) -> list[Signal]:
        """Return the most recent signals, optionally filtered by market."""
        if market_id:
            cursor = await self._db.conn.execute(
                "SELECT * FROM signals WHERE market_id = ? "
                "ORDER BY timestamp DESC LIMIT ?",
                (market_id, limit),
            )
        else:
            cursor = await self._db.conn.execute(
                "SELECT * FROM signals ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            )
        rows = await cursor.fetchall()
        return [Signal.from_row(dict(r)) for r in rows]

    async def count(self) -> int:
        """Return the total number of signals."""
        cursor = await self._db.conn.execute("SELECT COUNT(*) FROM signals")
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def count_candidates(self) -> int:
        """Return the number of signals with a ``CANDIDATE`` decision.

        Used by the dashboard to report the count of active signals.
        """
        return await self._fetch_count(
            "SELECT COUNT(*) FROM signals WHERE decision = 'CANDIDATE'"
        )

    _SORT_COLUMNS = frozenset(
        {
            "timestamp",
            "net_edge",
            "confidence",
            "gross_edge",
            "model_probability",
            "implied_probability",
        }
    )

    async def list_paginated(
        self,
        limit: int = 50,
        offset: int = 0,
        market_id: str | None = None,
        strategy: str | None = None,
        decision: str | None = None,
        min_edge: float | None = None,
        min_confidence: float | None = None,
        sort_by: str | None = None,
        sort_order: str = "desc",
    ) -> tuple[list[Signal], int]:
        """Return a page of signals and the total number of matches.

        Filters combine with AND semantics; each is applied only when
        a non-None value is provided.

        *sort_by* must be a column present in ``_SORT_COLUMNS``; the
        direction is controlled by *sort_order* (``ASC`` or ``DESC``).
        """
        clauses: list[str] = []
        params: list[object] = []
        if market_id:
            clauses.append("market_id = ?")
            params.append(market_id)
        if strategy:
            clauses.append("strategy = ?")
            params.append(strategy)
        if decision:
            clauses.append("decision = ?")
            params.append(decision)
        if min_edge is not None:
            clauses.append("net_edge >= ?")
            params.append(min_edge)
        if min_confidence is not None:
            clauses.append("confidence >= ?")
            params.append(min_confidence)
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""

        order = "timestamp DESC"
        if sort_by and sort_by in self._SORT_COLUMNS:
            direction = "ASC" if str(sort_order).upper() == "ASC" else "DESC"
            order = f"{sort_by} {direction}"

        rows = await self._fetch_rows(
            f"SELECT * FROM signals {where}"
            f"ORDER BY {order} LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        total = await self._fetch_count(
            f"SELECT COUNT(*) FROM signals {where}", tuple(params)
        )
        return [Signal.from_row(r) for r in rows], total


# ── OrderRepository ─────────────────────────────────────────────────


class OrderRepository(_QueryMixin):
    """CRUD for the ``orders`` table."""

    def __init__(self, db: Database | None = None) -> None:
        self._db = db or _db

    async def insert(self, order: Order) -> None:
        """Insert a new order record.

        If the order_id already exists the insert is silently ignored
        (idempotent — prevents duplicate orders on retry).
        """
        await self._db.conn.execute(
            """INSERT OR IGNORE INTO orders
               (order_id, market_id, side, requested_price, requested_size,
                status, filled_size, average_fill, submitted_at, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                order.order_id,
                order.market_id,
                order.side,
                order.requested_price,
                order.requested_size,
                order.status,
                order.filled_size,
                order.average_fill,
                order.submitted_at,
                order.completed_at,
            ),
        )
        await self._db.conn.commit()

    async def get(self, order_id: str) -> Order | None:
        """Fetch an order by ID."""
        cursor = await self._db.conn.execute(
            "SELECT * FROM orders WHERE order_id = ?", (order_id,)
        )
        row = await cursor.fetchone()
        return Order.from_row(dict(row)) if row else None

    async def update_status(self, order: Order) -> None:
        """Update the mutable fields of an existing order."""
        await self._db.conn.execute(
            "UPDATE orders SET status = ?, filled_size = ?, "
            "average_fill = ?, completed_at = ? WHERE order_id = ?",
            (
                order.status,
                order.filled_size,
                order.average_fill,
                order.completed_at,
                order.order_id,
            ),
        )
        await self._db.conn.commit()

    async def list_by_market(
        self, market_id: str, status: str | None = None
    ) -> list[Order]:
        """Return orders for a market, optionally filtered by status."""
        if status:
            cursor = await self._db.conn.execute(
                "SELECT * FROM orders WHERE market_id = ? AND status = ? "
                "ORDER BY submitted_at DESC",
                (market_id, status),
            )
        else:
            cursor = await self._db.conn.execute(
                "SELECT * FROM orders WHERE market_id = ? "
                "ORDER BY submitted_at DESC",
                (market_id,),
            )
        rows = await cursor.fetchall()
        return [Order.from_row(dict(r)) for r in rows]

    async def list_open(self) -> list[Order]:
        """Return all orders that are not in a terminal state."""
        cursor = await self._db.conn.execute(
            "SELECT * FROM orders WHERE status NOT IN "
            "('FILLED', 'REJECTED', 'CANCELLED') "
            "ORDER BY submitted_at DESC"
        )
        rows = await cursor.fetchall()
        return [Order.from_row(dict(r)) for r in rows]

    async def list_paginated(
        self,
        limit: int = 50,
        offset: int = 0,
        market_id: str | None = None,
        status: str | None = None,
    ) -> tuple[list[Order], int]:
        """Return a page of orders and the total number of matches."""
        clauses: list[str] = []
        params: list[object] = []
        if market_id:
            clauses.append("market_id = ?")
            params.append(market_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        rows = await self._fetch_rows(
            f"SELECT * FROM orders {where}"
            "ORDER BY submitted_at DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        total = await self._fetch_count(
            f"SELECT COUNT(*) FROM orders {where}", tuple(params)
        )
        return [Order.from_row(r) for r in rows], total

    async def count(self) -> int:
        """Return the total number of orders."""
        return await self._fetch_count("SELECT COUNT(*) FROM orders")

    async def list_filled(self) -> list[Order]:
        """Return all filled (or partially filled) orders by submission time.

        Used to reconstruct historical P&L series and exposure/streak
        aggregates for the dashboard.
        """
        cursor = await self._db.conn.execute(
            "SELECT * FROM orders WHERE status IN ('FILLED', 'PARTIALLY_FILLED') "
            "ORDER BY submitted_at ASC"
        )
        rows = await cursor.fetchall()
        return [Order.from_row(dict(r)) for r in rows]

    async def count_filled(self) -> int:
        """Return the number of orders in a filled (or partially filled) state."""
        return await self._fetch_count(
            "SELECT COUNT(*) FROM orders WHERE status IN "
            "('FILLED', 'PARTIALLY_FILLED')"
        )


# ── PositionRepository ──────────────────────────────────────────────


class PositionRepository(_QueryMixin):
    """CRUD for the ``positions`` table."""

    def __init__(self, db: Database | None = None) -> None:
        self._db = db or _db

    async def upsert(self, position: Position) -> None:
        """Insert a position or update it if it already exists."""
        await self._db.conn.execute(
            """INSERT INTO positions
               (position_id, market_id, side, size, average_entry,
                current_price, realised_pnl, unrealised_pnl)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(position_id) DO UPDATE SET
                   size=excluded.size,
                   average_entry=excluded.average_entry,
                   current_price=excluded.current_price,
                   realised_pnl=excluded.realised_pnl,
                   unrealised_pnl=excluded.unrealised_pnl""",
            (
                position.position_id,
                position.market_id,
                position.side,
                position.size,
                position.average_entry,
                position.current_price,
                position.realised_pnl,
                position.unrealised_pnl,
            ),
        )
        await self._db.conn.commit()

    async def get(self, position_id: str) -> Position | None:
        """Fetch a position by ID."""
        cursor = await self._db.conn.execute(
            "SELECT * FROM positions WHERE position_id = ?", (position_id,)
        )
        row = await cursor.fetchone()
        return Position.from_row(dict(row)) if row else None

    async def list_open(self) -> list[Position]:
        """Return all positions with size > 0."""
        cursor = await self._db.conn.execute(
            "SELECT * FROM positions WHERE size > 0"
        )
        rows = await cursor.fetchall()
        return [Position.from_row(dict(r)) for r in rows]

    async def list_all(self) -> list[Position]:
        """Return every position record."""
        cursor = await self._db.conn.execute(
            "SELECT * FROM positions ORDER BY market_id"
        )
        rows = await cursor.fetchall()
        return [Position.from_row(dict(r)) for r in rows]

    async def total_exposure(self) -> float:
        """Sum of absolute sizes across all open positions."""
        cursor = await self._db.conn.execute(
            "SELECT COALESCE(SUM(ABS(size)), 0) FROM positions WHERE size != 0"
        )
        row = await cursor.fetchone()
        return float(row[0]) if row else 0.0

    async def list_paginated(
        self,
        limit: int = 50,
        offset: int = 0,
        side: str | None = None,
        open_only: bool = True,
        market_id: str | None = None,
    ) -> tuple[list[Position], int]:
        """Return a page of positions and the total number of matches.

        ``open_only`` restricts results to positions with ``size > 0``;
        ``market_id`` restricts results to a single market.
        """
        clauses: list[str] = []
        params: list[object] = []
        if side:
            clauses.append("side = ?")
            params.append(side)
        if market_id:
            clauses.append("market_id = ?")
            params.append(market_id)
        if open_only:
            clauses.append("size > 0")
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        rows = await self._fetch_rows(
            f"SELECT * FROM positions {where}ORDER BY market_id "
            "LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        total = await self._fetch_count(
            f"SELECT COUNT(*) FROM positions {where}", tuple(params)
        )
        return [Position.from_row(r) for r in rows], total

    async def count(self, open_only: bool = False) -> int:
        """Return the number of position records (optionally open only)."""
        if open_only:
            return await self._fetch_count(
                "SELECT COUNT(*) FROM positions WHERE size > 0"
            )
        return await self._fetch_count("SELECT COUNT(*) FROM positions")

    async def pnl_summary(self) -> dict[str, float]:
        """Return realised/unrealised P&L totals across all positions."""
        cursor = await self._db.conn.execute(
            "SELECT COALESCE(SUM(realised_pnl), 0), "
            "COALESCE(SUM(unrealised_pnl), 0) FROM positions"
        )
        row = await cursor.fetchone()
        return {
            "total_realised_pnl": float(row[0]) if row else 0.0,
            "total_unrealised_pnl": float(row[1]) if row else 0.0,
        }


# ── RiskEventRepository ─────────────────────────────────────────────


class RiskEventRepository(_QueryMixin):
    """CRUD for the ``risk_events`` table."""

    def __init__(self, db: Database | None = None) -> None:
        self._db = db or _db

    async def insert(self, event: RiskEvent) -> None:
        """Insert a risk event.

        If the same event_id already exists the insert is silently ignored
        so that replaying logs does not produce duplicates.
        """
        timestamp = event.timestamp or _now()
        await self._db.conn.execute(
            "INSERT OR IGNORE INTO risk_events "
            "(event_id, event_type, severity, details, timestamp, "
            "market_id, strategy, decision, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.event_id,
                event.event_type,
                event.severity,
                event.details,
                timestamp,
                event.market_id,
                event.strategy,
                event.decision,
                event.reason,
            ),
        )
        await self._db.conn.commit()

    async def get(self, event_id: str) -> RiskEvent | None:
        """Fetch a single risk event by ID."""
        cursor = await self._db.conn.execute(
            "SELECT * FROM risk_events WHERE event_id = ?", (event_id,)
        )
        row = await cursor.fetchone()
        return RiskEvent.from_row(dict(row)) if row else None

    async def list_recent(
        self, limit: int = 50, event_type: str | None = None
    ) -> list[RiskEvent]:
        """Return the most recent risk events, optionally filtered by type."""
        if event_type:
            cursor = await self._db.conn.execute(
                "SELECT * FROM risk_events WHERE event_type = ? "
                "ORDER BY timestamp DESC LIMIT ?",
                (event_type, limit),
            )
        else:
            cursor = await self._db.conn.execute(
                "SELECT * FROM risk_events ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            )
        rows = await cursor.fetchall()
        return [RiskEvent.from_row(dict(r)) for r in rows]

    async def count(self) -> int:
        """Return the total number of risk events logged."""
        cursor = await self._db.conn.execute(
            "SELECT COUNT(*) FROM risk_events"
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def list_paginated(
        self,
        limit: int = 50,
        offset: int = 0,
        event_type: str | None = None,
        severity: str | None = None,
        market_id: str | None = None,
    ) -> tuple[list[RiskEvent], int]:
        """Return a page of risk events and the total number of matches."""
        clauses: list[str] = []
        params: list[object] = []
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        if severity:
            clauses.append("severity = ?")
            params.append(severity)
        if market_id:
            clauses.append("market_id = ?")
            params.append(market_id)
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        rows = await self._fetch_rows(
            f"SELECT * FROM risk_events {where}"
            "ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        total = await self._fetch_count(
            f"SELECT COUNT(*) FROM risk_events {where}", tuple(params)
        )
        return [RiskEvent.from_row(r) for r in rows], total


# ── Singleton instances ─────────────────────────────────────────────

market_repo = MarketRepository()
snapshot_repo = SnapshotRepository()
signal_repo = SignalRepository()
order_repo = OrderRepository()
position_repo = PositionRepository()
risk_event_repo = RiskEventRepository()
