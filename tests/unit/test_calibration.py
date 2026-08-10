"""Unit tests for app.models.calibration — Calibrator and metrics."""

from __future__ import annotations

import os

import pytest

from app.models.calibration import CalibrationError, Calibrator, ReliabilityBin

# ── Helpers ──────────────────────────────────────────────────────────────


def _perfect_data() -> tuple[list[int], list[float]]:
    y_true = [0, 0, 1, 1]
    y_pred = [0.1, 0.2, 0.9, 0.8]
    return y_true, y_pred


def _noisy_data(n: int = 100) -> tuple[list[int], list[float]]:
    import random

    rng = random.Random(42)
    y_true = [rng.randint(0, 1) for _ in range(n)]
    y_pred = [rng.random() for _ in range(n)]
    return y_true, y_pred


def _well_calibrated_data(n: int = 200) -> tuple[list[int], list[float]]:
    import random

    rng = random.Random(0)
    y_true: list[int] = []
    y_pred: list[float] = []
    for _ in range(n):
        prob = rng.random()
        label = 1 if rng.random() < prob else 0
        y_true.append(label)
        y_pred.append(prob)
    return y_true, y_pred


# ── Constructor ──────────────────────────────────────────────────────────


class TestConstructor:
    def test_default_method_is_platt(self) -> None:
        cal = Calibrator()
        assert cal.method == "platt"
        assert cal._fitted is False

    def test_isotonic_method(self) -> None:
        cal = Calibrator(method="isotonic")
        assert cal.method == "isotonic"

    def test_unknown_method_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown calibration method"):
            Calibrator(method="invalid")


# ── Fit & calibrate ─────────────────────────────────────────────────────


class TestPlattCalibration:
    def test_fit_and_calibrate_valid_range(self) -> None:
        y_true, y_pred = _perfect_data()
        cal = Calibrator(method="platt")
        cal.fit(y_true, y_pred)
        calibrated = cal.calibrate(y_pred)
        assert len(calibrated) == len(y_pred)
        for p in calibrated:
            assert 0.0 <= p <= 1.0

    def test_calibrate_after_fit_changes_values(self) -> None:
        y_true, y_pred = _perfect_data()
        cal = Calibrator(method="platt")
        cal.fit(y_true, y_pred)
        calibrated = cal.calibrate(y_pred)
        assert calibrated != y_pred


class TestIsotonicCalibration:
    def test_fit_and_calibrate_valid_range(self) -> None:
        y_true, y_pred = _perfect_data()
        cal = Calibrator(method="isotonic")
        cal.fit(y_true, y_pred)
        calibrated = cal.calibrate(y_pred)
        assert len(calibrated) == len(y_pred)
        for p in calibrated:
            assert 0.0 <= p <= 1.0

    def test_calibrate_after_fit_changes_values(self) -> None:
        y_true, y_pred = _perfect_data()
        cal = Calibrator(method="isotonic")
        cal.fit(y_true, y_pred)
        calibrated = cal.calibrate(y_pred)
        assert calibrated != y_pred

    def test_calibrate_before_fit_returns_input(self) -> None:
        cal = Calibrator(method="isotonic")
        raw = [0.1, 0.5, 0.9]
        assert cal.calibrate(raw) == raw


class TestCalibrateOne:
    def test_calibrate_one_platt(self) -> None:
        y_true, y_pred = _perfect_data()
        cal = Calibrator(method="platt")
        cal.fit(y_true, y_pred)
        result = cal.calibrate_one(0.7)
        assert isinstance(result, float)
        assert 0.0 <= result <= 1.0

    def test_calibrate_one_isotonic(self) -> None:
        y_true, y_pred = _perfect_data()
        cal = Calibrator(method="isotonic")
        cal.fit(y_true, y_pred)
        result = cal.calibrate_one(0.7)
        assert isinstance(result, float)
        assert 0.0 <= result <= 1.0


# ── Error handling ───────────────────────────────────────────────────────


class TestErrorHandling:
    def test_length_mismatch_fit(self) -> None:
        cal = Calibrator()
        with pytest.raises(CalibrationError, match="same length"):
            cal.fit([0, 1], [0.5])

    def test_fewer_than_two_samples(self) -> None:
        cal = Calibrator()
        with pytest.raises(CalibrationError, match="at least 2 samples"):
            cal.fit([0], [0.5])

    def test_empty_input_brier_score(self) -> None:
        with pytest.raises(CalibrationError):
            Calibrator.brier_score([], [])

    def test_length_mismatch_brier_score(self) -> None:
        with pytest.raises(CalibrationError):
            Calibrator.brier_score([0, 1], [0.5])

    def test_empty_input_log_loss(self) -> None:
        with pytest.raises(CalibrationError):
            Calibrator.log_loss([], [])

    def test_length_mismatch_log_loss(self) -> None:
        with pytest.raises(CalibrationError):
            Calibrator.log_loss([0, 1], [0.5])

    def test_empty_input_ece(self) -> None:
        with pytest.raises(CalibrationError):
            Calibrator.expected_calibration_error([], [])

    def test_length_mismatch_ece(self) -> None:
        with pytest.raises(CalibrationError):
            Calibrator.expected_calibration_error([0, 1], [0.5])

    def test_length_mismatch_reliability(self) -> None:
        with pytest.raises(CalibrationError):
            Calibrator.reliability_analysis([0, 1], [0.5])


# ── Brier score ──────────────────────────────────────────────────────────


