"""Trading strategy implementations.

Every strategy evaluates market features and returns a ``Signal``
with a ``decision`` of either ``NO_SIGNAL`` or ``CANDIDATE``.
"""

from app.strategies.arbitrage import ArbitrageStrategy, RelationshipType
from app.strategies.base import Signal, Strategy, StrategyDecision
from app.strategies.ensemble import EnsembleStrategy
from app.strategies.microstructure import MicrostructureStrategy
from app.strategies.probability import ProbabilityStrategy

__all__ = [
    "Strategy",
    "StrategyDecision",
    "Signal",
    "RelationshipType",
    "MicrostructureStrategy",
    "ArbitrageStrategy",
    "ProbabilityStrategy",
    "EnsembleStrategy",
]
