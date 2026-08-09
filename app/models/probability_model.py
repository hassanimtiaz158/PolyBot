"""Probability model — training, inference, serialisation, and evaluation.

Provides a ``ProbabilityModel`` class that wraps scikit-learn's logistic
regression (baseline) with optional LightGBM for larger datasets.

Key design decisions
--------------------
* **Training / inference separation** — ``train()`` is a class method that
  returns a fitted instance; ``predict()`` and ``predict_proba()`` are
  instance methods that assume the model is ready.
* **Time-aware splitting** — training always uses a chronological split
  to prevent look-ahead leakage.
* **Model versioning** — every training run increments the version
  (major.minor.patch) stored in the serialised artefact.
* **Confidence gating** — ``predict()`` returns ``None`` for the
  probability when confidence is below the threshold, signalling
  ``NO_TRADE`` to the caller.
* **Feature expectations** — a known feature list is declared at init;
  missing features are filled with 0.0 and flagged.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np
from lightgbm import LGBMClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

from app.models.calibration import Calibrator

logger = logging.getLogger(__name__)

MODEL_VERSION = "0.1.0"

# Default feature columns expected by the model.
DEFAULT_FEATURES: list[str] = [
    "midpoint",
    "spread",
    "obi",
    "bid_depth",
    "ask_depth",
    "volume",
    "velocity_60s",
    "realised_volatility",
    "time_to_resolution",
    "liquidity_score",
    "relative_spread",
]


class ModelNotReadyError(Exception):
    """Raised when an operation requires a fitted model."""


@dataclass
class ModelOutput:
    """Output of a single model prediction.

    When ``probability`` is ``None`` the caller should treat this as
    ``NO_TRADE`` — the model's confidence was insufficient.
    """

    probability: float | None
    confidence: float
    model_version: str
    timestamp: str
    warnings: list[str] = field(default_factory=list)


class ProbabilityModel:
    """Probability estimation model with calibration and versioning.

    Parameters
    ----------
    model_type : str
        ``"logistic"`` (default) or ``"tree"``.
    features : list[str] | None
        Feature column names.  Defaults to ``DEFAULT_FEATURES``.
    min_confidence : float
        Minimum confidence threshold.  Predictions below this return
        ``probability=None``.
    calibration_method : str
        ``"platt"`` or ``"isotonic"``.
    version : str
        Model version string.
    """

    def __init__(
        self,
        model_type: str = "logistic",
        features: list[str] | None = None,
        min_confidence: float = 0.6,
        calibration_method: str = "platt",
        version: str = MODEL_VERSION,
    ) -> None:
        if model_type not in ("logistic", "tree"):
            raise ValueError(f"Unknown model_type: {model_type!r}")
        self.model_type = model_type
        self.features = features or list(DEFAULT_FEATURES)
        self.min_confidence = min_confidence
        self.calibration_method = calibration_method
        self.version = version
        self._classifier: LogisticRegression | LGBMClassifier | None = None
        self._scaler: StandardScaler | None = None
        self._calibrator: Calibrator | None = None
        self._fitted = False
        self._feature_names: list[str] = []
        self._training_date: str | None = None
        self._model_id: str = ""

    # ── Training (class method — returns fitted instance) ───────────

    @classmethod
    def train(
        cls,
        X: list[dict[str, Any]] | None = None,
        y: list[int] | None = None,
        *,
        model_type: str = "logistic",
        features: list[str] | None = None,
        min_confidence: float = 0.6,
        calibration_method: str = "platt",
        cal_ratio: float = 0.2,
    ) -> ProbabilityModel:
        """Train a new model on labelled data.

        Parameters
        ----------
        X : list[dict[str, Any]] | None
            Feature dictionaries.  If ``None``, a dummy model is returned
            (for testing or offline mode).
        y : list[int] | None
            Binary labels (0/1).
        model_type : str
            ``"logistic"`` or ``"tree"``.
        features : list[str] | None
            Feature names.
        min_confidence : float
            Minimum confidence threshold.
        calibration_method : str
            ``"platt"`` or ``"isotonic"``.
        cal_ratio : float
            Fraction of training data held out for fitting the calibrator.

        Returns
        -------
        ProbabilityModel
            A fully fitted instance.
        """
        if X is None or y is None:
            return cls._make_dummy(model_type, features, min_confidence)

        inst = cls(
            model_type=model_type,
            features=features,
            min_confidence=min_confidence,
            calibration_method=calibration_method,
            version=cls._bump_version(MODEL_VERSION),
        )
        inst._model_id = uuid.uuid4().hex[:12]
        inst._training_date = datetime.now(UTC).isoformat()
        inst._fitted = True

        # Convert to feature matrix
        feat_names = inst.features
        X_arr, _ = inst._dicts_to_matrix(X, feat_names)
        y_arr = np.array(y, dtype=int)

        # Chronological split for calibration hold-out
        n = len(X_arr)
        n_cal = max(1, int(n * cal_ratio))
        n_train = n - n_cal

        if n_train < 2:
            # Tiny dataset — train on everything, skip calibration
            X_train, y_train = X_arr, y_arr
            X_cal, y_cal = None, None
        else:
            # Last cal_ratio fraction held out for calibration
            X_train = X_arr[:n_train]
            y_train = y_arr[:n_train]
            X_cal = X_arr[n_train:]
            y_cal = y_arr[n_train:]

        # Scale
        scaler = StandardScaler()
        X_train_scaled = scaler.fit_transform(X_train)
        inst._scaler = scaler

        # Train classifier
        if len(np.unique(y_train)) < 2:
            logger.warning(
                "Training data has only one class (%s) — returning dummy model",
                int(y_train[0]) if len(y_train) > 0 else "?",
            )
            return cls._make_dummy(model_type, features, min_confidence)

        if model_type == "logistic":
            clf = LogisticRegression(solver="lbfgs", max_iter=1000, random_state=42)
        else:
            clf = LGBMClassifier(
                n_estimators=200,
                max_depth=5,
                random_state=42,
                verbose=-1,
            )
        try:
            clf.fit(X_train_scaled, y_train)
        except ValueError:
            logger.exception("Classifier training failed — returning dummy model")
            return cls._make_dummy(model_type, features, min_confidence)
        inst._classifier = clf

        # Fit calibrator on held-out predictions (if available)
        if X_cal is not None and y_cal is not None and len(X_cal) >= 2:
            cal_scores = clf.predict_proba(scaler.transform(X_cal))[:, 1]
            calibrator = Calibrator(method=calibration_method)
            calibrator.fit([int(v) for v in y_cal], [float(s) for s in cal_scores])
            inst._calibrator = calibrator

        return inst

    @classmethod
    def _make_dummy(
        cls,
        model_type: str = "logistic",
        features: list[str] | None = None,
        min_confidence: float = 0.6,
    ) -> ProbabilityModel:
        """Create an unfitted dummy model (for offline/testing)."""
        inst = cls(
            model_type=model_type,
            features=features,
            min_confidence=min_confidence,
            version=MODEL_VERSION,
        )
        inst._fitted = False
        return inst

    @staticmethod
    def _bump_version(current: str) -> str:
        """Increment the patch version (semver-like)."""
        parts = current.split(".")
        try:
            patch = int(parts[-1]) + 1
            return ".".join(parts[:-1] + [str(patch)])
        except (IndexError, ValueError):
            return f"{current}.1"

    @staticmethod
    def _dicts_to_matrix(
        X: list[dict[str, Any]],
        feature_names: list[str],
    ) -> tuple[Any, list[list[str]]]:  # numpy ndarray
        """Convert a list of feature dicts to a NumPy matrix.

        Missing features are filled with 0.0.  Returns
        ``(matrix, warnings)`` where each warning is a
        ``[sample_idx, feature_name]`` pair.
        """
        n = len(X)
        m = len(feature_names)
        mat = np.zeros((n, m), dtype=float)
        warnings: list[list[str]] = []
        for i, d in enumerate(X):
            for j, name in enumerate(feature_names):
                val = d.get(name)
                if val is None:
                    mat[i, j] = 0.0
                    warnings.append([str(i), name])
                else:
                    try:
                        mat[i, j] = float(val)
                    except (ValueError, TypeError):
                        mat[i, j] = 0.0
                        warnings.append([str(i), name])
        return mat, warnings

    # ── Training convenience (on already-fitted) ────────────────────

    def _check_fitted(self) -> None:
        if not self._fitted or self._classifier is None:
            raise ModelNotReadyError(
                "Model has not been trained. Call ProbabilityModel.train() first."
            )

    # ── Inference ───────────────────────────────────────────────────

    def predict(self, features: dict[str, Any]) -> ModelOutput:
        """Predict probability for a single sample.

        Parameters
        ----------
        features : dict[str, Any]
            Feature dictionary.

        Returns
        -------
        ModelOutput
            If ``probability`` is ``None``, confidence was below
            ``min_confidence`` → treat as ``NO_TRADE``.
        """
        self._check_fitted()

        matrix, _ = self._dicts_to_matrix([features], self.features)
        scaled = self._scaler.transform(matrix)  # type: ignore[union-attr]

        raw_proba = float(self._classifier.predict_proba(scaled)[0, 1])  # type: ignore[union-attr]

        # Calibrate
        if self._calibrator is not None:
            calibrated = self._calibrator.calibrate_one(raw_proba)
        else:
            calibrated = raw_proba

        # Distance to 0.5 as a confidence proxy
        margin = abs(calibrated - 0.5) * 2.0  # 0 → 0%, 1 → 100%
        confidence = min(1.0, margin)

        warnings: list[str] = []
        if confidence < self.min_confidence:
            return ModelOutput(
                probability=None,
                confidence=confidence,
                model_version=self.version,
                timestamp=datetime.now(UTC).isoformat(),
                warnings=["Confidence below minimum threshold: NO_TRADE"],
            )

        return ModelOutput(
            probability=calibrated,
            confidence=confidence,
            model_version=self.version,
            timestamp=datetime.now(UTC).isoformat(),
            warnings=warnings,
        )

    def predict_proba(
        self, X: list[dict[str, Any]]
    ) -> list[ModelOutput]:
        """Predict probabilities for a batch of samples."""
        return [self.predict(d) for d in X]

    # ── Evaluation ─────────────────────────────────────────────────

    def evaluate(
        self,
        X: list[dict[str, Any]] | None = None,
        y: list[int] | None = None,
    ) -> dict[str, Any]:
        """Evaluate model performance on labelled data.

        Returns a dict with keys: ``brier_score``, ``log_loss``,
        ``ece``, ``accuracy``, ``n_samples``, ``model_version``.
        """
        self._check_fitted()

        if X is None or y is None:
            return {
                "brier_score": None,
                "log_loss": None,
                "ece": None,
                "accuracy": None,
                "n_samples": 0,
                "model_version": self.version,
            }

        outputs = self.predict_proba(X)
        y_pred: list[float] = []
        y_bin: list[float] = []
        for out, label in zip(outputs, y):
            if out.probability is not None:
                y_pred.append(out.probability)
                y_bin.append(float(label))

        if not y_pred:
            return {
                "brier_score": None,
                "log_loss": None,
                "ece": None,
                "accuracy": None,
                "n_samples": 0,
                "model_version": self.version,
            }

        brier = Calibrator.brier_score([int(v) for v in y_bin], y_pred)
        ll = Calibrator.log_loss([int(v) for v in y_bin], y_pred)
        ece = Calibrator.expected_calibration_error(
            [int(v) for v in y_bin], y_pred
        )
        hard_preds = [1 if p >= 0.5 else 0 for p in y_pred]
        acc = sum(1 for h, t in zip(hard_preds, y_bin) if h == int(t)) / len(
            y_bin
        )

        return {
            "brier_score": brier,
            "log_loss": ll,
            "ece": ece,
            "accuracy": acc,
            "n_samples": len(y_pred),
            "model_version": self.version,
        }

    def evaluate_calibration(
        self,
        X: list[dict[str, Any]] | None = None,
        y: list[int] | None = None,
        n_bins: int = 10,
    ) -> dict[str, Any]:
        """Return reliability-analysis data for diagnostics."""
        self._check_fitted()
        if X is None or y is None:
            return {"bins": [], "n_samples": 0}
        outputs = self.predict_proba(X)
        y_pred = [
            o.probability for o in outputs if o.probability is not None
        ]
        y_filt = [
            float(label)
            for o, label in zip(outputs, y)
            if o.probability is not None
        ]
        bins = Calibrator.reliability_analysis(
            [int(v) for v in y_filt], y_pred, n_bins
        )
        return {
            "bins": [b.__dict__ for b in bins],
            "n_samples": len(y_pred),
        }

    # ── Serialization ───────────────────────────────────────────────

    def save(self, path: str) -> None:
        """Serialise the model to disk."""
        from app.models._security import safe_dump

        state = {
            "version": self.version,
            "model_type": self.model_type,
            "features": self.features,
            "min_confidence": self.min_confidence,
            "calibration_method": self.calibration_method,
            "classifier": self._classifier,
            "scaler": self._scaler,
            "calibrator": self._calibrator,
            "fitted": self._fitted,
            "model_id": self._model_id,
            "training_date": self._training_date,
        }
        safe_dump(state, path)

    @classmethod
    def load(cls, path: str) -> ProbabilityModel:
        """Load a serialised model from disk."""
        from app.models._security import safe_load

        state = safe_load(path)
        obj = cls(
            model_type=state["model_type"],
            features=state["features"],
            min_confidence=state["min_confidence"],
            calibration_method=state["calibration_method"],
            version=state["version"],
        )
        obj._classifier = state["classifier"]
        obj._scaler = state["scaler"]
        obj._calibrator = state["calibrator"]
        obj._fitted = state["fitted"]
        obj._model_id = state.get("model_id", "")
        obj._training_date = state.get("training_date")
        return obj

    # ── Status helpers ──────────────────────────────────────────────

    @property
    def is_ready(self) -> bool:
        """``True`` if the model is fitted and ready for inference."""
        return self._fitted and self._classifier is not None

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def training_date(self) -> str | None:
        return self._training_date
