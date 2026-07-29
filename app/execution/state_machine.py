"""Order state machine — CREATED → RISK_APPROVED → SUBMITTED → ..."""

from enum import Enum, auto


class OrderState(Enum):
    """Valid order states."""

    CREATED = auto()
    RISK_APPROVED = auto()
    SUBMITTED = auto()
    REJECTED = auto()
    CANCELLED = auto()
    PARTIALLY_FILLED = auto()
    FILLED = auto()


# Valid transitions
_VALID_TRANSITIONS: dict[OrderState, set[OrderState]] = {
    OrderState.CREATED: {OrderState.RISK_APPROVED, OrderState.REJECTED},
    OrderState.RISK_APPROVED: {OrderState.SUBMITTED, OrderState.REJECTED},
    OrderState.SUBMITTED: {
        OrderState.REJECTED,
        OrderState.CANCELLED,
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
    },
    OrderState.PARTIALLY_FILLED: {OrderState.CANCELLED, OrderState.FILLED},
    OrderState.REJECTED: set(),
    OrderState.CANCELLED: set(),
    OrderState.FILLED: set(),
}


class OrderStateMachine:
    """Tracks and validates order state transitions."""

    def __init__(self, initial_state: OrderState = OrderState.CREATED) -> None:
        self._state = initial_state

    @property
    def state(self) -> OrderState:
        return self._state

    def transition(self, new_state: OrderState) -> None:
        """Attempt a state transition; raises ValueError if invalid."""
        if new_state not in _VALID_TRANSITIONS[self._state]:
            raise ValueError(
                f"Invalid transition: {self._state.name} -> {new_state.name}"
            )
        self._state = new_state

    def can_transition(self, new_state: OrderState) -> bool:
        """Check if a transition is valid without applying it."""
        return new_state in _VALID_TRANSITIONS[self._state]

    def is_terminal(self) -> bool:
        """Check if the order is in a terminal state."""
        return self._state in {OrderState.REJECTED, OrderState.CANCELLED, OrderState.FILLED}
