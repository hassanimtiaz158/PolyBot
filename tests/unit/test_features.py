"""Unit tests for all feature-engineering modules.

Covers every mathematical calculation, edge case, and the feature
validation layer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from math import isclose

import pytest

from app.features.liquidity import LiquidityFeatures
from app.features.market_quality import MarketQuality
from app.features.momentum import MomentumFeatures
from app.features.orderbook import OrderBookFeatures
from app.features.validator import FeatureValidator
from app.features.volatility import VolatilityFeatures
from app.storage.models import MarketSnapshot

# ═══════════════════════════════════════════════════════════════════════
#  Shared helpers
# ═══════════════════════════════════════════════════════════════════════

NOW = datetime.now(UTC)
FRESH_TS = NOW.isoformat()
STALE_TS = (NOW - timedelta(seconds=60)).isoformat()
FUTURE_TS = (NOW + timedelta(hours=1)).isoformat()


def make_snapshot(
    bid: float | None = 0.45,
    ask: float | None = 0.55,
    bid_depth: float | None = 10_000.0,
    ask_depth: float | None = 8_000.0,
    midpoint: float | None = None,
    spread: float | None = None,
    volume: float | None = 50_000.0,
    timestamp: str | None = None,
) -> MarketSnapshot:
    if midpoint is None and bid is not None and ask is not None:
        midpoint = (bid + ask) / 2.0
    if spread is None and bid is not None and ask is not None:
        spread = ask - bid
    return MarketSnapshot(
        market_id="mkt_001",
        bid=bid,
        ask=ask,
        midpoint=midpoint,
        spread=spread,
        bid_depth=bid_depth,
        ask_depth=ask_depth,
        volume=volume,
        timestamp=timestamp or datetime.now(UTC).isoformat(),
    )


# ═══════════════════════════════════════════════════════════════════════
#  OrderBookFeatures
# ═══════════════════════════════════════════════════════════════════════


class TestOrderBookFeaturesMidpoint:
    def test_normal(self) -> None:
        assert OrderBookFeatures.midpoint(0.45, 0.55) == 0.50

    def test_none_bid(self) -> None:
        assert OrderBookFeatures.midpoint(None, 0.55) is None

    def test_none_ask(self) -> None:
        assert OrderBookFeatures.midpoint(0.45, None) is None

    def test_both_none(self) -> None:
        assert OrderBookFeatures.midpoint(None, None) is None

    def test_zero_values(self) -> None:
        assert OrderBookFeatures.midpoint(0.0, 0.0) == 0.0


class TestOrderBookFeaturesAbsoluteSpread:
    def test_normal(self) -> None:
        assert OrderBookFeatures.absolute_spread(0.45, 0.55) == pytest.approx(0.10)

    def test_zero_spread(self) -> None:
        assert OrderBookFeatures.absolute_spread(0.50, 0.50) == 0.0

    def test_none_bid(self) -> None:
        assert OrderBookFeatures.absolute_spread(None, 0.55) is None

    def test_none_ask(self) -> None:
        assert OrderBookFeatures.absolute_spread(0.45, None) is None


class TestOrderBookFeaturesRelativeSpread:
    def test_normal(self) -> None:
        rs = OrderBookFeatures.relative_spread(0.45, 0.55)
        assert rs is not None
        assert isclose(rs, 0.2, abs_tol=1e-9)

    def test_narrow_spread(self) -> None:
        rs = OrderBookFeatures.relative_spread(0.498, 0.502)
        assert rs is not None
        assert isclose(rs, 0.008, abs_tol=1e-3)

    def test_none_bid(self) -> None:
        assert OrderBookFeatures.relative_spread(None, 0.55) is None

    def test_zero_midpoint(self) -> None:
        assert OrderBookFeatures.relative_spread(0.0, 0.0) is None


class TestOrderBookFeaturesOBI:
    def test_positive(self) -> None:
        obi = OrderBookFeatures.order_book_imbalance(10_000, 8_000)
        assert obi is not None
        assert isclose(obi, 0.1111, abs_tol=0.001)

    def test_negative(self) -> None:
        obi = OrderBookFeatures.order_book_imbalance(8_000, 10_000)
        assert obi is not None
        assert isclose(obi, -0.1111, abs_tol=0.001)

    def test_zero_depth(self) -> None:
        assert OrderBookFeatures.order_book_imbalance(0.0, 0.0) == 0.0

    def test_none_bid_depth(self) -> None:
        assert OrderBookFeatures.order_book_imbalance(None, 8_000) is None

    def test_none_ask_depth(self) -> None:
        assert OrderBookFeatures.order_book_imbalance(10_000, None) is None

    def test_imbalanced_heavy_bids(self) -> None:
        obi = OrderBookFeatures.order_book_imbalance(100_000, 1_000)
        assert obi is not None
        assert isclose(obi, 0.9802, abs_tol=0.001)

    def test_one_sided_book_bids_only(self) -> None:
        """One-sided book where ask_depth is 0."""
        obi = OrderBookFeatures.order_book_imbalance(10_000, 0.0)
        assert obi is not None
        assert obi == 1.0

    def test_one_sided_book_asks_only(self) -> None:
        obi = OrderBookFeatures.order_book_imbalance(0.0, 10_000)
        assert obi is not None
        assert obi == -1.0


class TestOrderBookFeaturesTopNDepth:
    def test_normal(self) -> None:
        bids = [{"price": "0.45", "size": "100"}, {"price": "0.44", "size": "200"}]
        asks = [{"price": "0.46", "size": "150"}, {"price": "0.47", "size": "250"}]
        result = OrderBookFeatures.top_n_depth(bids, asks, n=2)
        assert result["top_n_bid_depth"] == 300.0
        assert result["top_n_ask_depth"] == 400.0

    def test_fewer_levels_than_n(self) -> None:
        bids = [{"price": "0.45", "size": "100"}]
        result = OrderBookFeatures.top_n_depth(bids, [], n=5)
        assert result["top_n_bid_depth"] == 100.0
        assert result["top_n_ask_depth"] == 0.0

    def test_none_levels(self) -> None:
        result = OrderBookFeatures.top_n_depth(None, None)
        assert result["top_n_bid_depth"] is None
        assert result["top_n_ask_depth"] is None

    def test_missing_size_field(self) -> None:
        bids = [{"price": "0.45"}, {"price": "0.44", "size": "200"}]
        result = OrderBookFeatures.top_n_depth(bids, [], n=5)
        assert result["top_n_bid_depth"] == 200.0

    def test_non_numeric_size(self) -> None:
        bids = [{"price": "0.45", "size": "abc"}, {"price": "0.44", "size": "200"}]
        result = OrderBookFeatures.top_n_depth(bids, [], n=5)
        assert result["top_n_bid_depth"] == 200.0

    def test_empty_lists(self) -> None:
        result = OrderBookFeatures.top_n_depth([], [])
        assert result["top_n_bid_depth"] == 0.0
        assert result["top_n_ask_depth"] == 0.0


class TestOrderBookFeaturesDataFreshness:
    def test_fresh_timestamp(self) -> None:
        assert OrderBookFeatures.data_freshness(FRESH_TS, max_age_seconds=30) is True

    def test_stale_timestamp(self) -> None:
        assert OrderBookFeatures.data_freshness(STALE_TS, max_age_seconds=10) is False

    def test_none_timestamp(self) -> None:
        assert OrderBookFeatures.data_freshness(None) is False

    def test_future_timestamp(self) -> None:
        assert OrderBookFeatures.data_freshness(FUTURE_TS, max_age_seconds=30) is True


class TestOrderBookFeaturesCompute:
    def test_full_snapshot(self) -> None:
        snap = make_snapshot()
        result = OrderBookFeatures().compute(snap)
        assert isclose(result["midpoint"], 0.50, abs_tol=1e-9)
        assert isclose(result["absolute_spread"], 0.10, abs_tol=1e-9)
        assert isclose(result["relative_spread"], 0.20, abs_tol=1e-9)
        assert result["bid_depth"] == 10_000.0
        assert result["ask_depth"] == 8_000.0
        assert isclose(result["obi"], 0.1111, abs_tol=0.001)
        assert result["data_fresh"] is True
        assert result["timestamp"] is not None

    def test_empty_order_book(self) -> None:
        snap = make_snapshot(bid=None, ask=None, bid_depth=None, ask_depth=None)
        result = OrderBookFeatures().compute(snap)
        assert result["midpoint"] is None
        assert result["absolute_spread"] is None
        assert result["relative_spread"] is None
        assert result["obi"] is None
        assert result["data_fresh"] is True  # timestamp is fresh

    def test_zero_depth(self) -> None:
        snap = make_snapshot(bid_depth=0.0, ask_depth=0.0)
        result = OrderBookFeatures().compute(snap)
        assert result["obi"] == 0.0

    def test_one_sided_book(self) -> None:
        snap = make_snapshot(bid=0.45, ask=None, bid_depth=10_000.0, ask_depth=0.0)
        result = OrderBookFeatures().compute(snap)
        assert result["midpoint"] is None
        assert result["absolute_spread"] is None
        assert result["relative_spread"] is None
        assert result["obi"] == 1.0  # bid depth only

    def test_dict_input(self) -> None:
        d = {
            "market_id": "x",
            "bid": 0.40,
            "ask": 0.60,
            "bid_depth": 5000.0,
            "ask_depth": 5000.0,
            "timestamp": FRESH_TS,
        }
        result = OrderBookFeatures().compute(d)
        assert isclose(result["midpoint"], 0.50)
        assert result["obi"] == 0.0

    def test_raw_bids_asks_used_in_top_n(self) -> None:
        snap = make_snapshot()
        bids = [{"price": "0.45", "size": "100"}]
        asks = [{"price": "0.46", "size": "150"}]
        result = OrderBookFeatures().compute(snap, bids=bids, asks=asks)
        assert result["top_n_bid_depth"] == 100.0
        assert result["top_n_ask_depth"] == 150.0


# ═══════════════════════════════════════════════════════════════════════
#  MomentumFeatures
# ═══════════════════════════════════════════════════════════════════════


class TestMomentumFeatures:
    def test_insufficient_data_returns_none(self) -> None:
        mf = MomentumFeatures()
        assert mf.return_over_window(5) is None

    def test_single_observation_returns_none(self) -> None:
        mf = MomentumFeatures()
        mf.update(FRESH_TS, 0.50)
        assert mf.return_over_window(5) is None

    def test_return_calculation(self) -> None:
        mf = MomentumFeatures()
        past = (NOW - timedelta(seconds=60)).isoformat()
        mf.update(past, 0.40)
        mf.update(FRESH_TS, 0.50)
        ret = mf.return_over_window(30)
        assert ret is not None
        assert isclose(ret, 0.25, abs_tol=1e-6)

    def test_negative_return(self) -> None:
        mf = MomentumFeatures()
        past = (NOW - timedelta(seconds=60)).isoformat()
        mf.update(past, 0.50)
        mf.update(FRESH_TS, 0.40)
        ret = mf.return_over_window(30)
        assert ret is not None
        assert isclose(ret, -0.20, abs_tol=1e-6)

    def test_return_over_small_window_within_history(self) -> None:
        mf = MomentumFeatures()
        t0 = (NOW - timedelta(seconds=60)).isoformat()
        t1 = (NOW - timedelta(seconds=30)).isoformat()
        t2 = FRESH_TS
        mf.update(t0, 0.40)
        mf.update(t1, 0.45)
        mf.update(t2, 0.50)
        ret_10s = mf.return_over_window(10)
        assert ret_10s is not None
        # Only t1 and t2 are within 10s window — uses earliest within cutoff
        assert 0.0 < ret_10s < 0.5

    def test_velocity(self) -> None:
        mf = MomentumFeatures()
        past = (NOW - timedelta(seconds=10)).isoformat()
        mf.update(past, 0.40)
        mf.update(FRESH_TS, 0.50)
        vel = mf.velocity(10)
        assert vel is not None
        assert isclose(vel, 0.025, abs_tol=1e-6)  # 25% / 10s

    def test_velocity_insufficient_data(self) -> None:
        mf = MomentumFeatures()
        assert mf.velocity(60) is None

    def test_returns_dict_structure(self) -> None:
        mf = MomentumFeatures(windows_seconds=[5, 30])
        snap = make_snapshot()
        result = mf.compute(snap)
        assert "return_5s" in result
        assert "return_30s" in result
        assert "velocity_60s" in result
        assert "timestamp" in result

    def test_all_windows_return_none_with_one_point(self) -> None:
        mf = MomentumFeatures()
        snap = make_snapshot(midpoint=0.50, timestamp=FRESH_TS)
        result = mf.compute(snap)
        for w in mf.windows:
            assert result[f"return_{w}s"] is None
        assert result["velocity_60s"] is None

    def test_two_points_all_windows_have_values(self) -> None:
        mf = MomentumFeatures(windows_seconds=[5, 30])
        past = (NOW - timedelta(seconds=60)).isoformat()
        mf.update(past, 0.40)
        mf.update((NOW - timedelta(seconds=10)).isoformat(), 0.42)
        result = mf.compute(make_snapshot(midpoint=0.44, timestamp=FRESH_TS))
        assert result["return_5s"] is not None
        assert result["return_30s"] is not None

    def test_update_with_invalid_timestamp_does_not_crash(self) -> None:
        mf = MomentumFeatures()
        mf.update("not-a-date", 0.50)  # should be ignored gracefully
        assert mf.return_over_window(5) is None

    def test_observation_order_preserved(self) -> None:
        mf = MomentumFeatures()
        old = (NOW - timedelta(seconds=60)).isoformat()
        recent = (NOW - timedelta(seconds=5)).isoformat()
        mf.update(old, 0.40)
        mf.update(recent, 0.50)
        mf.update(FRESH_TS, 0.45)
        ret = mf.return_over_window(10)
        assert ret is not None

    def test_compute_with_dict(self) -> None:
        mf = MomentumFeatures(windows_seconds=[10])
        d = {"market_id": "x", "midpoint": 0.50, "timestamp": FRESH_TS}
        result = mf.compute(d)
        assert "return_10s" in result


# ═══════════════════════════════════════════════════════════════════════
#  VolatilityFeatures
# ═══════════════════════════════════════════════════════════════════════


class TestVolatilityFeatures:
    def test_insufficient_data_returns_none(self) -> None:
        vf = VolatilityFeatures(window=10)
        assert vf.realised_volatility() is None

    def test_single_observation_returns_none(self) -> None:
        vf = VolatilityFeatures(window=10)
        vf.update(0.50)
        assert vf.realised_volatility() is None

    def test_constant_prices_zero_volatility(self) -> None:
        vf = VolatilityFeatures(window=10)
        for _ in range(10):
            vf.update(0.50)
        vol = vf.realised_volatility()
        assert vol is not None
        assert isclose(vol, 0.0, abs_tol=1e-9)

    def test_increasing_prices_positive_volatility(self) -> None:
        vf = VolatilityFeatures(window=10)
        prices = [0.40, 0.41, 0.42, 0.43, 0.44, 0.45]
        for p in prices:
            vf.update(p)
        vol = vf.realised_volatility()
        assert vol is not None
        assert vol > 0.0

    def test_window_respected(self) -> None:
        vf = VolatilityFeatures(window=3)
        for p in [0.40, 0.50, 0.40, 0.50, 0.40]:
            vf.update(p)
        vol = vf.realised_volatility()
        assert vol is not None
        # deque maxlen=4, so only last 4 prices used
        assert len(vf._prices) <= 4

    def test_compute_updates_and_returns_volatility(self) -> None:
        vf = VolatilityFeatures(window=10)
        snap = make_snapshot(midpoint=0.45)
        result = vf.compute(snap)
        assert result["realised_volatility"] is None  # only 1 observation
        assert result["volatility_window"] == 10
        assert result["volatility_observations"] == 1

        snap2 = make_snapshot(midpoint=0.46)
        result2 = vf.compute(snap2)
        assert result2["realised_volatility"] is not None

    def test_compute_with_dict(self) -> None:
        vf = VolatilityFeatures(window=5)
        d = {"market_id": "x", "midpoint": 0.50, "timestamp": FRESH_TS}
        result = vf.compute(d)
        assert "realised_volatility" in result

    def test_log_returns_handle_zero_prev_price(self) -> None:
        vf = VolatilityFeatures(window=10)
        vf.update(0.0)
        vf.update(0.50)
        # zero price skipped → only one valid price → vol is None
        vol = vf.realised_volatility()
        assert vol is None

    def test_log_returns_skips_zero_then_calculates(self) -> None:
        vf = VolatilityFeatures(window=10)
        vf.update(0.0)
        vf.update(0.40)
        vf.update(0.50)
        # After skipping 0→0.40, only the 0.40→0.50 transition remains
        vol = vf.realised_volatility()
        assert vol is not None
        assert vol >= 0.0

    def test_invalid_window_raises(self) -> None:
        with pytest.raises(ValueError):
            VolatilityFeatures(window=1)


# ═══════════════════════════════════════════════════════════════════════
#  LiquidityFeatures
# ═══════════════════════════════════════════════════════════════════════


class TestLiquidityFeaturesDepthLiquidity:
    def test_normal(self) -> None:
        assert LiquidityFeatures.depth_liquidity(10_000, 8_000) == 18_000

    def test_none_bid_depth(self) -> None:
        assert LiquidityFeatures.depth_liquidity(None, 8_000) is None

    def test_zero_depth(self) -> None:
        assert LiquidityFeatures.depth_liquidity(0.0, 0.0) == 0.0


class TestLiquidityFeaturesLiquidityScore:
    def test_high_liquidity(self) -> None:
        score = LiquidityFeatures.liquidity_score(50_000, 50_000, 100_000, 0.01)
        assert score is not None
        assert 0.8 <= score <= 1.0

    def test_low_liquidity(self) -> None:
        score = LiquidityFeatures.liquidity_score(100, 100, 500, 0.10)
        assert score is not None
        assert score < 0.5

    def test_none_depth(self) -> None:
        assert LiquidityFeatures.liquidity_score(None, None, 0, 0) is None

    def test_zero_volume_and_depth(self) -> None:
        score = LiquidityFeatures.liquidity_score(0, 0, 0, 0.05)
        assert score is not None
        assert score >= 0.0

    def test_extreme_spread_penalty(self) -> None:
        score = LiquidityFeatures.liquidity_score(50_000, 50_000, 100_000, 0.50)
        assert score is not None
        assert score == pytest.approx(0.70)  # depth+volume contribute, spread=0

    def test_missing_spread_no_penalty(self) -> None:
        score = LiquidityFeatures.liquidity_score(50_000, 50_000, 100_000, None)
        assert score is not None
        assert score > 0.5


class TestLiquidityFeaturesCompute:
    def test_full_snapshot(self) -> None:
        snap = make_snapshot()
        result = LiquidityFeatures().compute(snap)
        assert result["total_depth"] == 18_000.0
        assert result["volume"] == 50_000.0
        assert result["liquidity_score"] is not None
        assert 0.2 < result["liquidity_score"] < 0.4

    def test_missing_depth(self) -> None:
        snap = make_snapshot(bid_depth=None, ask_depth=None)
        result = LiquidityFeatures().compute(snap)
        assert result["total_depth"] is None
        assert result["liquidity_score"] is None

    def test_dict_input(self) -> None:
        d = {
            "market_id": "x",
            "bid_depth": 10_000.0,
            "ask_depth": 8_000.0,
            "volume": 50_000.0,
            "spread": 0.10,
            "timestamp": FRESH_TS,
        }
        result = LiquidityFeatures().compute(d)
        assert result["total_depth"] == 18_000.0


# ═══════════════════════════════════════════════════════════════════════
#  MarketQuality
# ═══════════════════════════════════════════════════════════════════════


class TestMarketQualityTimeToResolution:
    def test_future_resolution(self) -> None:
        future = (NOW + timedelta(days=7)).isoformat()
        ttr = MarketQuality.time_to_resolution(future)
        assert ttr is not None
        assert ttr > 0

    def test_past_resolution(self) -> None:
        past = (NOW - timedelta(days=1)).isoformat()
        ttr = MarketQuality.time_to_resolution(past)
        assert ttr is not None
        assert ttr == 0.0

    def test_none_resolution(self) -> None:
        assert MarketQuality.time_to_resolution(None) is None

    def test_unparseable(self) -> None:
        assert MarketQuality.time_to_resolution("garbage") is None


class TestMarketQualityDataFreshness:
    def test_fresh(self) -> None:
        assert MarketQuality.data_freshness(FRESH_TS, max_age_seconds=30) is True

    def test_stale(self) -> None:
        assert MarketQuality.data_freshness(STALE_TS, max_age_seconds=10) is False

    def test_none(self) -> None:
        assert MarketQuality.data_freshness(None) is False


class TestMarketQualitySpreadQuality:
    def test_zero_spread(self) -> None:
        assert MarketQuality.spread_quality(0.0) == 1.0

    def test_moderate_spread(self) -> None:
        assert isclose(MarketQuality.spread_quality(0.05), 0.50)

    def test_wide_spread(self) -> None:
        assert MarketQuality.spread_quality(0.15) == 0.0

    def test_none_spread(self) -> None:
        assert MarketQuality.spread_quality(None) == 0.0


class TestMarketQualityCompositeScore:
    def test_perfect_score(self) -> None:
        mq = MarketQuality()
        score = mq.composite_score(
            liquidity_score=1.0,
            spread=0.01,
            volume=100_000.0,
            total_depth=50_000.0,
            data_fresh=True,
        )
        assert isclose(score, 1.0, abs_tol=0.05)

    def test_zero_score(self) -> None:
        mq = MarketQuality()
        score = mq.composite_score(
            liquidity_score=0.0,
            spread=0.50,
            volume=0.0,
            total_depth=0.0,
            data_fresh=False,
        )
        assert score == 0.0

    def test_missing_liquidity_score(self) -> None:
        mq = MarketQuality()
        score = mq.composite_score(
            liquidity_score=None,
            spread=0.05,
            volume=50_000.0,
            total_depth=20_000.0,
            data_fresh=True,
        )
        assert 0.3 < score < 0.9


class TestMarketQualityCompute:
    def test_full_input(self) -> None:
        snap = make_snapshot()
        obf = OrderBookFeatures().compute(snap)
        lf = LiquidityFeatures().compute(snap)
        mq = MarketQuality()
        result = mq.compute(
            snap,
            orderbook_features=obf,
            liquidity_features=lf,
            resolution_time=(NOW + timedelta(days=7)).isoformat(),
        )
        assert "time_to_resolution" in result
        assert "data_fresh" in result
        assert "liquidity_score" in result
        assert "composite_score" in result
        assert result["data_fresh"] is True
        assert result["composite_score"] > 0.3

    def test_no_features_dicts(self) -> None:
        snap = make_snapshot(timestamp=STALE_TS)
        result = MarketQuality().compute(snap)
        assert result["data_fresh"] is False
        assert result["liquidity_score"] is None
        assert result["composite_score"] <= 0.15

    def test_stale_data_score_lower(self) -> None:
        fresh_snap = make_snapshot()  # uses datetime.now(UTC).isoformat()
        stale_snap = make_snapshot(timestamp=STALE_TS)
        mq = MarketQuality()
        fresh_result = mq.compute(fresh_snap)
        stale_result = mq.compute(stale_snap)
        assert stale_result["composite_score"] < fresh_result["composite_score"]

    def test_dict_input(self) -> None:
        d = {
            "market_id": "x",
            "bid": 0.45,
            "ask": 0.55,
            "midpoint": 0.50,
            "spread": 0.10,
            "bid_depth": 10_000.0,
            "ask_depth": 8_000.0,
            "volume": 50_000.0,
            "timestamp": FRESH_TS,
        }
        result = MarketQuality().compute(d)
        assert "composite_score" in result


# ═══════════════════════════════════════════════════════════════════════
#  FeatureValidator
# ═══════════════════════════════════════════════════════════════════════


class TestFeatureValidator:
    def test_valid_features(self) -> None:
        features = {
            "midpoint": 0.50,
            "absolute_spread": 0.10,
            "obi": 0.05,
            "bid_depth": 10_000.0,
            "ask_depth": 8_000.0,
            "liquidity_score": 0.85,
            "composite_score": 0.75,
            "timestamp": FRESH_TS,
        }
        result = FeatureValidator.validate(features, max_age_seconds=30)
        assert len(result["errors"]) == 0

    def test_missing_timestamp(self) -> None:
        result = FeatureValidator.validate({"midpoint": 0.50})
        assert any("no timestamp" in e.lower() for e in result["errors"])

    def test_stale_timestamp(self) -> None:
        features = {"timestamp": STALE_TS, "midpoint": 0.50}
        result = FeatureValidator.validate(features, max_age_seconds=10)
        assert any("old" in e.lower() for e in result["errors"])

    def test_bid_outside_range(self) -> None:
        features = {"bid": 1.50, "timestamp": FRESH_TS}
        result = FeatureValidator.validate(features, max_age_seconds=30)
        assert any("bid" in e for e in result["errors"])

    def test_negative_depth(self) -> None:
        features = {"bid_depth": -100.0, "timestamp": FRESH_TS}
        result = FeatureValidator.validate(features, max_age_seconds=30)
        assert any("negative" in e for e in result["errors"])

    def test_extreme_return_warning(self) -> None:
        features = {"return_5s": 15.0, "timestamp": FRESH_TS}
        result = FeatureValidator.validate(features, max_age_seconds=30)
        assert len(result["errors"]) == 0
        assert any("return" in w and "extreme" in w for w in result["warnings"])

    def test_extreme_velocity_warning(self) -> None:
        features = {"velocity_60s": 0.5, "timestamp": FRESH_TS}
        result = FeatureValidator.validate(features, max_age_seconds=30)
        assert any("velocity" in w for w in result["warnings"])

    def test_negative_volatility_error(self) -> None:
        features = {"realised_volatility": -0.1, "timestamp": FRESH_TS}
        result = FeatureValidator.validate(features, max_age_seconds=30)
        assert any("negative" in e for e in result["errors"])

    def test_is_valid_returns_bool(self) -> None:
        good = {"midpoint": 0.50, "timestamp": FRESH_TS}
        bad = {"midpoint": 1.50, "timestamp": FRESH_TS}
        assert FeatureValidator.is_valid(good, max_age_seconds=30) is True
        assert FeatureValidator.is_valid(bad, max_age_seconds=30) is False

    def test_has_timestamp(self) -> None:
        assert FeatureValidator.has_timestamp({"timestamp": FRESH_TS}) is True
        assert FeatureValidator.has_timestamp({}) is False

    def test_obi_outside_range(self) -> None:
        features = {"obi": 1.5, "timestamp": FRESH_TS}
        result = FeatureValidator.validate(features, max_age_seconds=30)
        assert any("OBI" in e for e in result["errors"])

    def test_spread_outside_range(self) -> None:
        features = {"absolute_spread": 2.0, "timestamp": FRESH_TS}
        result = FeatureValidator.validate(features, max_age_seconds=30)
        assert any("Spread" in e for e in result["errors"])

    def test_non_numeric_value(self) -> None:
        features = {"midpoint": "abc", "timestamp": FRESH_TS}
        result = FeatureValidator.validate(features, max_age_seconds=30)
        assert any("not numeric" in e for e in result["errors"])
