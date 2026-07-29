"""Market eligibility scoring and filtering.

Implements the weighted quality score described in PRD section 7.
"""

from dataclasses import dataclass

from app.config.settings import settings


@dataclass
class EligibilityResult:
    """Result of a market eligibility evaluation."""

    market_id: str
    score: float
    eligible: bool
    reasons: list[str]


class MarketEligibility:
    """Evaluates whether a market meets minimum quality thresholds.

    Weights (from PRD):
      - Liquidity: 25%
      - Spread: 20%
      - Historical signal quality: 20%
      - Model confidence: 20%
      - Execution quality: 15%
    """

    def evaluate(self, market_id: str, **metrics: float) -> EligibilityResult:
        """Compute the composite eligibility score for a market."""
        score = 0.0
        reasons: list[str] = []

        liquidity = metrics.get("liquidity", 0)
        spread = metrics.get("spread", 1.0)
        hist_quality = metrics.get("historical_signal_quality", 0)
        model_conf = metrics.get("model_confidence", 0)
        exec_quality = metrics.get("execution_quality", 0)

        if settings.min_liquidity > 0:
            liq_ratio = min(liquidity / settings.min_liquidity, 1.0)
            score += liq_ratio * 0.25
        if settings.max_spread > 0:
            spr_ratio = 1.0 - min(spread / settings.max_spread, 1.0)
            score += spr_ratio * 0.20
        score += hist_quality * 0.20
        score += model_conf * 0.20
        score += exec_quality * 0.15

        if score < 0.6:
            reasons.append(f"Score {score:.2f} below ignore threshold (0.6)")

        eligible = score >= 0.6
        return EligibilityResult(
            market_id=market_id,
            score=round(score, 4),
            eligible=eligible,
            reasons=reasons,
        )
