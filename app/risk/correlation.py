"""Correlated exposure and portfolio-level risk limits.

The bot must not treat every market as independent.  When multiple
markets depend on the same underlying event, they are correlated
exposure and must be managed as one bucket: if the combined exposure
would exceed the event limit, the bot prefers NO TRADE over breaking
the limit.

``CorrelationRegistry`` answers three questions about a market:

* which event it depends on (defaults to the market itself),
* which direction it bets (``+1``: YES = event occurs,
  ``-1``: YES = event does NOT occur),
* when it resolves (markets that resolve at the same time form a
  concentration bucket).

The registry is fail-closed: registering a market twice with
conflicting metadata raises ``ValueError`` instead of silently
overwriting, and every unregistered market is treated as its own
event (no hidden correlations are assumed).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import settings


@dataclass
class CorrelationGroup:
    """All markets that depend on one underlying event."""

    event_id: str
    resolution_time: float | None = None
    markets: dict[str, float] = field(default_factory=dict)
    """market_id -> direction (+1 / -1)"""


class CorrelationRegistry:
    """Maps markets to underlying events, directions, and resolution times.

    * ``register_event`` creates an event bucket (with an optional
      resolution timestamp, in seconds since the epoch).
    * ``register_market`` attaches a market to an event with a
      direction.  Events are created on demand when a market is
      registered against an unknown event id.
    * ``event_for`` / ``direction_for`` / ``resolution_time_for``
      fall back to the market itself as its own event (direction
      ``+1``, no resolution time) when unregistered.
    """

    def __init__(self) -> None:
        self._events: dict[str, CorrelationGroup] = {}
        self._market_event: dict[str, str] = {}
        self._market_direction: dict[str, float] = {}
        self._market_resolution: dict[str, float] = {}

    # ── Registration ───────────────────────────────────────────────

    def register_event(
        self, event_id: str, resolution_time: float | None = None
    ) -> CorrelationGroup:
        """Create (or fetch) an event bucket."""
        group = self._events.get(event_id)
        if group is None:
            group = CorrelationGroup(event_id=event_id)
            self._events[event_id] = group
        if resolution_time is not None:
            group.resolution_time = resolution_time
            for market_id in group.markets:
                self._market_resolution[market_id] = resolution_time
        return group

    def register_market(
        self,
        market_id: str,
        event_id: str,
        direction: float = 1.0,
        resolution_time: float | None = None,
    ) -> None:
        """Attach a market to an event with a direction.

        Raises ``ValueError`` (fail-closed) if the market is already
        registered with a different event, direction, or resolution
        time.
        """
        if direction not in (1.0, -1.0):
            raise ValueError(
                f"direction must be +1 or -1, got {direction!r} for {market_id!r}"
            )
        known_event = self._market_event.get(market_id)
        if known_event is not None and known_event != event_id:
            raise ValueError(
                f"market {market_id!r} already registered to event "
                f"{known_event!r}, cannot re-register to {event_id!r}"
            )
        known_direction = self._market_direction.get(market_id)
        if known_direction is not None and known_direction != direction:
            raise ValueError(
                f"market {market_id!r} already registered with direction "
                f"{known_direction}, cannot change to {direction}"
            )
        known_resolution = self._market_resolution.get(market_id)
        if (
            known_resolution is not None
            and resolution_time is not None
            and known_resolution != resolution_time
        ):
            raise ValueError(
                f"market {market_id!r} already registered with resolution "
                f"time {known_resolution}, cannot change to {resolution_time}"
            )

        group = self.register_event(event_id, resolution_time)
        group.markets[market_id] = direction
        self._market_event[market_id] = event_id
        self._market_direction[market_id] = direction
        if resolution_time is not None:
            self._market_resolution[market_id] = resolution_time

    # ── Queries ────────────────────────────────────────────────────

    def event_for(self, market_id: str) -> str:
        """Event a market depends on (market itself when unregistered)."""
        return self._market_event.get(market_id, market_id)

    def direction_for(self, market_id: str) -> float:
        """Direction of a market's bet on its event (``+1`` default)."""
        return self._market_direction.get(market_id, 1.0)

    def resolution_time_for(self, market_id: str) -> float | None:
        """Resolution timestamp of a market (``None`` when unknown)."""
        return self._market_resolution.get(market_id)

    def markets_in_event(self, event_id: str) -> dict[str, float]:
        """market_id -> direction for every market in an event."""
        group = self._events.get(event_id)
        return dict(group.markets) if group else {}

    def markets_with_resolution(self, resolution_time: float) -> set[str]:
        """Markets whose event resolves at *resolution_time*."""
        return {
            market_id
            for market_id, res in self._market_resolution.items()
            if res == resolution_time
        }

    def events(self) -> dict[str, CorrelationGroup]:
        """A copy of the event registry."""
        return {
            event_id: CorrelationGroup(
                event_id=group.event_id,
                resolution_time=group.resolution_time,
                markets=dict(group.markets),
            )
            for event_id, group in self._events.items()
        }

    def event_count(self) -> int:
        return len(self._events)

    def market_count(self) -> int:
        return len(self._market_event)


class PortfolioRiskLimits:
    """Portfolio-level concentration limits.

    Mirrors the style of :class:`app.risk.limits.RiskLimits`: no
    ``__init__``, thresholds read from the settings globals at check
    time, and every check returns a machine-readable rejection code
    (or ``""``).  Fail-closed: limits are compared as
    ``exposure > equity * pct``.
    """

    @staticmethod
    def check_event_exposure(event_exposure: float, equity: float) -> str:
        limit = equity * settings.max_event_exposure_pct
        if event_exposure > limit:
            return "EVENT_EXPOSURE_TOO_HIGH"
        return ""

    @staticmethod
    def check_strategy_exposure(strategy_exposure: float, equity: float) -> str:
        limit = equity * settings.max_strategy_exposure_pct
        if strategy_exposure > limit:
            return "STRATEGY_EXPOSURE_TOO_HIGH"
        return ""

    @staticmethod
    def check_directional_exposure(
        directional_exposure: float, equity: float
    ) -> str:
        limit = equity * settings.max_directional_exposure_pct
        if abs(directional_exposure) > limit:
            return "DIRECTIONAL_EXPOSURE_TOO_HIGH"
        return ""

    @staticmethod
    def check_resolution_concentration(
        resolution_exposure: float, equity: float
    ) -> str:
        limit = equity * settings.max_resolution_exposure_pct
        if resolution_exposure > limit:
            return "RESOLUTION_CONCENTRATION_TOO_HIGH"
        return ""
