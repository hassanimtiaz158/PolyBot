"""Tests for the data normalizer (API response → internal schema)."""

from __future__ import annotations

from typing import Any

import pytest

from app.data.normalizer import DataNormalizer
from app.storage.models import Market, MarketSnapshot


class TestNormalizeMarket:
    def test_full_gamma_response(self) -> None:
        raw: dict[str, Any] = {
            "id": "703257",
            "question": "Will the US confirm that aliens exist before 2027?",
            "conditionId": "0xabc123",
            "status": "active",
            "endDate": "2026-12-31T23:59:59Z",
            "liquidityNum": 50000.0,
            "startDate": "2025-01-01T00:00:00Z",
        }
        result = DataNormalizer.normalize_market(raw)
        assert result["market_id"] == "703257"
        assert result["question"] == "Will the US confirm that aliens exist before 2027?"
        assert result["condition_id"] == "0xabc123"
        assert result["status"] == "active"
        assert result["resolution_time"] == "2026-12-31T23:59:59Z"
        assert result["liquidity"] == 50000.0
        assert result["created_at"] == "2025-01-01T00:00:00Z"

    def test_minimal_gamma_response(self) -> None:
        raw: dict[str, Any] = {
            "id": "42",
            "question": "Test?",
            "conditionId": "0xdef456",
        }
        result = DataNormalizer.normalize_market(raw)
        assert result["market_id"] == "42"
        assert result["question"] == "Test?"
        assert result["condition_id"] == "0xdef456"
        assert result["status"] is None
        assert result["liquidity"] is None

    def test_missing_id_defaults_to_empty_string(self) -> None:
        result = DataNormalizer.normalize_market({"question": "Q?"})
        assert result["market_id"] == ""
        assert result["question"] == "Q?"

    def test_none_question_becomes_empty_string(self) -> None:
        result = DataNormalizer.normalize_market({"id": "1", "question": None, "conditionId": "c"})
        assert result["question"] == ""

    def test_liquidity_via_num_field(self) -> None:
        raw: dict[str, Any] = {"id": "1", "question": "Q?", "conditionId": "c", "liquidity": 1000.0}
        result = DataNormalizer.normalize_market(raw)
        assert result["liquidity"] == 1000.0

    def test_non_numeric_liquidity(self) -> None:
        raw: dict[str, Any] = {"id": "1", "question": "Q?", "conditionId": "c", "liquidity": "lots"}
        result = DataNormalizer.normalize_market(raw)
        assert result["liquidity"] is None


class TestNormalizeMarkets:
    def test_batch_normalize(self) -> None:
        raw_list: list[dict[str, Any]] = [
            {"id": "1", "question": "Q1?", "conditionId": "c1"},
            {"id": "2", "question": "Q2?", "conditionId": "c2"},
        ]
        results = DataNormalizer.normalize_markets(raw_list)
        assert len(results) == 2
        assert results[0]["market_id"] == "1"
        assert results[1]["market_id"] == "2"

    def test_empty_list(self) -> None:
        assert DataNormalizer.normalize_markets([]) == []


