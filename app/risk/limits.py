"""Hard limit enforcement with machine-readable rejection codes.

Every check returns a ``LimitCheck`` named tuple with a boolean
``approved`` flag and a ``reason`` string (empty string when approved).

Rejection codes
---------------
STALE_DATA                 — feature timestamp exceeds max age
INVALID_DATA               — missing or malformed required fields
NET_EDGE_BELOW_THRESHOLD   — net edge < min_net_edge
CONFIDENCE_BELOW_THRESHOLD — confidence < min_confidence
SPREAD_TOO_HIGH            — bid-ask spread > max_spread
LIQUIDITY_TOO_LOW          — liquidity score < min_liquidity
POSITION_SIZE_EXCEEDS_MAX  — proposed size > equity × max_position_pct
MARKET_EXPOSURE_TOO_HIGH   — single market > equity × max_market_exposure_pct
TOTAL_EXPOSURE_TOO_HIGH    — portfolio > equity × max_total_exposure_pct
DAILY_LOSS_LIMIT_REACHED   — daily P&L < -(equity × max_daily_loss_pct)
CONSECUTIVE_LOSS_LIMIT_REACHED — loss streak ≥ max_consecutive_losses
MAX_OPEN_POSITIONS_REACHED — open positions ≥ max_open_positions
POSITION_SIZE_ZERO         — computed size is zero
"""

from __future__ import annotations

from collections import namedtuple
from datetime import UTC, datetime
from typing import Any

from app.config.settings import settings

LimitCheck = namedtuple("LimitCheck", ["approved", "reason"])


def _ok() -> LimitCheck:
    return LimitCheck(True, "")


def _fail(reason: str) -> LimitCheck:
    return LimitCheck(False, reason)


class RiskLimits:
    """Evaluates hard risk limits against the current portfolio state.

    All thresholds come from ``settings``.  Every public method returns a
    ``LimitCheck(approved: bool, reason: str)``.
    """

    # ── System / data quality ──────────────────────────────────────────

    def check_data_freshness(self, timestamp: str | None) -> LimitCheck:
        """Reject if the data timestamp exceeds ``data_max_age_seconds``."""
        if timestamp is None:
            return _fail("STALE_DATA")
        try:
            dt = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
            age = (datetime.now(UTC) - dt).total_seconds()
            if age > settings.data_max_age_seconds:
                return _fail("STALE_DATA")
        except (ValueError, TypeError):
            return _fail("STALE_DATA")
        return _ok()

    def check_data_validity(self, features: dict[str, Any]) -> LimitCheck:
        """Reject if required fields are missing or invalid.

        Validates: presence, numeric type, finite values, and positive
        midpoint/spread/bid/ask.  NaN or Inf in any field is rejected.
        """
        required = ["market_id", "midpoint", "spread", "bid", "ask"]
        for field in required:
            val = features.get(field)
            if val is None:
                return _fail("INVALID_DATA")
        # Validate numeric fields are finite
        for field in ("midpoint", "spread", "bid", "ask"):
            val = features.get(field)
            if isinstance(val, (int, float)) and not (
                val == val  # NaN check
                and val != float("inf")
                and val != float("-inf")
            ):
                return _fail("INVALID_DATA")
            if isinstance(val, (int, float)) and val < 0:
                return _fail("INVALID_DATA")
        return _ok()

    def check_net_edge(self, net_edge: float | None) -> LimitCheck:
        """Reject if net edge is below ``min_net_edge``."""
        if net_edge is None:
            return _fail("NET_EDGE_BELOW_THRESHOLD")
        if net_edge < settings.min_net_edge:
            return _fail("NET_EDGE_BELOW_THRESHOLD")
        return _ok()

    def check_confidence(self, confidence: float | None) -> LimitCheck:
        """Reject if confidence is below ``min_confidence``."""
        if confidence is None:
            return _fail("CONFIDENCE_BELOW_THRESHOLD")
        if confidence < settings.min_confidence:
            return _fail("CONFIDENCE_BELOW_THRESHOLD")
        return _ok()

    # ── Trade parameters ───────────────────────────────────────────────

    def check_spread(self, spread: float | None) -> LimitCheck:
        """Reject if spread exceeds ``max_spread``."""
        if spread is None:
            return _fail("SPREAD_TOO_HIGH")
        if spread > settings.max_spread:
            return _fail("SPREAD_TOO_HIGH")
        return _ok()

    def check_liquidity(self, liquidity: float | None) -> LimitCheck:
        """Reject if liquidity is below ``min_liquidity``."""
        if liquidity is None:
            return _fail("LIQUIDITY_TOO_LOW")
        if liquidity < settings.min_liquidity:
            return _fail("LIQUIDITY_TOO_LOW")
        return _ok()

    # ── Portfolio limits ───────────────────────────────────────────────

    def check_position_size(self, proposed_size: float, equity: float) -> LimitCheck:
        """Reject if proposed size exceeds ``max_position_pct`` of equity."""
        if proposed_size <= 0:
            return _fail("POSITION_SIZE_ZERO")
        if proposed_size > equity * settings.max_position_pct:
            return _fail("POSITION_SIZE_EXCEEDS_MAX")
        return _ok()

    def check_market_exposure(
        self, market_exposure: float, equity: float
    ) -> LimitCheck:
        """Reject if single-market exposure exceeds limit."""
        if market_exposure > equity * settings.max_market_exposure_pct:
            return _fail("MARKET_EXPOSURE_TOO_HIGH")
        return _ok()

    def check_total_exposure(
        self, total_exposure: float, equity: float
    ) -> LimitCheck:
        """Reject if total portfolio exposure exceeds limit."""
        if total_exposure > equity * settings.max_total_exposure_pct:
            return _fail("TOTAL_EXPOSURE_TOO_HIGH")
        return _ok()

    def check_daily_loss(self, daily_pnl: float, equity: float) -> LimitCheck:
        """Reject if today's loss exceeds ``max_daily_loss_pct``."""
        if daily_pnl < -(equity * settings.max_daily_loss_pct):
            return _fail("DAILY_LOSS_LIMIT_REACHED")
        return _ok()

    def check_consecutive_losses(self, consecutive_losses: int) -> LimitCheck:
        """Reject if consecutive losses reach ``max_consecutive_losses``."""
        if consecutive_losses >= settings.max_consecutive_losses:
            return _fail("CONSECUTIVE_LOSS_LIMIT_REACHED")
        return _ok()

    def check_open_positions(self, open_count: int) -> LimitCheck:
        """Reject if open positions reach ``max_open_positions``."""
        if open_count >= settings.max_open_positions:
            return _fail("MAX_OPEN_POSITIONS_REACHED")
        return _ok()

    # ── Aggregate ───────────────────────────────────────────────────────

    def all_checks(
        self,
        proposed_size: float,
        equity: float,
        market_exposure: float,
        total_exposure: float,
        daily_pnl: float,
        consecutive_losses: int,
        open_positions: int,
    ) -> LimitCheck:
        """Run all portfolio-level checks.

        Returns the **first** failing check, or an OK check if all pass.
        This ensures callers get a single clear machine-readable reason.
        """
        for check in [
            self.check_position_size(proposed_size, equity),
            self.check_market_exposure(market_exposure, equity),
            self.check_total_exposure(total_exposure, equity),
            self.check_daily_loss(daily_pnl, equity),
            self.check_consecutive_losses(consecutive_losses),
            self.check_open_positions(open_positions),
        ]:
            if not check.approved:
                return check
        return _ok()
