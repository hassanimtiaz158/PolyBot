"""Typed data models for all database entities."""

from dataclasses import dataclass
from typing import Any


@dataclass
class Market:
    """Represents a single prediction market."""

    market_id: str
    question: str
    condition_id: str | None = None
    status: str | None = None
    resolution_time: str | None = None
    liquidity: float | None = None
    created_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Market":
        return cls(
            market_id=row["market_id"],
            question=row["question"],
            condition_id=row.get("condition_id"),
            status=row.get("status"),
            resolution_time=row.get("resolution_time"),
            liquidity=row.get("liquidity"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )


@dataclass
class MarketSnapshot:
    """Point-in-time snapshot of market order-book data."""

    market_id: str
    id: int | None = None
    timestamp: str | None = None
    bid: float | None = None
    ask: float | None = None
    midpoint: float | None = None
    spread: float | None = None
    bid_depth: float | None = None
    ask_depth: float | None = None
    volume: float | None = None
    time_to_resolution: float | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "MarketSnapshot":
        return cls(
            id=row.get("id"),
            market_id=row["market_id"],
            timestamp=row.get("timestamp"),
            bid=row.get("bid"),
            ask=row.get("ask"),
            midpoint=row.get("midpoint"),
            spread=row.get("spread"),
            bid_depth=row.get("bid_depth"),
            ask_depth=row.get("ask_depth"),
            volume=row.get("volume"),
            time_to_resolution=row.get("time_to_resolution"),
        )


@dataclass
class Signal:
    """Strategy signal output from a single evaluation."""

    signal_id: str
    market_id: str
    strategy: str
    side: str
    decision: str
    model_probability: float | None = None
    implied_probability: float | None = None
    gross_edge: float | None = None
    estimated_cost: float | None = None
    net_edge: float | None = None
    confidence: float | None = None
    rejection_reason: str | None = None
    timestamp: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Signal":
        return cls(
            signal_id=row["signal_id"],
            market_id=row["market_id"],
            strategy=row["strategy"],
            side=row["side"],
            decision=row["decision"],
            model_probability=row.get("model_probability"),
            implied_probability=row.get("implied_probability"),
            gross_edge=row.get("gross_edge"),
            estimated_cost=row.get("estimated_cost"),
            net_edge=row.get("net_edge"),
            confidence=row.get("confidence"),
            rejection_reason=row.get("rejection_reason"),
            timestamp=row.get("timestamp"),
        )


@dataclass
class Order:
    """Order record representing a single submitted order."""

    order_id: str
    market_id: str
    side: str
    status: str
    requested_price: float | None = None
    requested_size: float | None = None
    filled_size: float | None = None
    average_fill: float | None = None
    submitted_at: str | None = None
    completed_at: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Order":
        return cls(
            order_id=row["order_id"],
            market_id=row["market_id"],
            side=row["side"],
            status=row["status"],
            requested_price=row.get("requested_price"),
            requested_size=row.get("requested_size"),
            filled_size=row.get("filled_size"),
            average_fill=row.get("average_fill"),
            submitted_at=row.get("submitted_at"),
            completed_at=row.get("completed_at"),
        )


@dataclass
class Position:
    """Open or closed position in a single market."""

    position_id: str
    market_id: str
    side: str
    size: float | None = None
    average_entry: float | None = None
    current_price: float | None = None
    realised_pnl: float | None = None
    unrealised_pnl: float | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Position":
        return cls(
            position_id=row["position_id"],
            market_id=row["market_id"],
            side=row["side"],
            size=row.get("size"),
            average_entry=row.get("average_entry"),
            current_price=row.get("current_price"),
            realised_pnl=row.get("realised_pnl"),
            unrealised_pnl=row.get("unrealised_pnl"),
        )


@dataclass
class RiskEvent:
    """Risk-related event for audit trail."""

    event_id: str
    event_type: str
    severity: str
    details: str | None = None
    timestamp: str | None = None
    market_id: str | None = None
    strategy: str | None = None
    decision: str | None = None
    reason: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "RiskEvent":
        return cls(
            event_id=row["event_id"],
            event_type=row["event_type"],
            severity=row["severity"],
            details=row.get("details"),
            timestamp=row.get("timestamp"),
            market_id=row.get("market_id"),
            strategy=row.get("strategy"),
            decision=row.get("decision"),
            reason=row.get("reason"),
        )