class TestNormalizeSnapshot:
    def test_full_clob_response(self) -> None:
        raw: dict[str, Any] = {
            "market": "0xcond",
            "asset_id": "0xtoken",
            "timestamp": "1712345678",
            "hash": "0xhash",
            "bids": [{"price": "0.45", "size": "100"}, {"price": "0.44", "size": "200"}],
            "asks": [{"price": "0.46", "size": "150"}, {"price": "0.47", "size": "250"}],
            "min_order_size": "1",
            "tick_size": "0.01",
            "neg_risk": False,
            "last_trade_price": "0.45",
        }
        result = DataNormalizer.normalize_snapshot("mkt_001", raw)
        assert result["market_id"] == "mkt_001"
        assert result["bid"] == 0.45
        assert result["ask"] == 0.46
        assert result["midpoint"] == pytest.approx(0.455)
        assert result["spread"] == pytest.approx(0.01)
        assert result["bid_depth"] == 300.0
        assert result["ask_depth"] == 400.0
        assert result["timestamp"] is not None
        assert "T" in (result["timestamp"] or "")

    def test_empty_order_book(self) -> None:
        raw: dict[str, Any] = {
            "market": "0xcond",
            "asset_id": "0xtoken",
            "timestamp": "1712345678",
            "bids": [],
            "asks": [],
        }
        result = DataNormalizer.normalize_snapshot("mkt_002", raw)
        assert result["bid"] is None
        assert result["ask"] is None
        assert result["midpoint"] is None
        assert result["spread"] is None
        assert result["bid_depth"] is None
        assert result["ask_depth"] is None

    def test_missing_price_fields(self) -> None:
        raw: dict[str, Any] = {
            "market": "0xcond",
            "asset_id": "0xtoken",
            "timestamp": "1712345678",
            "bids": [{"size": "100"}],
            "asks": [{"price": "0.55", "size": "150"}],
        }
        result = DataNormalizer.normalize_snapshot("mkt_003", raw)
        assert result["bid"] is None
        assert result["ask"] == 0.55

    def test_string_prices(self) -> None:
        raw: dict[str, Any] = {
            "market": "0xcond",
            "asset_id": "0xtoken",
            "timestamp": "1712345678",
            "bids": [{"price": "0.300", "size": "50"}],
            "asks": [{"price": "0.700", "size": "60"}],
        }
        result = DataNormalizer.normalize_snapshot("mkt_004", raw)
        assert result["bid"] == 0.30
        assert result["ask"] == 0.70
        assert result["midpoint"] == 0.50

    def test_timestamp_iso_format(self) -> None:
        raw: dict[str, Any] = {
            "market": "0xcond",
            "asset_id": "0xtoken",
            "timestamp": "2026-07-29T12:00:00Z",
            "bids": [{"price": "0.40", "size": "10"}],
            "asks": [{"price": "0.60", "size": "10"}],
        }
        result = DataNormalizer.normalize_snapshot("mkt_005", raw)
        assert result["timestamp"] == "2026-07-29T12:00:00+00:00"

    def test_missing_timestamp(self) -> None:
        raw: dict[str, Any] = {
            "market": "0xcond",
            "asset_id": "0xtoken",
            "bids": [{"price": "0.40", "size": "10"}],
            "asks": [{"price": "0.60", "size": "10"}],
        }
        result = DataNormalizer.normalize_snapshot("mkt_006", raw)
        assert result["timestamp"] is None


class TestExtractTokenIds:
    def test_json_string(self) -> None:
        raw: dict[str, Any] = {
            "clobTokenIds": '["0xabc", "0xdef"]',
        }
        ids = DataNormalizer.extract_token_ids(raw)
        assert ids == ["0xabc", "0xdef"]

    def test_list(self) -> None:
        raw: dict[str, Any] = {
            "clobTokenIds": ["0xabc", "0xdef"],
        }
        ids = DataNormalizer.extract_token_ids(raw)
        assert ids == ["0xabc", "0xdef"]

    def test_missing_field(self) -> None:
        ids = DataNormalizer.extract_token_ids({})
        assert ids == []

    def test_malformed_json(self) -> None:
        raw: dict[str, Any] = {"clobTokenIds": "not-json"}
        ids = DataNormalizer.extract_token_ids(raw)
        assert ids == []


class TestToModel:
    def test_to_market_model(self) -> None:
        data: dict[str, Any] = {
            "market_id": "mkt_001",
            "question": "Will X happen?",
            "condition_id": "cond_001",
        }
        model = DataNormalizer.to_market_model(data)
        assert isinstance(model, Market)
        assert model.market_id == "mkt_001"
        assert model.question == "Will X happen?"

    def test_to_snapshot_model(self) -> None:
        data: dict[str, Any] = {
            "market_id": "mkt_001",
            "bid": 0.45,
            "ask": 0.55,
            "timestamp": "2026-01-01T00:00:00+00:00",
        }
        model = DataNormalizer.to_snapshot_model(data)
        assert isinstance(model, MarketSnapshot)
        assert model.market_id == "mkt_001"
        assert model.bid == 0.45
        assert model.ask == 0.55
