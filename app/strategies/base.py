"""Abstract base class and data types for all strategies."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class Signal:
    """Output of a single strategy evaluation."""

    strategy: str
    market_id: str
    side: str
    model_probability: float
    confidence: float
    reason: str


class Strategy(ABC):
    """Interface that every strategy module must implement."""

    name: str = "base"

    @abstractmethod
    def generate_signal(
        self, snapshot: dict[str, Any], features: dict[str, Any], context: dict[str, Any]
    ) -> Signal | None:
        """Evaluate market data and return a signal, or None if no trade."""
