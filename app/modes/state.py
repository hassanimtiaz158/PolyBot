"""Operating mode state machine.

All transitions require explicit operator action, except HALTED
which can be triggered automatically by circuit breakers.
"""

from enum import Enum


class OperatingMode(Enum):
    """Valid operating modes for the trading bot."""

    RESEARCH = "RESEARCH"
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    LIVE_GUARDED = "LIVE_GUARDED"
    HALTED = "HALTED"


# Valid mode transitions: {from_mode: {to_mode, ...}}
_VALID_TRANSITIONS: dict[OperatingMode, set[OperatingMode]] = {
    OperatingMode.RESEARCH: {OperatingMode.BACKTEST, OperatingMode.HALTED},
    OperatingMode.BACKTEST: {OperatingMode.PAPER, OperatingMode.RESEARCH, OperatingMode.HALTED},
    OperatingMode.PAPER: {OperatingMode.LIVE_GUARDED, OperatingMode.BACKTEST, OperatingMode.HALTED},
    OperatingMode.LIVE_GUARDED: {OperatingMode.PAPER, OperatingMode.HALTED},
    OperatingMode.HALTED: {OperatingMode.RESEARCH, OperatingMode.BACKTEST, OperatingMode.PAPER},
}


class ModeState:
    """Persistable operating mode state machine.

    On startup the system always loads into HALTED mode until health
    checks pass and the operator explicitly transitions out.
    """

    def __init__(self, initial_mode: OperatingMode = OperatingMode.HALTED) -> None:
        self._mode = initial_mode

    @property
    def mode(self) -> OperatingMode:
        return self._mode

    def can_transition(self, target: OperatingMode) -> bool:
        """Check if a mode transition is valid."""
        return target in _VALID_TRANSITIONS.get(self._mode, set())

    def transition(self, target: OperatingMode) -> None:
        """Apply a mode transition; raises ValueError if invalid."""
        if not self.can_transition(target):
            raise ValueError(
                f"Invalid mode transition: {self._mode.value} -> {target.value}"
            )
        self._mode = target

    def is_trading(self) -> bool:
        """Check if the mode allows order submission."""
        return self._mode in {OperatingMode.PAPER, OperatingMode.LIVE_GUARDED}

    def is_live(self) -> bool:
        """Check if the mode allows real order submission."""
        return self._mode == OperatingMode.LIVE_GUARDED
