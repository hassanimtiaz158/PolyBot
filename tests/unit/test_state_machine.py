"""Tests for the order state machine."""

import pytest

from app.execution.state_machine import OrderState, OrderStateMachine


class TestOrderStateMachine:
    def test_initial_state(self):
        sm = OrderStateMachine()
        assert sm.state == OrderState.CREATED

    def test_valid_transition(self):
        sm = OrderStateMachine()
        sm.transition(OrderState.RISK_APPROVED)
        assert sm.state == OrderState.RISK_APPROVED

    def test_invalid_transition_raises(self):
        sm = OrderStateMachine()
        with pytest.raises(ValueError, match="Invalid transition"):
            sm.transition(OrderState.FILLED)

    def test_can_transition(self):
        sm = OrderStateMachine()
        assert sm.can_transition(OrderState.RISK_APPROVED) is True
        assert sm.can_transition(OrderState.FILLED) is False

    def test_is_terminal(self):
        sm = OrderStateMachine()
        assert sm.is_terminal() is False
        sm.transition(OrderState.RISK_APPROVED)
        sm.transition(OrderState.SUBMITTED)
        sm.transition(OrderState.FILLED)
        assert sm.is_terminal() is True

    def test_full_lifecycle(self):
        sm = OrderStateMachine()
        assert sm.state == OrderState.CREATED
        sm.transition(OrderState.RISK_APPROVED)
        assert sm.state == OrderState.RISK_APPROVED
        sm.transition(OrderState.SUBMITTED)
        assert sm.state == OrderState.SUBMITTED
        sm.transition(OrderState.PARTIALLY_FILLED)
        assert sm.state == OrderState.PARTIALLY_FILLED
        sm.transition(OrderState.FILLED)
        assert sm.state == OrderState.FILLED
