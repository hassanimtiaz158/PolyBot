"""Probability calibration using Platt scaling and isotonic regression.

Calibrates raw model scores into well-calibrated probabilities and
provides evaluation metrics: Brier score, log loss, expected
calibration error (ECE), and reliability-diagram data.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression

CALIBRATION_VERSION = "0.1.0"


class CalibrationError(Exception):
    """Raised when calibration fails or produces invalid results."""


@dataclass
class ReliabilityBin:
    """A single bin from a reliability analysis."""

    bin_index: int
    bin_center: float
    mean_predicted: float
    mean_actual: float
    count: int


class Calibrator:
    """Calibrates raw model scores into probabilities.

    Parameters
    ----------
    method : str
        ``"platt"`` for Platt scaling (sigmoid) or ``"isotonic"`` for
        isotonic regression.

    Attributes
    ----------
    version : str
        Calibrator implementation version.
    """

    def __init__(self, method: str = "platt") -> None:
        if method not in ("platt", "isotonic"):
            raise ValueError(f"Unknown calibration method: {method!r}")
        self.method = method
        self.version = CALIBRATION_VERSION
        self._calibrator: LogisticRegression | IsotonicRegression | None = None
        self._fitted = False

    # ── Fit & calibrate ─────────────────────────────────────────────

    def fit(self, y_true: list[int], y_pred: list[float]) -> None:
        """Fit the calibration model on held-out predictions.

        Parameters
        ----------
        y_true : list[int]
            Ground-truth binary labels (0 or 1).
        y_pred : list[float]
            Raw model scores / probabilities before calibration.
        """
        if len(y_true) != len(y_pred):
            raise CalibrationError(
                f"y_true ({len(y_true)}) and y_pred ({len(y_pred)}) "
                "must have the same length"
            )
        if len(y_true) < 2:
            raise CalibrationError(
                f"Need at least 2 samples for calibration, got {len(y_true)}"
            )

        X = np.array(y_pred).reshape(-1, 1)
        y = np.array(y_true, dtype=int)

        if self.method == "platt":
            cal = LogisticRegression(solver="lbfgs")
            cal.fit(X, y)
            self._calibrator = cal
        else:
            cal = IsotonicRegression(out_of_bounds="clip")
            cal.fit(X.flatten(), y)
            self._calibrator = cal

        self._fitted = True

    def calibrate(self, probabilities: list[float]) -> list[float]:
        """Apply calibration to a list of raw probabilities.

        Parameters
        ----------
        probabilities : list[float]
            Raw scores to calibrate.

        Returns
        -------
        list[float]
            Calibrated probabilities.
        """
        if not self._fitted or self._calibrator is None:
            return probabilities

        X = np.array(probabilities).reshape(-1, 1)
        if self.method == "platt":
            probs = self._calibrator.predict_proba(X)[:, 1]
        else:
            probs = self._calibrator.predict(X.flatten())
        return [float(p) for p in np.clip(probs, 0.0, 1.0)]

    def calibrate_one(self, probability: float) -> float:
        """Calibrate a single probability value."""
        return self.calibrate([probability])[0]

    # ── Evaluation metrics ──────────────────────────────────────────

    @staticmethod
    def brier_score(y_true: list[int], y_pred: list[float]) -> float:
        """Calculate the Brier score (mean squared error).

        Lower is better.  Range [0, 1].
        """
        if not y_true or not y_pred:
            raise CalibrationError("Empty input for Brier score")
        if len(y_true) != len(y_pred):
            raise CalibrationError("y_true and y_pred length mismatch")
        return float(
            sum((t - p) ** 2 for t, p in zip(y_true, y_pred)) / len(y_true)
        )

    @staticmethod
    def log_loss(y_true: list[int], y_pred: list[float]) -> float:
        """Calculate the logarithmic loss (cross-entropy).

        Lower is better.  Clamps predictions to avoid log(0).
        """
        if not y_true or not y_pred:
            raise CalibrationError("Empty input for log loss")
        if len(y_true) != len(y_pred):
            raise CalibrationError("y_true and y_pred length mismatch")
        total = 0.0
        for t, p in zip(y_true, y_pred):
            p = max(min(p, 1.0 - 1e-15), 1e-15)
            total += t * math.log(p) + (1.0 - t) * math.log(1.0 - p)
        return float(-total / len(y_true))

    @staticmethod
    def expected_calibration_error(
        y_true: list[int],
        y_pred: list[float],
        n_bins: int = 10,
    ) -> float:
        """Expected Calibration Error (ECE).

        Partitions predictions into *n_bins* and computes the weighted
        average absolute difference between mean predicted and mean
        actual in each bin.
        """
        if not y_true or not y_pred:
            raise CalibrationError("Empty input for ECE")
        if len(y_true) != len(y_pred):
            raise CalibrationError("y_true and y_pred length mismatch")

        bins = Calibrator.reliability_analysis(y_true, y_pred, n_bins)
        total_samples = sum(b.count for b in bins)
        if total_samples == 0:
            return 0.0
        ece = sum(
            b.count * abs(b.mean_predicted - b.mean_actual) for b in bins
        )
        return ece / total_samples

    @staticmethod
    def reliability_analysis(
        y_true: list[int],
        y_pred: list[float],
        n_bins: int = 10,
    ) -> list[ReliabilityBin]:
        """Compute per-bin data for reliability diagrams.

        Returns a list of ``ReliabilityBin`` dataclass instances.
        """
        if len(y_true) != len(y_pred):
            raise CalibrationError("y_true and y_pred length mismatch")
        if n_bins < 1:
            raise CalibrationError("n_bins must be >= 1")

        y_true_arr = np.array(y_true, dtype=float)
        y_pred_arr = np.array(y_pred, dtype=float)

        bins: list[ReliabilityBin] = []
        for i in range(n_bins):
            lo = i / n_bins
            hi = (i + 1) / n_bins
            mask = (y_pred_arr >= lo) & (y_pred_arr < hi)
            count = int(mask.sum())
            if count == 0:
                bins.append(
                    ReliabilityBin(
                        bin_index=i,
                        bin_center=(lo + hi) / 2.0,
                        mean_predicted=0.0,
                        mean_actual=0.0,
                        count=0,
                    )
                )
            else:
                bins.append(
                    ReliabilityBin(
                        bin_index=i,
                        bin_center=(lo + hi) / 2.0,
                        mean_predicted=float(y_pred_arr[mask].mean()),
                        mean_actual=float(y_true_arr[mask].mean()),
                        count=count,
                    )
                )
        return bins

    # ── Serialization ───────────────────────────────────────────────

    def save(self, path: str) -> None:
        """Serialize the calibrator to disk."""
        from app.models._security import safe_dump

        state = {
            "version": self.version,
            "method": self.method,
            "calibrator": self._calibrator,
        }
        safe_dump(state, path)

    @classmethod
    def load(cls, path: str) -> Calibrator:
        """Deserialize a calibrator from disk."""
        from app.models._security import safe_load

        state = safe_load(path)
        obj = cls(method=state["method"])
        obj._calibrator = state["calibrator"]
        obj._fitted = state["calibrator"] is not None
        return obj
