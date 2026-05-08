"""Property-based tests for state machine transition logic.

Property 1: 狀態機轉換合法性
Validates: Requirements 10.3

Uses hypothesis to generate random (current_status, target_status) combinations,
verifying that:
- Valid transitions (defined in VALID_TRANSITIONS) succeed and return True
- Invalid transitions (not in VALID_TRANSITIONS) raise InvalidTransitionError
- validate_transition is consistent with transition_case_status behavior
- The state machine is deterministic (same inputs always produce same outcome)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from hypothesis import given, settings, strategies as st

from dreams_workflow.shared.exceptions import InvalidTransitionError
from dreams_workflow.shared.models import CaseStatus
from dreams_workflow.shared.state_machine import (
    VALID_TRANSITIONS,
    validate_transition,
    transition_case_status,
)


# =============================================================================
# Strategies for generating test data
# =============================================================================

# Strategy for any CaseStatus value
case_status_strategy = st.sampled_from(list(CaseStatus))

# Strategy for a pair of (current, target) statuses
status_pair_strategy = st.tuples(case_status_strategy, case_status_strategy)

# Strategy for generating valid transitions only
_valid_pairs: list[tuple[CaseStatus, CaseStatus]] = []
for source, targets in VALID_TRANSITIONS.items():
    for target in targets:
        _valid_pairs.append((source, target))

valid_transition_strategy = st.sampled_from(_valid_pairs)

# Strategy for generating invalid transitions only
_all_statuses = list(CaseStatus)
_invalid_pairs: list[tuple[CaseStatus, CaseStatus]] = []
for source in _all_statuses:
    allowed = VALID_TRANSITIONS.get(source, [])
    for target in _all_statuses:
        if target not in allowed:
            _invalid_pairs.append((source, target))

invalid_transition_strategy = st.sampled_from(_invalid_pairs)

# Strategy for case IDs
case_id_strategy = st.text(
    min_size=1,
    max_size=20,
    alphabet=st.characters(whitelist_categories=("L", "N", "Pd")),
)

# Strategy for transition reasons
reason_strategy = st.text(min_size=1, max_size=50)


# =============================================================================
# Helper
# =============================================================================


def _make_mock_store(current_status: CaseStatus) -> MagicMock:
    """Create a mock CaseStatusStore that returns the given current status."""
    store = MagicMock()
    store.get_case_status.return_value = current_status
    return store


# =============================================================================
# Property Tests
# =============================================================================


class TestStateMachineTransitionLegality:
    """Property 1: 狀態機轉換合法性"""

    # Feature: dreams-application-flow, Property 1: 狀態機轉換合法性

    @settings(max_examples=100)
    @given(transition=valid_transition_strategy)
    def test_valid_transitions_always_succeed(
        self, transition: tuple[CaseStatus, CaseStatus]
    ):
        """All transitions defined in VALID_TRANSITIONS succeed via validate_transition."""
        current, target = transition
        assert validate_transition(current, target) is True

    @settings(max_examples=100)
    @given(transition=invalid_transition_strategy)
    def test_invalid_transitions_always_rejected(
        self, transition: tuple[CaseStatus, CaseStatus]
    ):
        """All transitions NOT in VALID_TRANSITIONS are rejected by validate_transition."""
        current, target = transition
        assert validate_transition(current, target) is False

    @settings(max_examples=100)
    @given(
        transition=valid_transition_strategy,
        case_id=case_id_strategy,
        reason=reason_strategy,
    )
    def test_valid_transition_case_status_returns_true(
        self, transition: tuple[CaseStatus, CaseStatus], case_id: str, reason: str
    ):
        """transition_case_status returns True for valid transitions."""
        current, target = transition
        store = _make_mock_store(current)

        result = transition_case_status(
            case_id=case_id,
            new_status=target,
            reason=reason,
            current_status=current,
            store=store,
        )

        assert result is True

    @settings(max_examples=100)
    @given(
        transition=invalid_transition_strategy,
        case_id=case_id_strategy,
        reason=reason_strategy,
    )
    def test_invalid_transition_case_status_raises_error(
        self, transition: tuple[CaseStatus, CaseStatus], case_id: str, reason: str
    ):
        """transition_case_status raises InvalidTransitionError for invalid transitions."""
        current, target = transition
        store = _make_mock_store(current)

        with pytest.raises(InvalidTransitionError) as exc_info:
            transition_case_status(
                case_id=case_id,
                new_status=target,
                reason=reason,
                current_status=current,
                store=store,
            )

        assert exc_info.value.current_status == current.value
        assert exc_info.value.target_status == target.value

    @settings(max_examples=100)
    @given(pair=status_pair_strategy)
    def test_validate_transition_is_deterministic(
        self, pair: tuple[CaseStatus, CaseStatus]
    ):
        """Same (current, target) pair always produces the same validation result."""
        current, target = pair
        result1 = validate_transition(current, target)
        result2 = validate_transition(current, target)
        assert result1 == result2

    @settings(max_examples=100)
    @given(pair=status_pair_strategy)
    def test_validate_and_transition_are_consistent(
        self, pair: tuple[CaseStatus, CaseStatus]
    ):
        """validate_transition result is consistent with transition_case_status behavior.

        If validate_transition returns True, transition_case_status should succeed.
        If validate_transition returns False, transition_case_status should raise.
        """
        current, target = pair
        is_valid = validate_transition(current, target)
        store = _make_mock_store(current)

        if is_valid:
            result = transition_case_status(
                case_id="test-case",
                new_status=target,
                reason="consistency check",
                current_status=current,
                store=store,
            )
            assert result is True
        else:
            with pytest.raises(InvalidTransitionError):
                transition_case_status(
                    case_id="test-case",
                    new_status=target,
                    reason="consistency check",
                    current_status=current,
                    store=store,
                )

    @settings(max_examples=50)
    @given(status=case_status_strategy)
    def test_self_transition_is_always_invalid(self, status: CaseStatus):
        """A status cannot transition to itself (no self-loops in the state machine)."""
        # Verify no self-transitions are defined
        assert validate_transition(status, status) is False

    @settings(max_examples=50)
    @given(
        transition=valid_transition_strategy,
        case_id=case_id_strategy,
        reason=reason_strategy,
    )
    def test_valid_transition_updates_store(
        self, transition: tuple[CaseStatus, CaseStatus], case_id: str, reason: str
    ):
        """Successful transitions call store.update_case_status with the new status."""
        current, target = transition
        store = _make_mock_store(current)

        transition_case_status(
            case_id=case_id,
            new_status=target,
            reason=reason,
            current_status=current,
            store=store,
        )

        store.update_case_status.assert_called_once_with(case_id, target.value)

    @settings(max_examples=50)
    @given(
        transition=invalid_transition_strategy,
        case_id=case_id_strategy,
        reason=reason_strategy,
    )
    def test_invalid_transition_does_not_update_store(
        self, transition: tuple[CaseStatus, CaseStatus], case_id: str, reason: str
    ):
        """Failed transitions do not call store.update_case_status."""
        current, target = transition
        store = _make_mock_store(current)

        with pytest.raises(InvalidTransitionError):
            transition_case_status(
                case_id=case_id,
                new_status=target,
                reason=reason,
                current_status=current,
                store=store,
            )

        store.update_case_status.assert_not_called()