class TestBrierScore:
    def test_perfect_predictions(self) -> None:
        y_true = [0, 0, 1, 1]
        y_pred = [0.0, 0.0, 1.0, 1.0]
        assert Calibrator.brier_score(y_true, y_pred) == pytest.approx(0.0)

    def test_all_wrong(self) -> None:
        y_true = [0, 0, 1, 1]
        y_pred = [1.0, 1.0, 0.0, 0.0]
        score = Calibrator.brier_score(y_true, y_pred)
        assert score == pytest.approx(1.0)

    def test_range(self) -> None:
        y_true, y_pred = _noisy_data()
        score = Calibrator.brier_score(y_true, y_pred)
        assert 0.0 <= score <= 1.0


# ── Log loss ─────────────────────────────────────────────────────────────


class TestLogLoss:
    def test_perfect_predictions(self) -> None:
        y_true = [0, 0, 1, 1]
        y_pred = [0.0, 0.0, 1.0, 1.0]
        score = Calibrator.log_loss(y_true, y_pred)
        assert score == pytest.approx(0.0, abs=1e-10)

    def test_worse_than_random(self) -> None:
        y_true = [0, 0, 1, 1]
        y_pred = [1.0, 1.0, 0.0, 0.0]
        score = Calibrator.log_loss(y_true, y_pred)
        assert score > 10.0

    def test_reasonable_range(self) -> None:
        y_true, y_pred = _noisy_data()
        score = Calibrator.log_loss(y_true, y_pred)
        assert score > 0.0


# ── ECE ──────────────────────────────────────────────────────────────────


class TestExpectedCalibrationError:
    def test_well_calibrated_low_ece(self) -> None:
        y_true, y_pred = _well_calibrated_data()
        ece = Calibrator.expected_calibration_error(y_true, y_pred)
        assert ece < 0.1

    def test_perfect_predictions_zero_ece(self) -> None:
        y_true = [0, 0, 1, 1]
        y_pred = [0.0, 0.0, 1.0, 1.0]
        ece = Calibrator.expected_calibration_error(y_true, y_pred)
        assert ece == pytest.approx(0.0)

    def test_custom_n_bins(self) -> None:
        y_true, y_pred = _well_calibrated_data()
        ece = Calibrator.expected_calibration_error(y_true, y_pred, n_bins=5)
        assert ece < 0.15


# ── Reliability analysis ─────────────────────────────────────────────────


class TestReliabilityAnalysis:
    def test_correct_number_of_bins(self) -> None:
        y_true, y_pred = _noisy_data()
        bins = Calibrator.reliability_analysis(y_true, y_pred, n_bins=10)
        assert len(bins) == 10

    def test_bins_are_reliability_bin_instances(self) -> None:
        y_true, y_pred = _noisy_data()
        bins = Calibrator.reliability_analysis(y_true, y_pred, n_bins=5)
        for b in bins:
            assert isinstance(b, ReliabilityBin)

    def test_counts_sum_to_total(self) -> None:
        y_true, y_pred = _noisy_data()
        bins = Calibrator.reliability_analysis(y_true, y_pred, n_bins=10)
        total = sum(b.count for b in bins)
        assert total == len(y_true)

    def test_bin_centers_in_range(self) -> None:
        y_true, y_pred = _noisy_data()
        bins = Calibrator.reliability_analysis(y_true, y_pred, n_bins=10)
        for b in bins:
            assert 0.0 <= b.bin_center <= 1.0

    def test_single_bin(self) -> None:
        y_true, y_pred = _perfect_data()
        bins = Calibrator.reliability_analysis(y_true, y_pred, n_bins=1)
        assert len(bins) == 1
        assert bins[0].count == len(y_true)


# ── Serialization ────────────────────────────────────────────────────────


class TestSerialization:
    def _make_path(self, tmp_path: object, name: str = "calibrator.pkl") -> str:
        return os.path.join(str(tmp_path), name)

    def test_save_and_load_roundtrip_platt(self, tmp_path: object) -> None:
        path = self._make_path(tmp_path)
        y_true, y_pred = _perfect_data()
        cal = Calibrator(method="platt")
        cal.fit(y_true, y_pred)
        cal.save(path)
        loaded = Calibrator.load(path)
        assert loaded.method == "platt"
        original = cal.calibrate(y_pred)
        restored = loaded.calibrate(y_pred)
        assert original == pytest.approx(restored)

    def test_save_and_load_roundtrip_isotonic(self, tmp_path: object) -> None:
        path = self._make_path(tmp_path)
        y_true, y_pred = _perfect_data()
        cal = Calibrator(method="isotonic")
        cal.fit(y_true, y_pred)
        cal.save(path)
        loaded = Calibrator.load(path)
        assert loaded.method == "isotonic"
        original = cal.calibrate(y_pred)
        restored = loaded.calibrate(y_pred)
        assert original == pytest.approx(restored)

    def test_save_and_load_preserves_version(self, tmp_path: object) -> None:
        path = self._make_path(tmp_path)
        y_true, y_pred = _perfect_data()
        cal = Calibrator(method="platt")
        cal.fit(y_true, y_pred)
        cal.save(path)
        loaded = Calibrator.load(path)
        assert loaded.version == cal.version

    def test_save_and_load_fitted_state(self, tmp_path: object) -> None:
        path = self._make_path(tmp_path)
        y_true, y_pred = _perfect_data()
        cal = Calibrator(method="platt")
        cal.fit(y_true, y_pred)
        cal.save(path)
        loaded = Calibrator.load(path)
        assert loaded._fitted is True
        assert loaded._calibrator is not None
