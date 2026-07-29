"""Abstract execution adapter interface."""

from abc import ABC, abstractmethod
from typing import Any


class ExecutionAdapter(ABC):
    """Interface for order execution (shared by paper and live)."""

    @abstractmethod
    async def submit(self, order: dict[str, Any]) -> dict[str, Any]:
        """Submit an order and return the result."""
        ...

    @abstractmethod
    async def cancel(self, order_id: str) -> bool:
        """Cancel an order by ID."""
        ...

    @abstractmethod
    async def status(self, order_id: str) -> dict[str, Any]:
        """Get the current status of an order."""
        ...
