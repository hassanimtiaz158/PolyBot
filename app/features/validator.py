"""Feature-validation layer.

Checks computed feature dicts for missing values, stale data, extreme
values, and insufficient observations.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

# ── Thresholds ────────────────────────────────────────────────────────

PRICE_MIN = 0.0
PRICE_MAX = 1.0
SPREAD_MIN = 0.0
SPREAD_MAX = 1.0
OBI_MIN = -1.0
OBI_MAX = 1.0
MAX_COMPOSITE_SCORE = 1.0
DEFAULT_MAX_AGE = 5  # seconds


class FeatureValidator:
    """Validates computed feature dicts and returns warnings / errors.

    Usage::

        v = FeatureValidator()
        issues = v.validate(orderbook_features)
        if issues["errors"]:
            ...  # reject
    """

    @staticmethod
    def validate(
        features: dict[str, Any],
        max_age_seconds: int = DEFAULT_MAX_AGE,
    ) -> dict[str, list[str]]:
        """Run all checks on a feature dict.

        Returns ``{"errors": [...], "warnings": [...]}``.
        Errors should prevent trading; warnings are advisory.
        """
        errors: list[str] = []
        warnings: list[str] = []

        # ── Timestamp freshness ─────────────────────────────────────
        ts = features.get("timestamp")
        if ts is None:
            errors.append("Feature dict has no timestamp")
        else:
            try:
                dt = datetime.fromisoformat(
                    str(ts).replace("Z", "+00:00")
                )
                age = (datetime.now(UTC) - dt).total_seconds()
                if age > max_age_seconds:
                    errors.append(
                        f"Feature timestamp is {age:.1f}s old "
                        f"(max {max_age_seconds}s)"
                    )
            except (ValueError, TypeError):
                errors.append(f"Unparseable timestamp: {ts!r}")

        # ── Price-range checks ──────────────────────────────────────
        for key in ("midpoint", "bid", "ask"):
            val = features.get(key)
            if val is not None and not isinstance(val, (int, float)):
                errors.append(f"{key} is not numeric: {val!r}")
            elif val is not None and (val < PRICE_MIN or val > PRICE_MAX):
                errors.append(f"{key}={val} outside [{PRICE_MIN}, {PRICE_MAX}]")

        # ── Spread checks ───────────────────────────────────────────
        spread = features.get("absolute_spread") or features.get("spread")
        if spread is not None:
            if not isinstance(spread, (int, float)):
                errors.append(f"Spread is not numeric: {spread!r}")
            elif spread < SPREAD_MIN or spread > SPREAD_MAX:
                errors.append(
                    f"Spread={spread} outside [{SPREAD_MIN}, {SPREAD_MAX}]"
                )

        # ── OBI range ───────────────────────────────────────────────
        obi = features.get("obi")
        if obi is not None:
            if not isinstance(obi, (int, float)):
                errors.append(f"OBI is not numeric: {obi!r}")
            elif obi < OBI_MIN or obi > OBI_MAX:
                errors.append(f"OBI={obi} outside [{OBI_MIN}, {OBI_MAX}]")

        # ── Liquidity / composite scores ────────────────────────────
        for key in ("liquidity_score", "composite_score"):
            val = features.get(key)
            if val is not None:
                if not isinstance(val, (int, float)):
                    errors.append(f"{key} is not numeric: {val!r}")
                elif val < 0 or val > MAX_COMPOSITE_SCORE:
                    errors.append(
                        f"{key}={val} outside [0, {MAX_COMPOSITE_SCORE}]"
                    )

        # ── Depth checks ────────────────────────────────────────────
        for key in ("bid_depth", "ask_depth", "total_depth"):
            val = features.get(key)
            if val is not None:
                if not isinstance(val, (int, float)):
                    errors.append(f"{key} is not numeric: {val!r}")
                elif val < 0:
                    errors.append(f"{key}={val} is negative")

        # ── Return checks ───────────────────────────────────────────
        for key, val in features.items():
            if key.startswith("return_") and val is not None:
                if not isinstance(val, (int, float)):
                    warnings.append(f"{key} is not numeric: {val!r}")
                # Returns can be outside [-1, 1] in degenerate cases
                if isinstance(val, float) and (val < -10 or val > 10):
                    warnings.append(f"{key}={val} is extreme")

        # ── Velocity checks ─────────────────────────────────────────
        vel = features.get("velocity_60s")
        if vel is not None and isinstance(vel, (int, float)):
            if abs(vel) > 0.1:
                warnings.append(f"velocity_60s={vel} is extreme (> 10%/s)")

        # ── Volatility checks ───────────────────────────────────────
        vol = features.get("realised_volatility")
        if vol is not None and isinstance(vol, (int, float)) and vol < 0:
            errors.append(f"realised_volatility={vol} is negative")

        return {"errors": errors, "warnings": warnings}

    @staticmethod
    def is_valid(
        features: dict[str, Any],
        max_age_seconds: int = DEFAULT_MAX_AGE,
    ) -> bool:
        """``True`` when there are no errors (warnings are OK)."""
        result = FeatureValidator.validate(features, max_age_seconds)
        return len(result["errors"]) == 0

    @staticmethod
    def has_timestamp(features: dict[str, Any]) -> bool:
        """``True`` if the feature dict carries a timestamp."""
        return features.get("timestamp") is not None
