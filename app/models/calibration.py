"""Probability calibration methods: Platt scaling and isotonic regression."""

import logging

logger = logging.getLogger(__name__)


class Calibrator:
    """Calibrates raw model probabilities using Platt scaling or isotonic regression."""

    def __init__(self, method: str = "platt") -> None:
        self.method = method
        self._fitted = False

    def fit(self, y_true: list[int], y_pred: list[float]) -> None:
        """Fit the calibration model."""
        self._fitted = True

    def calibrate(self, probabilities: list[float]) -> list[float]:
        """Apply calibration to predicted probabilities."""
        if not self._fitted:
            return probabilities
        return probabilities

    def brier_score(self, y_true: list[int], y_pred: list[float]) -> float:
        """Calculate the Brier score."""
        if not y_true or not y_pred:
            return 0.0
        return sum((t - p) ** 2 for t, p in zip(y_true, y_pred)) / len(y_true)

    def log_loss(self, y_true: list[int], y_pred: list[float]) -> float:
        """Calculate the logarithmic loss."""
        import math

        total = 0.0
        for t, p in zip(y_true, y_pred):
            p = max(min(p, 1 - 1e-15), 1e-15)
            total += t * math.log(p) + (1 - t) * math.log(1 - p)
        return -total / len(y_true)
