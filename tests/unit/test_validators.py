"""Tests for the data-quality validation layer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.data.validators import DataQuality, DataValidator, QualityReport


class TestDataQualityEnum:
    def test_ordering(self) -> None:
        assert DataQuality.HEALTHY < DataQuality.STALE
        assert DataQuality.STALE < DataQuality.INVALID
        assert DataQuality.INVALID < DataQuality.DISCONNECTED
        assert DataQuality.DISCONNECTED < DataQuality.UNKNOWN

    def test_equality(self) -> None:
        assert DataQuality.HEALTHY == DataQuality.HEALTHY
        assert DataQuality("HEALTHY") == DataQuality.HEALTHY

    def test_comparator_methods(self) -> None:
        assert DataQuality.HEALTHY <= DataQuality.HEALTHY
        assert DataQuality.HEALTHY <= DataQuality.STALE
        assert DataQuality.STALE >= DataQuality.HEALTHY
        assert DataQuality.STALE >= DataQuality.STALE
        assert DataQuality.INVALID > DataQuality.STALE


class TestQualityReport:
    def test_defaults(self) -> None:
        report = QualityReport(quality=DataQuality.HEALTHY)
        assert report.quality == DataQuality.HEALTHY
        assert report.reason is None
        assert report.details is None

    def test_full_construction(self) -> None:
        report = QualityReport(
            quality=DataQuality.INVALID,
            reason="Missing fields",
            details={"missing": ["bid"]},
        )
        assert report.quality == DataQuality.INVALID
        assert report.reason == "Missing fields"
        assert report.details == {"missing": ["bid"]}


class TestDataValidatorSnapshot:
    """Tests for ``DataValidator.check_snapshot``."""

    def make_valid_snapshot(self) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        return {
            "market_id": "mkt_001",
            "bid": 0.45,
            "ask": 0.55,
            "midpoint": 0.50,
            "spread": 0.10,
            "bid_depth": 10000.0,
            "ask_depth": 8000.0,
            "volume": 50000.0,
            "timestamp": now,
        }

    def test_normal_event(self) -> None:
        validator = DataValidator(max_age_seconds=300)
        report = validator.check_snapshot(self.make_valid_snapshot())
        assert report.quality == DataQuality.HEALTHY

    def test_missing_market_id(self) -> None:
        data = self.make_valid_snapshot()
        del data["market_id"]
        report = DataValidator().check_snapshot(data)
        assert report.quality == DataQuality.INVALID
        assert "market_id" in (report.reason or "")

    def test_missing_bid(self) -> None:
        data = self.make_valid_snapshot()
        data["bid"] = None
        report = DataValidator().check_snapshot(data)
        assert report.quality == DataQuality.INVALID

    def test_missing_timestamp(self) -> None:
        data = self.make_valid_snapshot()
        data["timestamp"] = None
        report = DataValidator().check_snapshot(data)
        assert report.quality == DataQuality.INVALID
        assert "timestamp" in (report.reason or "")

    def test_stale_timestamp(self) -> None:
        stale_time = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
        data = self.make_valid_snapshot()
        data["timestamp"] = stale_time
        report = DataValidator(max_age_seconds=10).check_snapshot(data)
        assert report.quality == DataQuality.STALE

    def test_invalid_timestamp_format(self) -> None:
        data = self.make_valid_snapshot()
        data["timestamp"] = "not-a-timestamp"
        report = DataValidator().check_snapshot(data)
        assert report.quality == DataQuality.INVALID

    def test_bid_outside_range(self) -> None:
        data = self.make_valid_snapshot()
        data["bid"] = 1.5
        report = DataValidator().check_snapshot(data)
        assert report.quality == DataQuality.INVALID
        assert "bid" in (report.reason or "")

    def test_ask_outside_range(self) -> None:
        data = self.make_valid_snapshot()
        data["ask"] = -0.1
        report = DataValidator().check_snapshot(data)
        assert report.quality == DataQuality.INVALID
        assert "ask" in (report.reason or "")

    def test_negative_spread(self) -> None:
        data = self.make_valid_snapshot()
        data["spread"] = -0.1
        report = DataValidator().check_snapshot(data)
        assert report.quality == DataQuality.INVALID

    def test_non_numeric_bid(self) -> None:
        data = self.make_valid_snapshot()
        data["bid"] = "abc"
        report = DataValidator().check_snapshot(data)
        assert report.quality == DataQuality.INVALID

    def test_empty_dict(self) -> None:
        report = DataValidator().check_snapshot({})
        assert report.quality == DataQuality.INVALID

    def test_fresh_timestamp_within_bounds(self) -> None:
        now = datetime.now(UTC).isoformat()
        data = self.make_valid_snapshot()
        data["timestamp"] = now
        report = DataValidator(max_age_seconds=30).check_snapshot(data)
        assert report.quality == DataQuality.HEALTHY


class TestDataValidatorMarket:
    def test_valid_market(self) -> None:
        data = {
            "market_id": "mkt_001",
            "question": "Will X happen?",
            "condition_id": "cond_001",
        }
        report = DataValidator().check_market(data)
        assert report.quality == DataQuality.HEALTHY

    def test_missing_question(self) -> None:
        data = {"market_id": "mkt_001", "condition_id": "cond_001"}
        report = DataValidator().check_market(data)
        assert report.quality == DataQuality.INVALID
        assert "question" in (report.reason or "")

    def test_empty_market_data(self) -> None:
        report = DataValidator().check_market({})
        assert report.quality == DataQuality.INVALID


class TestDataValidatorAssess:
    def test_assess_snapshot_healthy(self) -> None:
        now = datetime.now(UTC).isoformat()
        data = {
            "market_id": "mkt_001",
            "bid": 0.45,
            "ask": 0.55,
            "timestamp": now,
        }
        quality = DataValidator(max_age_seconds=300).assess(data, data_type="snapshot")
        assert quality == DataQuality.HEALTHY

    def test_assess_market_healthy(self) -> None:
        data = {
            "market_id": "mkt_001",
            "question": "Will X happen?",
            "condition_id": "cond_001",
        }
        quality = DataValidator().assess(data, data_type="market")
        assert quality == DataQuality.HEALTHY

    def test_assess_invalid_data(self) -> None:
        quality = DataValidator().assess({}, data_type="snapshot")
        assert quality == DataQuality.INVALID

    def test_assess_stale_data(self) -> None:
        stale = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
        data = {
            "market_id": "mkt_001",
            "bid": 0.45,
            "ask": 0.55,
            "timestamp": stale,
        }
        quality = DataValidator(max_age_seconds=10).assess(data, data_type="snapshot")
        assert quality == DataQuality.STALE


class TestDataValidatorCheckTimestamp:
    def test_none_timestamp(self) -> None:
        report = DataValidator().check_timestamp(None)
        assert report.quality == DataQuality.INVALID

    def test_fresh_timestamp(self) -> None:
        now = datetime.now(UTC).isoformat()
        report = DataValidator(max_age_seconds=30).check_timestamp(now)
        assert report.quality == DataQuality.HEALTHY

    def test_stale_timestamp(self) -> None:
        stale = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
        report = DataValidator(max_age_seconds=10).check_timestamp(stale)
        assert report.quality == DataQuality.STALE

    def test_invalid_format(self) -> None:
        report = DataValidator().check_timestamp("garbage")
        assert report.quality == DataQuality.INVALID
