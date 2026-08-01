"""Tests for walk-forward validation."""

from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Any

import pytest

from app.backtesting.models import MarketSnapshot
from app.backtesting.walk_forward import (
    WalkForwardReporter,
    WalkForwardValidator,
    _compute_verdict,
    detect_degradation,
    detect_overfitting,
    detect_regime_sensitivity,
    detect_single_period_luck,
    detect_unstable_parameters,
    split_windows,
)
from app.strategies.base import Signal, Strategy

# ── Helpers ──────────────────────────────────────────────────────────


def snaps(prices: list[float], market_id: str = "btc_up") -> list[MarketSnapshot]:
    """Chronological snapshots with fresh timestamps (risk-gate safe)."""
    base = time.time()
    return [
        MarketSnapshot(
            timestamp=base + float(i * 60),
            market_id=market_id,
            midpoint=p,
            spread=0.02,
            bid=p - 0.01,
            ask=p + 0.01,
            depth=1_000_000.0,
        )
        for i, p in enumerate(prices)
    ]


class ThresholdStrategy(Strategy):
    """Trainable: fits a threshold to the median training midpoint.

    Buys YES when the midpoint is well below the threshold and NO when
    well above — always proposing a 0.06 gross edge (safely above
    ``min_net_edge = 0.05`` even with float rounding).
    """

    name = "threshold"

    def __init__(self) -> None:
        super().__init__(min_confidence=0.0, min_liquidity_score=0.0)
        self._threshold = 0.5

    def fit(self, snapshots: list[MarketSnapshot]) -> None:
        mids = sorted(s.midpoint for s in snapshots)
        self._threshold = mids[len(mids) // 2]

    def params(self) -> dict[str, Any]:
        return {"threshold": round(self._threshold, 6)}

    def generate_signal(
        self,
        features: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> Signal:
        mid = features.get("midpoint", 0.5)
        market_id = features.get("market_id", "unknown")
        if mid <= self._threshold - 0.05:
            return self._candidate(
                market_id, "YES", min(0.95, mid + 0.06), mid,
                0.8, "yes_low", features,
            )
        if mid >= self._threshold + 0.05:
            return self._candidate(
                market_id, "NO", min(0.95, (1.0 - mid) + 0.06),
                1.0 - mid, 0.8, "no_high", features,
            )
        return self._reject(market_id, "mid")


class AlwaysRejectStrategy(Strategy):
    """Produces no candidates — zero trades everywhere."""

    name = "always_reject"

    def __init__(self) -> None:
        super().__init__(min_confidence=0.0, min_liquidity_score=0.0)

    def generate_signal(
        self,
        features: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> Signal:
        return self._reject(features.get("market_id", "unknown"), "no_trade")


def threshold_factory() -> ThresholdStrategy:
    return ThresholdStrategy()


def reject_factory() -> AlwaysRejectStrategy:
    return AlwaysRejectStrategy()


# ── Window splitting ─────────────────────────────────────────────────


class TestSplitWindows:
    def test_expanding(self) -> None:
        data = snaps([0.5] * 12)
        windows = split_windows(data, train_size=3, val_size=2)
        # (0:3,3:5) (0:5,5:7) (0:7,7:9) (0:9,9:11)
        assert len(windows) == 4
        assert [len(t) for t, _ in windows] == [3, 5, 7, 9]
        assert [len(v) for _, v in windows] == [2, 2, 2, 2]

    def test_rolling(self) -> None:
        data = snaps([0.5] * 12)
        windows = split_windows(data, train_size=3, val_size=2, mode="rolling")
        # (0:3,3:5) (2:5,5:7) (4:7,7:9) (6:9,9:11)
        assert len(windows) == 4
        assert [len(t) for t, _ in windows] == [3, 3, 3, 3]

    def test_custom_step(self) -> None:
        data = snaps([0.5] * 12)
        windows = split_windows(data, train_size=4, val_size=2, step=3)
        # (0:4,4:6) (3:7,7:9) (6:10,10:12)
        assert len(windows) == 3

    def test_chronological_no_overlap(self) -> None:
        data = snaps([0.5] * 12)
        for train, val in split_windows(data, 3, 2):
            assert train[-1].timestamp < val[0].timestamp
            for s in val:
                assert s not in train

    def test_no_shuffle_expanding_prefixes(self) -> None:
        data = snaps([0.5] * 12)
        windows = split_windows(data, 3, 2)
        for (train, _), (train_next, _) in zip(windows, windows[1:]):
            assert all(s in train_next for s in train)

    def test_final_period_never_in_train(self) -> None:
        """The last evaluation period must never be used for training."""
        data = snaps([0.5] * 12)
        windows = split_windows(data, 3, 2)
        last_val = windows[-1][1]
        for train, _ in windows:
            for s in last_val:
                assert s not in train

    def test_invalid_args(self) -> None:
        data = snaps([0.5] * 12)
        with pytest.raises(ValueError):
            split_windows(data, train_size=0, val_size=2)
        with pytest.raises(ValueError):
            split_windows(data, train_size=3, val_size=0)
        with pytest.raises(ValueError):
            split_windows(data, train_size=3, val_size=2, mode="bogus")
        with pytest.raises(ValueError):
            split_windows(data, train_size=3, val_size=2, step=0)

    def test_insufficient_data(self) -> None:
        """No complete window can be formed → empty split."""
        assert split_windows(snaps([0.5] * 4), train_size=3, val_size=2) == []


# ── Detectors ────────────────────────────────────────────────────────


class TestDetectors:
    def test_overfitting_positive_is_negative_oos(self) -> None:
        flagged, reason = detect_overfitting([10.0, 8.0], [-2.0, -3.0])
        assert flagged
        assert "IS" in reason

    def test_overfitting_not_flagged_when_oos_ok(self) -> None:
        flagged, _ = detect_overfitting([10.0, 8.0], [2.0, 3.0])
        assert not flagged

    def test_overfitting_empty(self) -> None:
        assert not detect_overfitting([], [1.0])[0]
        assert not detect_overfitting([1.0], [])[0]

    def test_unstable_parameters_constant(self) -> None:
        flagged, keys, drift = detect_unstable_parameters(
            [{"t": 0.5}, {"t": 0.5}]
        )
        assert not flagged
        assert keys == []
        assert drift == {"t": 0.0}

    def test_unstable_parameters_drift(self) -> None:
        flagged, keys, drift = detect_unstable_parameters(
            [{"t": 0.5}, {"t": 2.5}]
        )
        assert flagged
        assert keys == ["t"]
        assert drift["t"] > 0.5

    def test_unstable_ignores_non_numeric(self) -> None:
        flagged, _, _ = detect_unstable_parameters(
            [{"name": "a"}, {"name": "b"}]
        )
        assert not flagged

    def test_regime_sensitivity_high_dispersion(self) -> None:
        flagged, _ = detect_regime_sensitivity([10.0, -8.0, 9.0, -9.0])
        assert flagged

    def test_regime_sensitivity_stable(self) -> None:
        assert not detect_regime_sensitivity([3.0, 2.0, 4.0, 3.0])[0]

    def test_regime_sensitivity_too_few_windows(self) -> None:
        assert not detect_regime_sensitivity([10.0, -10.0])[0]

    def test_degradation_negative_trend(self) -> None:
        flagged, _ = detect_degradation([8.0, 5.0, 2.0, -1.0])
        assert flagged

    def test_degradation_no_trend(self) -> None:
        assert not detect_degradation([1.0, 2.0, 3.0, 4.0])[0]

    def test_degradation_too_few_windows(self) -> None:
        assert not detect_degradation([8.0, -1.0])[0]

    def test_single_period_luck_one_winner(self) -> None:
        flagged, _ = detect_single_period_luck([12.0, -3.0, -4.0])
        assert flagged

    def test_single_period_luck_excluding_best(self) -> None:
        flagged, _ = detect_single_period_luck([10.0, -4.0, -5.0])
        assert flagged

    def test_single_period_luck_all_winners(self) -> None:
        assert not detect_single_period_luck([3.0, 4.0, 5.0])[0]

    def test_single_period_luck_all_losers(self) -> None:
        assert not detect_single_period_luck([-2.0, -3.0])[0]

    def test_verdict_unstable(self) -> None:
        from app.backtesting.walk_forward import WalkForwardDiagnostics

        d = WalkForwardDiagnostics(
            overfitting=False, unstable_parameters=False,
            regime_sensitive=False, degradation=False,
            single_period_luck=True, verdict="",
        )
        assert _compute_verdict(d) == "UNSTABLE"

    def test_verdict_suspect_regime(self) -> None:
        from app.backtesting.walk_forward import WalkForwardDiagnostics

        d = WalkForwardDiagnostics(
            overfitting=False, unstable_parameters=False,
            regime_sensitive=True, degradation=False,
            single_period_luck=False, verdict="",
        )
        assert _compute_verdict(d) == "SUSPECT"

    def test_verdict_stable(self) -> None:
        from app.backtesting.walk_forward import WalkForwardDiagnostics

        d = WalkForwardDiagnostics(
            overfitting=False, unstable_parameters=False,
            regime_sensitive=False, degradation=False,
            single_period_luck=False, verdict="",
        )
        assert _compute_verdict(d) == "STABLE"


# ── Validator integration ────────────────────────────────────────────


class TestWalkForwardValidator:
    @pytest.mark.asyncio
    async def test_runs_all_windows(self) -> None:
        data = snaps([0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5])
        validator = WalkForwardValidator(
            threshold_factory, train_size=3, val_size=2,
        )
        report = await validator.run(data)

        assert validator.strategy_name == "threshold"
        # (0:3,3:5) (0:5,5:7) → 2 windows from 8 snapshots
        assert len(report.windows) == 2
        for w in report.windows:
            assert w.val_count == 2
            assert w.out_of_sample.num_trades >= 0
            assert w.params == {"threshold": 0.5}

    @pytest.mark.asyncio
    async def test_params_fitted_from_train_median(self) -> None:
        data = snaps([0.40, 0.44, 0.48, 0.60, 0.62, 0.64, 0.55, 0.50])
        validator = WalkForwardValidator(
            threshold_factory, train_size=3, val_size=2,
        )
        report = await validator.run(data)

        # Window 0 train = [0.40, 0.44, 0.48] → median 0.44
        assert report.windows[0].params["threshold"] == pytest.approx(0.44)
        # Window 1 train = [0.40, 0.44, 0.48, 0.60, 0.62] → median 0.48
        assert report.windows[1].params["threshold"] == pytest.approx(0.48)

    @pytest.mark.asyncio
    async def test_reproducible(self) -> None:
        data = snaps([0.5, 0.55, 0.45, 0.5, 0.55, 0.45, 0.5, 0.55])
        r1 = await WalkForwardValidator(
            threshold_factory, 3, 2,
        ).run(data)
        r2 = await WalkForwardValidator(
            threshold_factory, 3, 2,
        ).run(data)

        assert r1.data_hash == r2.data_hash
        assert [w.out_of_sample.total_pnl for w in r1.windows] == \
            [w.out_of_sample.total_pnl for w in r2.windows]
        assert [w.params for w in r1.windows] == [w.params for w in r2.windows]

    @pytest.mark.asyncio
    async def test_in_sample_evaluated(self) -> None:
        data = snaps([0.5] * 10)
        report = await WalkForwardValidator(
            threshold_factory, 3, 2, evaluate_in_sample=True,
        ).run(data)
        assert all(w.in_sample is not None for w in report.windows)

    @pytest.mark.asyncio
    async def test_in_sample_disabled(self) -> None:
        data = snaps([0.5] * 10)
        report = await WalkForwardValidator(
            threshold_factory, 3, 2, evaluate_in_sample=False,
        ).run(data)
        assert all(w.in_sample is None for w in report.windows)
        assert not report.diagnostics.overfitting

    @pytest.mark.asyncio
    async def test_zero_trade_windows_flagged(self) -> None:
        data = snaps([0.5] * 10)
        report = await WalkForwardValidator(
            reject_factory, 3, 2,
        ).run(data)
        assert len(report.windows) == 3
        assert report.num_zero_trade_windows == 3
        assert report.num_trades == 0
        assert report.failure_periods == []

    @pytest.mark.asyncio
    async def test_failure_periods_detected(self) -> None:
        # Window with rising midpoint after a NO entry → unrealised loss.
        data = snaps([0.44, 0.46, 0.48, 0.55, 0.58, 0.60, 0.62, 0.64])
        validator = WalkForwardValidator(
            threshold_factory, train_size=3, val_size=2,
        )
        report = await validator.run(data)

        # Window 0: val [0.55, 0.58] — NO position loses as mid rises.
        assert report.windows[0].out_of_sample.total_pnl < 0.0
        assert 0 in report.failure_periods

    @pytest.mark.asyncio
    async def test_empty_snapshots_rejected(self) -> None:
        validator = WalkForwardValidator(threshold_factory, 3, 2)
        with pytest.raises(ValueError):
            await validator.run([])

    @pytest.mark.asyncio
    async def test_insufficient_data_rejected(self) -> None:
        validator = WalkForwardValidator(threshold_factory, 3, 2)
        with pytest.raises(ValueError):
            await validator.run(snaps([0.5] * 4))

    @pytest.mark.asyncio
    async def test_aggregate_metrics_present(self) -> None:
        data = snaps([0.5] * 10)
        report = await WalkForwardValidator(
            threshold_factory, 3, 2,
        ).run(data)
        assert len(report.windows) == 3
        assert report.total_pnl == pytest.approx(
            sum(w.out_of_sample.total_pnl for w in report.windows)
        )
        assert report.total_return == pytest.approx(
            report.total_pnl / 10_000.0
        )
        assert report.expectancy == pytest.approx(
            report.total_pnl / report.num_trades if report.num_trades else 0.0
        )
        assert len(report.equity_curve) > 0
        assert len(report.fills) >= 0

    @pytest.mark.asyncio
    async def test_progress_callback_invoked(self) -> None:
        data = snaps([0.5] * 10)
        validator = WalkForwardValidator(threshold_factory, 3, 2)
        calls: list[tuple[int, int]] = []

        def cb(curr: int, total: int) -> None:
            calls.append((curr, total))

        await validator.run(data, progress_callback=cb)
        assert len(calls) == 3
        assert calls[-1] == (3, 3)

    @pytest.mark.asyncio
    async def test_strategy_returns_fresh_instance_per_window(self) -> None:
        """Each window gets a fresh, independently fitted strategy."""
        seen: list[ThresholdStrategy] = []
        data = snaps([0.4, 0.42, 0.44, 0.46, 0.48, 0.50, 0.52, 0.54])

        def factory_with_ids() -> ThresholdStrategy:
            s = ThresholdStrategy()
            seen.append(s)
            return s

        validator = WalkForwardValidator(factory_with_ids, 3, 2)
        report = await validator.run(data)

        # 1 probe instance (strategy name) + 2 per window (IS + OOS)
        assert len(seen) == 1 + 2 * len(report.windows)
        # All instances are distinct objects
        for i, a in enumerate(seen):
            assert all(a is not b for j, b in enumerate(seen) if j != i)


# ── Reporter ─────────────────────────────────────────────────────────


class TestWalkForwardReporter:
    @pytest.mark.asyncio
    async def test_writes_json_and_csv(self) -> None:
        data = snaps([0.5] * 10)
        report = await WalkForwardValidator(
            threshold_factory, 3, 2,
        ).run(data)

        with tempfile.TemporaryDirectory() as tmp:
            reporter = WalkForwardReporter(output_dir=tmp)
            paths = reporter.generate(report, label="wf_test")
            assert os.path.exists(paths["json"])
            assert os.path.exists(paths["csv"])

            with open(paths["json"]) as f:
                payload = json.load(f)
            assert payload["strategy"] == "threshold"
            assert payload["num_windows"] == 3
            assert payload["verdict"] in ("STABLE", "SUSPECT", "UNSTABLE")
            assert "oos_metrics" in payload
            assert "diagnostics" in payload
            assert len(payload["windows"]) == 3
            assert payload["data_hash"] == report.data_hash

            with open(paths["csv"]) as f:
                lines = f.read().strip().splitlines()
            assert lines[0].startswith("window,train_start")
            assert len(lines) == 4  # header + 3 windows
