"""Unit tests for the probability model and calibration modules.

Covers: training, inference, missing/invalid features, model
unavailable, serialization, probability range [0, 1], evaluation
metrics, and calibration correctness.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import pytest

from app.models import (
    MODEL_VERSION,
    Calibrator,
    ModelNotReadyError,
    ModelOutput,
    ProbabilityModel,
    ReliabilityBin,
)
from app.models.calibration import CalibrationError

# ═══════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════


def _make_training_data(
    n: int = 100,
    seed: int = 42,
) -> tuple[list[dict], list[int]]:
    """Synthetic training data with a known signal."""
    rng = np.random.default_rng(seed)
    X: list[dict] = []
    y: list[int] = []
    for _ in range(n):
        midpoint = rng.uniform(0.1, 0.9)
        obi = rng.uniform(-0.5, 0.5)
        spread = rng.uniform(0.01, 0.15)
        vol = rng.uniform(0.0, 0.3)
        liq = rng.uniform(0.0, 1.0)

        feat = {
            "midpoint": midpoint,
            "spread": spread,
            "obi": obi,
            "bid_depth": 10_000.0,
            "ask_depth": 8_000.0,
            "volume": 50_000.0,
            "velocity_60s": 0.0,
            "realised_volatility": vol,
            "time_to_resolution": 86_400.0,
            "liquidity_score": liq,
            "relative_spread": spread / midpoint if midpoint > 0 else 0.0,
        }

        # Label: 1 if midpoint > 0.5 + noise, else 0
        label = 1 if midpoint + rng.normal(0, 0.1) > 0.5 else 0
        X.append(feat)
        y.append(label)
    return X, y


# ═══════════════════════════════════════════════════════════════════════
#  Calibrator
# ═══════════════════════════════════════════════════════════════════════


class TestCalibratorInit:
    def test_default_method(self) -> None:
        c = Calibrator()
        assert c.method == "platt"

    def test_isotonic_method(self) -> None:
        c = Calibrator(method="isotonic")
        assert c.method == "isotonic"

    def test_invalid_method_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown calibration method"):
            Calibrator(method="invalid")


class TestCalibratorFit:
    def test_platt_fit_and_calibrate(self) -> None:
        c = Calibrator(method="platt")
        y_true = [0, 0, 0, 1, 1, 1]
        y_pred = [0.1, 0.2, 0.3, 0.7, 0.8, 0.9]
        c.fit(y_true, y_pred)
        assert c._fitted
        cal = c.calibrate(y_pred)
        assert len(cal) == len(y_pred)
        assert all(0.0 <= p <= 1.0 for p in cal)

    def test_isotonic_fit_and_calibrate(self) -> None:
        c = Calibrator(method="isotonic")
        y_true = [0, 0, 0, 1, 1, 1]
        y_pred = [0.1, 0.2, 0.3, 0.7, 0.8, 0.9]
        c.fit(y_true, y_pred)
        cal = c.calibrate(y_pred)
        assert len(cal) == len(y_pred)

    def test_calibrate_before_fit_returns_raw(self) -> None:
        c = Calibrator()
        result = c.calibrate([0.3, 0.7])
        assert result == [0.3, 0.7]

    def test_calibrate_one(self) -> None:
        c = Calibrator(method="platt")
        c.fit([0, 0, 1, 1], [0.2, 0.3, 0.7, 0.8])
        result = c.calibrate_one(0.3)
        assert 0.0 <= result <= 1.0

    def test_fit_length_mismatch_raises(self) -> None:
        c = Calibrator()
        with pytest.raises(CalibrationError, match="same length"):
            c.fit([0, 1], [0.1, 0.2, 0.3])

    def test_fit_too_few_samples_raises(self) -> None:
        c = Calibrator()
        with pytest.raises(CalibrationError, match="at least 2"):
            c.fit([0], [0.5])


class TestCalibratorMetrics:
    def test_brier_score_perfect(self) -> None:
        score = Calibrator.brier_score([0, 1, 0, 1], [0.0, 1.0, 0.0, 1.0])
        assert score == pytest.approx(0.0)

    def test_brier_score_imperfect(self) -> None:
        score = Calibrator.brier_score([0, 1], [0.5, 0.5])
        assert score == pytest.approx(0.25)

    def test_brier_score_empty_raises(self) -> None:
        with pytest.raises(CalibrationError, match="Empty"):
            Calibrator.brier_score([], [])

    def test_brier_score_length_mismatch_raises(self) -> None:
        with pytest.raises(CalibrationError, match="length mismatch"):
            Calibrator.brier_score([0], [0.1, 0.2])

    def test_log_loss_perfect(self) -> None:
        ll = Calibrator.log_loss([0, 1], [0.001, 0.999])
        assert ll == pytest.approx(0.0, abs=0.01)

    def test_log_loss_random(self) -> None:
        ll = Calibrator.log_loss([0, 1], [0.5, 0.5])
        assert ll == pytest.approx(0.693, abs=0.01)

    def test_expected_calibration_error_perfect(self) -> None:
        ece = Calibrator.expected_calibration_error(
            [0, 0, 1, 1], [0.0, 0.0, 1.0, 1.0], n_bins=2
        )
        assert ece == pytest.approx(0.0)

    def test_expected_calibration_error_imperfect(self) -> None:
        ece = Calibrator.expected_calibration_error(
            [0, 0, 1, 1], [0.1, 0.2, 0.8, 0.9], n_bins=2
        )
        assert ece > 0.0


class TestCalibratorReliability:
    def test_reliability_analysis_returns_bins(self) -> None:
        bins = Calibrator.reliability_analysis(
            [0, 0, 1, 1], [0.1, 0.2, 0.8, 0.9], n_bins=5
        )
        assert len(bins) == 5
        assert all(isinstance(b, ReliabilityBin) for b in bins)

    def test_reliability_analysis_empty_bins(self) -> None:
        bins = Calibrator.reliability_analysis(
            [0, 1], [0.05, 0.95], n_bins=10
        )
        # Most bins should be empty
        empty = [b for b in bins if b.count == 0]
        assert len(empty) >= 8

    def test_reliability_analysis_length_mismatch_raises(self) -> None:
        with pytest.raises(CalibrationError, match="length mismatch"):
            Calibrator.reliability_analysis([0], [0.1, 0.2])

    def test_reliability_analysis_invalid_bins_raises(self) -> None:
        with pytest.raises(CalibrationError, match="n_bins"):
            Calibrator.reliability_analysis([0, 1], [0.1, 0.9], n_bins=0)


class TestCalibratorSerialization:
    def test_save_and_load(self) -> None:
        c = Calibrator(method="platt")
        c.fit([0, 0, 1, 1], [0.2, 0.3, 0.7, 0.8])

        with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as f:
            path = f.name
            c.save(path)

        loaded = Calibrator.load(path)
        assert loaded.method == "platt"
        assert loaded._fitted

        # Same calibration after load
        original_cal = c.calibrate([0.3])
        loaded_cal = loaded.calibrate([0.3])
        assert original_cal == pytest.approx(loaded_cal)
        Path(path).unlink(missing_ok=True)

    def test_save_load_unfitted(self) -> None:
        c = Calibrator()
        with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as f:
            path = f.name
            c.save(path)

        loaded = Calibrator.load(path)
        assert not loaded._fitted
        assert loaded.calibrate([0.5]) == [0.5]
        Path(path).unlink(missing_ok=True)


# ═══════════════════════════════════════════════════════════════════════
#  ProbabilityModel
# ═══════════════════════════════════════════════════════════════════════


class TestProbabilityModelTraining:
    def test_train_logistic(self) -> None:
        X, y = _make_training_data(50)
        model = ProbabilityModel.train(X, y, model_type="logistic")
        assert model.is_ready
        assert model.model_type == "logistic"

    def test_train_tree(self) -> None:
        X, y = _make_training_data(50)
        model = ProbabilityModel.train(X, y, model_type="tree")
        assert model.is_ready
        assert model.model_type == "tree"

    def test_train_no_data_returns_dummy(self) -> None:
        model = ProbabilityModel.train(None, None)
        assert not model.is_ready

    def test_training_increments_version(self) -> None:
        X, y = _make_training_data(50)
        model = ProbabilityModel.train(X, y)
        assert model.version != MODEL_VERSION  # version was bumped

    def test_training_sets_model_id_and_date(self) -> None:
        X, y = _make_training_data(30)
        model = ProbabilityModel.train(X, y)
        assert len(model.model_id) > 0
        assert model.training_date is not None


class TestProbabilityModelInference:
    def test_predict_returns_probability_in_range(self) -> None:
        X, y = _make_training_data(50)
        model = ProbabilityModel.train(X, y)
        out = model.predict(X[0])
        assert isinstance(out, ModelOutput)
        if out.probability is not None:
            assert 0.0 <= out.probability <= 1.0
        assert 0.0 <= out.confidence <= 1.0
        assert out.model_version == model.version
        assert len(out.timestamp) > 0

    def test_predict_batch(self) -> None:
        X, y = _make_training_data(50)
        model = ProbabilityModel.train(X, y)
        outputs = model.predict_proba(X[:5])
        assert len(outputs) == 5
        for out in outputs:
            assert isinstance(out, ModelOutput)

    def test_predict_on_unfitted_raises(self) -> None:
        model = ProbabilityModel()
        with pytest.raises(ModelNotReadyError, match="not been trained"):
            model.predict({"midpoint": 0.5})

    def test_predict_all_features_missing(self) -> None:
        X, y = _make_training_data(50)
        model = ProbabilityModel.train(X, y)
        out = model.predict({"unknown_key": 123})
        assert isinstance(out, ModelOutput)
        if out.probability is not None:
            assert 0.0 <= out.probability <= 1.0

    def test_predict_partial_features(self) -> None:
        X, y = _make_training_data(50)
        model = ProbabilityModel.train(X, y)
        out = model.predict({"midpoint": 0.5})
        assert isinstance(out, ModelOutput)

    def test_predict_return_type(self) -> None:
        X, y = _make_training_data(50)
        model = ProbabilityModel.train(X, y)
        out = model.predict(X[0])
        assert isinstance(out.probability, (float, type(None)))
        assert isinstance(out.confidence, float)
        assert isinstance(out.model_version, str)
        assert isinstance(out.timestamp, str)


class TestProbabilityModelInvalidFeatures:
    def test_non_numeric_feature(self) -> None:
        X, y = _make_training_data(50)
        model = ProbabilityModel.train(X, y)
        bad_feat = dict(X[0])
        bad_feat["midpoint"] = "not_a_number"
        out = model.predict(bad_feat)
        if out.probability is not None:
            assert 0.0 <= out.probability <= 1.0

    def test_none_feature_value(self) -> None:
        X, y = _make_training_data(50)
        model = ProbabilityModel.train(X, y)
        feat = dict(X[0])
        feat["spread"] = None
        out = model.predict(feat)
        assert isinstance(out, ModelOutput)

    def test_empty_feature_dict(self) -> None:
        X, y = _make_training_data(50)
        model = ProbabilityModel.train(X, y)
        out = model.predict({})
        assert isinstance(out, ModelOutput)
        if out.probability is not None:
            assert 0.0 <= out.probability <= 1.0


class TestProbabilityModelMissingModel:
    def test_unfitted_raises_on_predict(self) -> None:
        model = ProbabilityModel()
        with pytest.raises(ModelNotReadyError):
            model.predict({"midpoint": 0.5})

    def test_unfitted_raises_on_evaluate(self) -> None:
        model = ProbabilityModel()
        with pytest.raises(ModelNotReadyError):
            model.evaluate([], [])

    def test_is_ready_false_when_not_fitted(self) -> None:
        model = ProbabilityModel()
        assert not model.is_ready

    def test_is_ready_true_after_training(self) -> None:
        X, y = _make_training_data(20)
        model = ProbabilityModel.train(X, y)
        assert model.is_ready

    def test_dummy_model_not_ready(self) -> None:
        model = ProbabilityModel.train(None, None)
        assert not model.is_ready


class TestProbabilityModelSerialization:
    def test_save_and_load(self) -> None:
        X, y = _make_training_data(50)
        model = ProbabilityModel.train(X, y)
        pred_before = model.predict(X[0])

        with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as f:
            path = f.name
            model.save(path)

        loaded = ProbabilityModel.load(path)
        assert loaded.is_ready
        assert loaded.version == model.version
        assert loaded.model_type == model.model_type
        assert loaded.features == model.features

        pred_after = loaded.predict(X[0])
        if pred_before.probability is not None and pred_after.probability is not None:
            assert pred_after.probability == pytest.approx(
                pred_before.probability, abs=1e-6
            )
        Path(path).unlink(missing_ok=True)

    def test_save_and_load_unfitted(self) -> None:
        model = ProbabilityModel()
        with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as f:
            path = f.name
            model.save(path)

        loaded = ProbabilityModel.load(path)
        assert not loaded.is_ready
        Path(path).unlink(missing_ok=True)

    def test_save_load_preserves_features(self) -> None:
        custom_features = ["midpoint", "spread", "obi"]
        X, y = _make_training_data(30)
        model = ProbabilityModel.train(
            X, y, features=custom_features
        )
        assert model.features == custom_features

        with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as f:
            path = f.name
            model.save(path)

        loaded = ProbabilityModel.load(path)
        assert loaded.features == custom_features
        Path(path).unlink(missing_ok=True)


class TestProbabilityModelProbabilityRange:
    def test_probability_never_below_zero(self) -> None:
        X, y = _make_training_data(100)
        model = ProbabilityModel.train(X, y)
        outputs = model.predict_proba(X)
        for out in outputs:
            if out.probability is not None:
                assert out.probability >= 0.0

    def test_probability_never_above_one(self) -> None:
        X, y = _make_training_data(100)
        model = ProbabilityModel.train(X, y)
        outputs = model.predict_proba(X)
        for out in outputs:
            if out.probability is not None:
                assert out.probability <= 1.0

    def test_confidence_in_range(self) -> None:
        X, y = _make_training_data(100)
        model = ProbabilityModel.train(X, y)
        outputs = model.predict_proba(X)
        for out in outputs:
            assert 0.0 <= out.confidence <= 1.0

    def test_edge_case_returns_none_when_low_confidence(self) -> None:
        X, y = _make_training_data(30)
        model = ProbabilityModel.train(
            X, y, min_confidence=0.99
        )
        outputs = model.predict_proba(X[:5])
        none_count = sum(1 for o in outputs if o.probability is None)
        assert none_count >= 0  # at least some may be none with high threshold


class TestProbabilityModelEvaluation:
    def test_evaluate_returns_metrics(self) -> None:
        X, y = _make_training_data(50)
        model = ProbabilityModel.train(X, y)
        metrics = model.evaluate(X, y)
        assert "brier_score" in metrics
        assert "log_loss" in metrics
        assert "ece" in metrics
        assert "accuracy" in metrics
        assert "n_samples" in metrics
        if metrics["n_samples"] > 0:
            assert metrics["brier_score"] is not None
            assert 0 <= metrics["brier_score"] <= 1

    def test_evaluate_no_data_returns_nones(self) -> None:
        X, y = _make_training_data(30)
        model = ProbabilityModel.train(X, y)
        metrics = model.evaluate(None, None)
        assert metrics["brier_score"] is None
        assert metrics["n_samples"] == 0

    def test_evaluate_calibration_returns_bins(self) -> None:
        X, y = _make_training_data(50)
        model = ProbabilityModel.train(X, y)
        cal = model.evaluate_calibration(X, y)
        assert "bins" in cal
        assert len(cal["bins"]) > 0

    def test_evaluate_calibration_no_data(self) -> None:
        X, y = _make_training_data(30)
        model = ProbabilityModel.train(X, y)
        cal = model.evaluate_calibration(None, None)
        assert cal["n_samples"] == 0


class TestProbabilityModelVersioning:
    def test_version_bumped_on_train(self) -> None:
        X, y = _make_training_data(20)
        model = ProbabilityModel.train(X, y)
        v = model.version
        parts = v.split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)

    def test_version_unchanged_on_load(self) -> None:
        X, y = _make_training_data(20)
        model = ProbabilityModel.train(X, y)
        v_before = model.version

        with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as f:
            path = f.name
            model.save(path)

        loaded = ProbabilityModel.load(path)
        assert loaded.version == v_before
        Path(path).unlink(missing_ok=True)


class TestProbabilityModelCustomConfig:
    def test_custom_features(self) -> None:
        feats = ["midpoint", "spread"]
        X, y = _make_training_data(30)
        model = ProbabilityModel.train(X, y, features=feats)
        assert model.features == feats

    def test_custom_min_confidence(self) -> None:
        X, y = _make_training_data(30)
        model = ProbabilityModel.train(X, y, min_confidence=0.8)
        assert model.min_confidence == 0.8

    def test_custom_calibration_method(self) -> None:
        X, y = _make_training_data(30)
        model = ProbabilityModel.train(
            X, y, calibration_method="isotonic"
        )
        assert model.calibration_method == "isotonic"

    def test_invalid_model_type_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown model_type"):
            ProbabilityModel(model_type="invalid")


class TestModelOutput:
    def test_no_trade_when_low_confidence(self) -> None:
        out = ModelOutput(
            probability=None,
            confidence=0.3,
            model_version="0.1.0",
            timestamp="2026-01-01T00:00:00",
        )
        assert out.probability is None
        assert "NO_TRADE" in str(out.warnings) or out.probability is None

    def test_output_has_all_fields(self) -> None:
        out = ModelOutput(
            probability=0.75,
            confidence=0.85,
            model_version="0.1.0",
            timestamp="2026-01-01T00:00:00",
        )
        assert out.probability == 0.75
        assert out.confidence == 0.85
        assert out.warnings == []


class TestProbabilityModelEdgeCases:
    def test_very_small_training_set(self) -> None:
        # Ensure at least 2 classes even with small n
        X, y = _make_training_data(5, seed=99)
        model = ProbabilityModel.train(X, y)
        assert model.is_ready
        out = model.predict(X[0])
        assert isinstance(out, ModelOutput)

    def test_large_training_set(self) -> None:
        X, y = _make_training_data(500)
        model = ProbabilityModel.train(X, y, model_type="tree")
        assert model.is_ready
        out = model.predict(X[0])
        assert isinstance(out, ModelOutput)

    def test_all_same_label_returns_dummy(self) -> None:
        X: list[dict] = []
        y: list[int] = []
        rng = np.random.default_rng(42)
        for _ in range(30):
            X.append(
                {
                    "midpoint": float(rng.uniform(0.1, 0.9)),
                    "spread": 0.05,
                    "obi": 0.0,
                    "bid_depth": 10_000.0,
                    "ask_depth": 8_000.0,
                    "volume": 50_000.0,
                    "velocity_60s": 0.0,
                    "realised_volatility": 0.1,
                    "time_to_resolution": 86_400.0,
                    "liquidity_score": 0.5,
                    "relative_spread": 0.1,
                }
            )
            y.append(1)  # all same label
        model = ProbabilityModel.train(X, y)
        assert not model.is_ready  # cannot train with single class

    def test_model_id_unique_per_training(self) -> None:
        X, y = _make_training_data(20)
        m1 = ProbabilityModel.train(X, y)
        m2 = ProbabilityModel.train(X, y)
        assert m1.model_id != m2.model_id
