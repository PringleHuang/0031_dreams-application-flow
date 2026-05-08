"""Property-based tests for state change logging completeness.

# Feature: dreams-application-flow, Property 2: 狀態變更紀錄完整性

Validates that every successful state transition produces a log entry
containing case_id, original status, target status, and reason.
"""

import json
import logging
from unittest.mock import MagicMock, patch

from hypothesis import given, settings, strategies as st

from dreams_workflow.shared.models import CaseStatus
from dreams_workflow.shared.state_machine import (
    VALID_TRANSITIONS,
    transition_case_status,
)


# Strategy: generate valid (current, target) pairs from VALID_TRANSITIONS
def valid_transition_pairs():
    """Generate valid (current_status, target_status) pairs."""
    pairs = []
    for current, targets in VALID_TRANSITIONS.items():
        for target in targets:
            pairs.append((current, target))
    return st.sampled_from(pairs)


# Strategy: generate non-empty reason strings
reason_strategy = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P", "Z")),
    min_size=1,
    max_size=50,
)

# Strategy: generate case IDs
case_id_strategy = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N")),
    min_size=1,
    max_size=20,
)


class TestStateChangeLoggingCompleteness:
    """Property 2: 狀態變更紀錄完整性"""

    @settings(max_examples=100)
    @given(
        transition=valid_transition_pairs(),
        case_id=case_id_strategy,
        reason=reason_strategy,
    )
    def test_successful_transition_logs_all_required_fields(
        self, transition, case_id, reason
    ):
        """Every successful transition must log case_id, original status, target status, and reason."""
        current_status, target_status = transition

        # Mock store to avoid actual RAGIC calls
        mock_store = MagicMock()
        mock_store.get_case_status.return_value = current_status

        # Capture log output
        log_records = []

        class LogCapture(logging.Handler):
            def emit(self, record):
                log_records.append(record)

        logger = logging.getLogger("dreams_workflow.shared.state_machine")
        handler = LogCapture()
        handler.setLevel(logging.DEBUG)
        logger.addHandler(handler)

        try:
            result = transition_case_status(
                case_id=case_id,
                new_status=target_status,
                reason=reason,
                current_status=current_status,
                store=mock_store,
            )

            assert result is True

            # Verify at least one log record contains all required fields
            found_complete_log = False
            for record in log_records:
                msg = record.getMessage()
                has_case_id = hasattr(record, "case_id") and record.case_id == case_id
                has_operation = (
                    hasattr(record, "operation_type")
                    and record.operation_type == "state_transition"
                )
                has_current = current_status.value in msg
                has_target = target_status.value in msg
                has_reason = reason in msg

                if has_case_id and has_operation and has_current and has_target and has_reason:
                    found_complete_log = True
                    break

            assert found_complete_log, (
                f"No complete log found for transition "
                f"{current_status.value} → {target_status.value} "
                f"(case_id={case_id}, reason={reason}). "
                f"Log records: {[r.getMessage() for r in log_records]}"
            )

        finally:
            logger.removeHandler(handler)

    @settings(max_examples=100)
    @given(
        transition=valid_transition_pairs(),
        case_id=case_id_strategy,
        reason=reason_strategy,
    )
    def test_successful_transition_log_level_is_info(
        self, transition, case_id, reason
    ):
        """Successful transitions should be logged at INFO level."""
        current_status, target_status = transition

        mock_store = MagicMock()
        mock_store.get_case_status.return_value = current_status

        log_records = []

        class LogCapture(logging.Handler):
            def emit(self, record):
                log_records.append(record)

        logger = logging.getLogger("dreams_workflow.shared.state_machine")
        handler = LogCapture()
        handler.setLevel(logging.DEBUG)
        logger.addHandler(handler)

        try:
            transition_case_status(
                case_id=case_id,
                new_status=target_status,
                reason=reason,
                current_status=current_status,
                store=mock_store,
            )

            # Find the state_transition log
            transition_logs = [
                r
                for r in log_records
                if hasattr(r, "operation_type")
                and r.operation_type == "state_transition"
            ]

            assert len(transition_logs) >= 1, "No state_transition log found"
            assert transition_logs[0].levelno == logging.INFO

        finally:
            logger.removeHandler(handler)

    @settings(max_examples=100)
    @given(
        transition=valid_transition_pairs(),
        case_id=case_id_strategy,
        reason=reason_strategy,
    )
    def test_store_update_called_on_success(self, transition, case_id, reason):
        """Successful transitions must update the store (RAGIC)."""
        current_status, target_status = transition

        mock_store = MagicMock()
        mock_store.get_case_status.return_value = current_status

        transition_case_status(
            case_id=case_id,
            new_status=target_status,
            reason=reason,
            current_status=current_status,
            store=mock_store,
        )

        mock_store.update_case_status.assert_called_once_with(
            case_id, target_status.value
        )
