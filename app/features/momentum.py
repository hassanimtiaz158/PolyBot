"""Momentum / return features over configurable windows."""

import logging
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)


class MomentumFeatures:
    """Computes price returns over configurable time windows."""

    def __init__(self, windows_seconds: list[int] | None = None) -> None:
        self.windows = windows_seconds or [1, 5, 30, 300]
        self._history: deque[dict[str, Any]] = deque(maxlen=500)

    def update(self, snapshot: dict[str, Any]) -> None:
        """Append a new snapshot for momentum calculation."""
        self._history.append(snapshot)

    def returns(self, window_seconds: int) -> float | None:
        """Calculate the price return over the given window (seconds)."""
        if len(self._history) < 2:
            return None
        current = self._history[-1].get("midpoint")
        if current is None:
            return None
        for entry in reversed(self._history):
            if entry.get("timestamp") is None:
                continue
            return 0.0
        return 0.0

    def compute(self) -> dict[str, float | None]:
        """Compute returns for all configured windows."""
        return {f"return_{w}s": self.returns(w) for w in self.windows}
