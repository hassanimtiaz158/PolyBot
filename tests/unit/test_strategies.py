"""Unit tests for the strategy engine.

Covers MicrostructureStrategy, ArbitrageStrategy, and the Strategy base
class gating logic.  ProbabilityStrategy and EnsembleStrategy are stubs
that always return NO_SIGNAL.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.strategies import (
    ArbitrageStrategy,
    MicrostructureStrategy,
    StrategyDecision,
)
from app.strategies.base import Signal

# ═══════════════════════════════════════════════════════════════════════
#  Shared helpers
# ═══════════════════════════════════════════════════════════════════════


def fresh_ts() -> str:
    return datetime.now(UTC).isoformat()


def stale_ts(seconds: int = 60) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()


def valid_features(**overrides: object) -> dict:
    """Return a feature dict that passes all data-quality gates."""
    base: dict = {
        "market_id": "mkt_001",
        "timestamp": fresh_ts(),
        "midpoint": 0.55,
        "bid": 0.50,
        "ask": 0.60,
        "spread": 0.10,
        "absolute_spread": 0.10,
        "relative_spread": 0.1818,
        "obi": 0.30,
        "bid_depth": 10_000.0,
        "ask_depth": 8_000.0,
        "total_depth": 18_000.0,
        "volume": 50_000.0,
        "liquidity_score": 0.70,
        "top_n_bid_depth": 5_000.0,
        "top_n_ask_depth": 4_000.0,
        "data_fresh": True,
        "velocity_60s": 0.02,
        "realised_volatility": 0.15,
    }
    base.update(overrides)
    return base


def valid_arb_context(**overrides: object) -> dict:
    """Return a context with a single IMPLIES relationship."""
    base: dict = {
        "relationships": [
            {
                "type": "IMPLIES",
                "markets": ["mkt_A", "mkt_B"],
            },
        ],
        "market_data": {
            "mkt_A": {
                "market_id": "mkt_A",
                "midpoint": 0.40,
                "liquidity_score": 0.7,
                "timestamp": fresh_ts(),
                "spread": 0.05,
            },
            "mkt_B": {
                "market_id": "mkt_B",
                "midpoint": 0.60,
                "liquidity_score": 0.7,
                "timestamp": fresh_ts(),
                "spread": 0.05,
            },
        },
    }
    base.update(overrides)
    return base


# ═══════════════════════════════════════════════════════════════════════
#  Base Strategy — gating logic
# ═══════════════════════════════════════════════════════════════════════


class TestStrategyBase:
    """Tests for the strategy base class gating helpers."""

    def test_reject_returns_no_signal(self) -> None:
        strat = MicrostructureStrategy()
        sig = strat._reject("mkt_001", "test rejection")
        assert sig.decision == StrategyDecision.NO_SIGNAL
        assert sig.reason == "test rejection"
        assert sig.market_id == "mkt_001"

    def test_candidate_sets_gross_edge(self) -> None:
        strat = MicrostructureStrategy()
        sig = strat._candidate(
            market_id="mkt_001",
            side="YES",
            model_probability=0.65,
            implied_probability=0.55,
            confidence=0.8,
            reason="test",
        )
        assert sig.decision == StrategyDecision.CANDIDATE
        assert sig.gross_edge == pytest.approx(0.10, abs=1e-6)

    def test_candidate_no_edge_without_probs(self) -> None:
        sig = Signal(
            strategy="test",
            market_id="mkt_001",
            side="YES",
            decision=StrategyDecision.CANDIDATE,
            reason="no probs",
        )
        assert sig.gross_edge is None

    def test_data_quality_missing_timestamp(self) -> None:
        strat = MicrostructureStrategy()
        reason = strat._check_data_quality({"market_id": "mkt_001"})
        assert reason is not None
        assert "Missing timestamp" in reason

    def test_data_quality_stale_timestamp(self) -> None:
        strat = MicrostructureStrategy(max_data_age_seconds=5)
        reason = strat._check_data_quality(
            {"market_id": "mkt_001", "timestamp": stale_ts(60)}
        )
        assert reason is not None
        assert "Stale" in reason

    def test_data_quality_unparseable_timestamp(self) -> None:
        strat = MicrostructureStrategy()
        reason = strat._check_data_quality(
            {"market_id": "mkt_001", "timestamp": "not-a-timestamp"}
        )
        assert reason is not None
        assert "Unparseable" in reason

    def test_data_quality_low_liquidity(self) -> None:
        strat = MicrostructureStrategy(min_liquidity_score=0.5)
        reason = strat._check_data_quality(
            {
                "market_id": "mkt_001",
                "timestamp": fresh_ts(),
                "liquidity_score": 0.2,
            }
        )
        assert reason is not None
        assert "Insufficient liquidity" in reason

    def test_data_quality_sufficient_liquidity(self) -> None:
        strat = MicrostructureStrategy(min_liquidity_score=0.5)
        reason = strat._check_data_quality(
            {
                "market_id": "mkt_001",
                "timestamp": fresh_ts(),
                "liquidity_score": 0.7,
            }
        )
        assert reason is None

    def test_confidence_low(self) -> None:
        strat = MicrostructureStrategy(min_confidence=0.6)
        reason = strat._check_confidence(0.3)
        assert reason is not None
        assert "Low confidence" in reason

    def test_confidence_missing(self) -> None:
        strat = MicrostructureStrategy()
        reason = strat._check_confidence(None)
        assert reason is not None
        assert "Missing confidence" in reason


# ═══════════════════════════════════════════════════════════════════════
#  MicrostructureStrategy
# ═══════════════════════════════════════════════════════════════════════


class TestMicrostructureStrategy:
    """Tests for the microstructure strategy."""

    def test_valid_signal(self) -> None:
        strat = MicrostructureStrategy()
        sig = strat.generate_signal(valid_features(obi=0.30))
        assert sig.decision == StrategyDecision.CANDIDATE
        assert sig.side == "YES"
        assert sig.model_probability is not None
        assert sig.implied_probability == 0.55
        assert sig.gross_edge is not None and sig.gross_edge > 0
        assert sig.confidence is not None and sig.confidence > 0
        assert sig.strategy == "microstructure"

    def test_valid_signal_no_side(self) -> None:
        strat = MicrostructureStrategy()
        sig = strat.generate_signal(
            valid_features(obi=-0.60, spread=0.05, absolute_spread=0.05)
        )
        assert sig.decision == StrategyDecision.CANDIDATE
        assert sig.side == "NO"

    def test_no_signal_small_obi(self) -> None:
        strat = MicrostructureStrategy(min_obi_abs=0.05)
        sig = strat.generate_signal(valid_features(obi=0.01))
        assert sig.decision == StrategyDecision.NO_SIGNAL
        assert "OBI magnitude" in sig.reason

    def test_low_confidence(self) -> None:
        strat = MicrostructureStrategy(min_confidence=0.9)
        sig = strat.generate_signal(valid_features(obi=0.30))
        assert sig.decision == StrategyDecision.NO_SIGNAL
        assert "Low confidence" in sig.reason

    def test_insufficient_liquidity(self) -> None:
        strat = MicrostructureStrategy(min_liquidity_score=0.8)
        sig = strat.generate_signal(valid_features(liquidity_score=0.2))
        assert sig.decision == StrategyDecision.NO_SIGNAL
        assert "Insufficient liquidity" in sig.reason

    def test_stale_data(self) -> None:
        strat = MicrostructureStrategy(max_data_age_seconds=5)
        sig = strat.generate_signal(valid_features(timestamp=stale_ts(60)))
        assert sig.decision == StrategyDecision.NO_SIGNAL
        assert "Stale" in sig.reason

    def test_missing_timestamp(self) -> None:
        strat = MicrostructureStrategy()
        sig = strat.generate_signal(valid_features(timestamp=None))
        assert sig.decision == StrategyDecision.NO_SIGNAL
        assert "Missing timestamp" in sig.reason

    def test_missing_midpoint(self) -> None:
        strat = MicrostructureStrategy()
        sig = strat.generate_signal(valid_features(midpoint=None))
        assert sig.decision == StrategyDecision.NO_SIGNAL
        assert "Missing or zero midpoint" in sig.reason

    def test_missing_obi(self) -> None:
        strat = MicrostructureStrategy()
        sig = strat.generate_signal(valid_features(obi=None))
        assert sig.decision == StrategyDecision.NO_SIGNAL
        assert "Missing order-book imbalance" in sig.reason

    def test_missing_bid(self) -> None:
        strat = MicrostructureStrategy()
        sig = strat.generate_signal(valid_features(bid=None))
        assert sig.decision == StrategyDecision.NO_SIGNAL
        assert "Missing bid or ask" in sig.reason

    def test_missing_ask(self) -> None:
        strat = MicrostructureStrategy()
        sig = strat.generate_signal(valid_features(ask=None))
        assert sig.decision == StrategyDecision.NO_SIGNAL
        assert "Missing bid or ask" in sig.reason

    def test_small_edge_rejected(self) -> None:
        strat = MicrostructureStrategy(min_edge_bps=100.0)
        sig = strat.generate_signal(valid_features(obi=0.05, midpoint=0.505))
        assert sig.decision == StrategyDecision.NO_SIGNAL
        assert "Gross edge too small" in sig.reason

    def test_momentum_aligned_increases_confidence(self) -> None:
        strat = MicrostructureStrategy()
        sig_aligned = strat.generate_signal(
            valid_features(
                obi=0.30, velocity_60s=0.02, spread=0.05,
                absolute_spread=0.05,
            )
        )
        sig_no_mom = strat.generate_signal(
            valid_features(
                obi=0.30, velocity_60s=None, spread=0.05,
                absolute_spread=0.05,
            )
        )
        assert sig_aligned.confidence is not None
        assert sig_no_mom.confidence is not None
        assert sig_aligned.confidence > sig_no_mom.confidence

    def test_signal_contains_feature_snapshot(self) -> None:
        strat = MicrostructureStrategy()
        feats = valid_features(obi=0.30)
        sig = strat.generate_signal(feats)
        assert sig.decision == StrategyDecision.CANDIDATE
        assert sig.feature_snapshot.get("obi") == 0.30

    def test_signal_generates_id(self) -> None:
        strat = MicrostructureStrategy()
        sig = strat.generate_signal(valid_features(obi=0.30))
        assert len(sig.signal_id) > 0

    def test_signal_generates_timestamp(self) -> None:
        strat = MicrostructureStrategy()
        sig = strat.generate_signal(valid_features(obi=0.30))
        assert len(sig.timestamp) > 0

    def test_context_is_optional(self) -> None:
        strat = MicrostructureStrategy()
        sig = strat.generate_signal(valid_features(obi=0.30))
        assert sig.decision == StrategyDecision.CANDIDATE


# ═══════════════════════════════════════════════════════════════════════
#  ArbitrageStrategy
# ═══════════════════════════════════════════════════════════════════════


class TestArbitrageStrategy:
    """Tests for the cross-market arbitrage strategy."""

    def test_implies_no_violation(self) -> None:
        """P(A)=0.40 <= P(B)=0.60  →  no violation."""
        strat = ArbitrageStrategy()
        sig = strat.generate_signal(
            valid_features(market_id="mkt_A"),
            context=valid_arb_context(),
        )
        assert sig.decision == StrategyDecision.NO_SIGNAL

    def test_implies_violation(self) -> None:
        """P(A)=0.90 > P(B)=0.60  →  violation detected."""
        strat = ArbitrageStrategy()
        ctx = valid_arb_context(
            market_data={
                "mkt_A": {
                    "market_id": "mkt_A",
                    "midpoint": 0.90,
                    "liquidity_score": 0.7,
                    "timestamp": fresh_ts(),
                    "spread": 0.05,
                },
                "mkt_B": {
                    "market_id": "mkt_B",
                    "midpoint": 0.60,
                    "liquidity_score": 0.7,
                    "timestamp": fresh_ts(),
                    "spread": 0.05,
                },
            }
        )
        sig = strat.generate_signal(
            valid_features(market_id="mkt_A"),
            context=ctx,
        )
        assert sig.decision == StrategyDecision.CANDIDATE
        assert "IMPLIES" in sig.reason

    def test_mutually_exclusive_violation(self) -> None:
        """P(A)=0.60 + P(B)=0.60 = 1.20 > 1.0  →  violation."""
        strat = ArbitrageStrategy()
        ctx = valid_arb_context(
            relationships=[
                {
                    "type": "MUTUALLY_EXCLUSIVE",
                    "markets": ["mkt_A", "mkt_B"],
                }
            ],
            market_data={
                "mkt_A": {
                    "market_id": "mkt_A",
                    "midpoint": 0.60,
                    "liquidity_score": 0.7,
                    "timestamp": fresh_ts(),
                    "spread": 0.05,
                },
                "mkt_B": {
                    "market_id": "mkt_B",
                    "midpoint": 0.60,
                    "liquidity_score": 0.7,
                    "timestamp": fresh_ts(),
                    "spread": 0.05,
                },
            },
        )
        sig = strat.generate_signal(
            valid_features(market_id="mkt_A"),
            context=ctx,
        )
        assert sig.decision == StrategyDecision.CANDIDATE
        assert "MUTUALLY_EXCLUSIVE" in sig.reason

    def test_complement_violation(self) -> None:
        """P(A)=0.80 + P(B)=0.30 = 1.10 ≠ 1.0  →  violation."""
        strat = ArbitrageStrategy()
        ctx = valid_arb_context(
            relationships=[
                {
                    "type": "COMPLEMENT",
                    "markets": ["mkt_A", "mkt_B"],
                }
            ],
            market_data={
                "mkt_A": {
                    "market_id": "mkt_A",
                    "midpoint": 0.80,
                    "liquidity_score": 0.7,
                    "timestamp": fresh_ts(),
                    "spread": 0.05,
                },
                "mkt_B": {
                    "market_id": "mkt_B",
                    "midpoint": 0.30,
                    "liquidity_score": 0.7,
                    "timestamp": fresh_ts(),
                    "spread": 0.05,
                },
            },
        )
        sig = strat.generate_signal(
            valid_features(market_id="mkt_A"),
            context=ctx,
        )
        assert sig.decision == StrategyDecision.CANDIDATE
        assert "COMPLEMENT" in sig.reason

    def test_sum_constraint_violation(self) -> None:
        """P(A)=0.40 + P(B)=0.50 = 0.90 ≠ 1.0  →  violation."""
        strat = ArbitrageStrategy()
        ctx = valid_arb_context(
            relationships=[
                {
                    "type": "SUM_CONSTRAINT",
                    "markets": ["mkt_A", "mkt_B", "mkt_C"],
                    "params": {"sum_target": 1.0},
                }
            ],
            market_data={
                "mkt_A": {
                    "market_id": "mkt_A",
                    "midpoint": 0.40,
                    "liquidity_score": 0.7,
                    "timestamp": fresh_ts(),
                    "spread": 0.05,
                },
                "mkt_B": {
                    "market_id": "mkt_B",
                    "midpoint": 0.50,
                    "liquidity_score": 0.7,
                    "timestamp": fresh_ts(),
                    "spread": 0.05,
                },
                "mkt_C": {
                    "market_id": "mkt_C",
                    "midpoint": 0.05,
                    "liquidity_score": 0.7,
                    "timestamp": fresh_ts(),
                    "spread": 0.05,
                },
            },
        )
        sig = strat.generate_signal(
            valid_features(market_id="mkt_A"),
            context=ctx,
        )
        assert sig.decision == StrategyDecision.CANDIDATE
        assert "SUM_CONSTRAINT" in sig.reason

    def test_sum_constraint_holds(self) -> None:
        """P(A)=0.50 + P(B)=0.50 = 1.0  →  no violation."""
        strat = ArbitrageStrategy()
        ctx = valid_arb_context(
            relationships=[
                {
                    "type": "SUM_CONSTRAINT",
                    "markets": ["mkt_A", "mkt_B"],
                    "params": {"sum_target": 1.0},
                }
            ],
            market_data={
                "mkt_A": {
                    "market_id": "mkt_A",
                    "midpoint": 0.50,
                    "liquidity_score": 0.7,
                    "timestamp": fresh_ts(),
                    "spread": 0.05,
                },
                "mkt_B": {
                    "market_id": "mkt_B",
                    "midpoint": 0.50,
                    "liquidity_score": 0.7,
                    "timestamp": fresh_ts(),
                    "spread": 0.05,
                },
            },
        )
        sig = strat.generate_signal(
            valid_features(market_id="mkt_A"),
            context=ctx,
        )
        assert sig.decision == StrategyDecision.NO_SIGNAL

    def test_market_not_in_relationship(self) -> None:
        """Market not in any relationship → no signal."""
        strat = ArbitrageStrategy()
        sig = strat.generate_signal(
            valid_features(market_id="mkt_Z"),
            context=valid_arb_context(),
        )
        assert sig.decision == StrategyDecision.NO_SIGNAL

    def test_no_context(self) -> None:
        strat = ArbitrageStrategy()
        sig = strat.generate_signal(valid_features(), context=None)
        assert sig.decision == StrategyDecision.NO_SIGNAL
        assert "No context" in sig.reason

    def test_empty_relationships(self) -> None:
        strat = ArbitrageStrategy()
        sig = strat.generate_signal(
            valid_features(),
            context={"relationships": [], "market_data": {"mkt_001": {}}},
        )
        assert sig.decision == StrategyDecision.NO_SIGNAL
        assert "No relationships" in sig.reason

    def test_empty_market_data(self) -> None:
        strat = ArbitrageStrategy()
        sig = strat.generate_signal(
            valid_features(),
            context={
                "relationships": [{"type": "IMPLIES", "markets": ["a", "b"]}],
                "market_data": {},
            },
        )
        assert sig.decision == StrategyDecision.NO_SIGNAL
        assert "No market_data" in sig.reason

    def test_contradictory_relationship_detected(self) -> None:
        """P(A)=0.99 > P(B)=0.01 with IMPLIES → strong violation."""
        strat = ArbitrageStrategy()
        ctx = valid_arb_context(
            market_data={
                "mkt_A": {"market_id": "mkt_A", "midpoint": 0.99},
                "mkt_B": {"market_id": "mkt_B", "midpoint": 0.01},
            }
        )
        sig = strat.generate_signal(
            valid_features(market_id="mkt_A"),
            context=ctx,
        )
        assert sig.decision == StrategyDecision.CANDIDATE
        assert "IMPLIES" in sig.reason
        assert sig.gross_edge is not None

    def test_negative_edge_no_signal(self) -> None:
        """With IMPLIES, if P(A)=0.30 <= P(B)=0.50, no edge → NO_SIGNAL."""
        strat = ArbitrageStrategy()
        ctx = valid_arb_context(
            market_data={
                "mkt_A": {"market_id": "mkt_A", "midpoint": 0.30},
                "mkt_B": {"market_id": "mkt_B", "midpoint": 0.50},
            }
        )
        sig = strat.generate_signal(
            valid_features(market_id="mkt_A"),
            context=ctx,
        )
        assert sig.decision == StrategyDecision.NO_SIGNAL

    def test_configurable_tolerance(self) -> None:
        """Tight tolerance detects small violations."""
        strat = ArbitrageStrategy(max_inconsistency=0.005)
        # P(A)=0.405, P(B)=0.400 → Δ=0.005 > 0.005? No, it's equal to tolerance
        # Actually Δ=0.005 is not > 0.005, so no violation
        ctx = valid_arb_context(
            market_data={
                "mkt_A": {"market_id": "mkt_A", "midpoint": 0.405},
                "mkt_B": {"market_id": "mkt_B", "midpoint": 0.400},
            }
        )
        sig = strat.generate_signal(
            valid_features(market_id="mkt_A"),
            context=ctx,
        )
        assert sig.decision == StrategyDecision.NO_SIGNAL

        # P(A)=0.406, P(B)=0.400 → Δ=0.006 > 0.005 → violation
        ctx2 = valid_arb_context(
            market_data={
                "mkt_A": {"market_id": "mkt_A", "midpoint": 0.406},
                "mkt_B": {"market_id": "mkt_B", "midpoint": 0.400},
            }
        )
        sig2 = strat.generate_signal(
            valid_features(market_id="mkt_A"),
            context=ctx2,
        )
        assert sig2.decision == StrategyDecision.CANDIDATE


# ═══════════════════════════════════════════════════════════════════════
#  Stub strategies
# ═══════════════════════════════════════════════════════════════════════


class TestStubStrategies:
    """ProbabilityStrategy and EnsembleStrategy are stubs."""

    def test_probability_returns_no_signal(self) -> None:
        from app.strategies import ProbabilityStrategy

        strat = ProbabilityStrategy()
        sig = strat.generate_signal(valid_features())
        assert sig.decision == StrategyDecision.NO_SIGNAL
        assert "not yet implemented" in sig.reason

    def test_ensemble_returns_no_signal(self) -> None:
        from app.strategies import EnsembleStrategy

        strat = EnsembleStrategy()
        sig = strat.generate_signal(valid_features())
        assert sig.decision == StrategyDecision.NO_SIGNAL
        assert "not yet implemented" in sig.reason


# ═══════════════════════════════════════════════════════════════════════
#  Signal dataclass
# ═══════════════════════════════════════════════════════════════════════


class TestSignalDataclass:
    """Signal construction and defaults."""

    def test_signal_id_generated(self) -> None:
        sig = Signal(strategy="test", market_id="mkt_001", side="YES")
        assert len(sig.signal_id) == 12

    def test_signal_timestamp_generated(self) -> None:
        sig = Signal(strategy="test", market_id="mkt_001", side="YES")
        assert len(sig.timestamp) > 0

    def test_signal_explicit_id(self) -> None:
        sig = Signal(
            strategy="test",
            market_id="mkt_001",
            side="YES",
            signal_id="my_custom_id",
        )
        assert sig.signal_id == "my_custom_id"

    def test_signal_decision_defaults_no_signal(self) -> None:
        sig = Signal(strategy="test", market_id="mkt_001", side="YES")
        assert sig.decision == StrategyDecision.NO_SIGNAL

    def test_signal_repr(self) -> None:
        sig = Signal(
            strategy="test",
            market_id="mkt_001",
            side="YES",
            decision=StrategyDecision.CANDIDATE,
        )
        r = repr(sig)
        assert "CANDIDATE" in r
        assert "mkt_001" in r
