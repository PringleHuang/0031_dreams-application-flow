"""Unit tests for state machine transition logic.

Tests all valid transition paths (positive cases), illegal transition rejection,
and boundary conditions (same-state transitions, None values).

Requirements: 10.3
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from dreams_workflow.shared.exceptions import InvalidTransitionError
from dreams_workflow.shared.models import CaseStatus
from dreams_workflow.shared.state_machine import (
    VALID_TRANSITIONS,
    validate_transition,
    transition_case_status,
)


# =============================================================================
# Helpers
# =============================================================================


def _make_mock_store(current_status: CaseStatus) -> MagicMock:
    """Create a mock CaseStatusStore returning the given current status."""
    store = MagicMock()
    store.get_case_status.return_value = current_status
    return store


# =============================================================================
# Test: All valid transition paths (positive cases)
# =============================================================================


class TestValidTransitionPaths:
    """Test all valid transition paths defined in VALID_TRANSITIONS."""

    def test_pending_questionnaire_to_pending_manual_confirm(self):
        """新約 AI 判定完成：待填問卷 → 待人工確認"""
        assert validate_transition(
            CaseStatus.PENDING_QUESTIONNAIRE, CaseStatus.PENDING_MANUAL_CONFIRM
        ) is True

    def test_pending_questionnaire_to_renewal_processing(self):
        """續約案件分流：待填問卷 → 續約處理"""
        assert validate_transition(
            CaseStatus.PENDING_QUESTIONNAIRE, CaseStatus.RENEWAL_PROCESSING
        ) is True

    def test_renewal_processing_to_case_closed(self):
        """續約完成直接結案：續約處理 → 已結案"""
        assert validate_transition(
            CaseStatus.RENEWAL_PROCESSING, CaseStatus.CASE_CLOSED
        ) is True

    def test_pending_manual_confirm_to_taipower_review(self):
        """人工確認合格：待人工確認 → 台電審核"""
        assert validate_transition(
            CaseStatus.PENDING_MANUAL_CONFIRM, CaseStatus.TAIPOWER_REVIEW
        ) is True

    def test_pending_manual_confirm_to_info_supplement(self):
        """人工確認不合格：待人工確認 → 資訊補件"""
        assert validate_transition(
            CaseStatus.PENDING_MANUAL_CONFIRM, CaseStatus.INFO_SUPPLEMENT
        ) is True

    def test_info_supplement_to_pending_manual_confirm(self):
        """補件後 AI 判定完成：資訊補件 → 待人工確認"""
        assert validate_transition(
            CaseStatus.INFO_SUPPLEMENT, CaseStatus.PENDING_MANUAL_CONFIRM
        ) is True

    def test_taipower_review_to_pre_send_confirm(self):
        """台電回覆後：台電審核 → 發送前人工確認"""
        assert validate_transition(
            CaseStatus.TAIPOWER_REVIEW, CaseStatus.PRE_SEND_CONFIRM
        ) is True

    def test_pre_send_confirm_to_installation_phase(self):
        """人工確認核准：發送前人工確認 → 安裝階段"""
        assert validate_transition(
            CaseStatus.PRE_SEND_CONFIRM, CaseStatus.INSTALLATION_PHASE
        ) is True

    def test_pre_send_confirm_to_taipower_supplement(self):
        """人工確認需補件：發送前人工確認 → 台電補件"""
        assert validate_transition(
            CaseStatus.PRE_SEND_CONFIRM, CaseStatus.TAIPOWER_SUPPLEMENT
        ) is True

    def test_taipower_supplement_to_taipower_review(self):
        """補件完成重新申請：台電補件 → 台電審核"""
        assert validate_transition(
            CaseStatus.TAIPOWER_SUPPLEMENT, CaseStatus.TAIPOWER_REVIEW
        ) is True

    def test_installation_phase_to_online_completed(self):
        """自主檢查通過：安裝階段 → 完成上線"""
        assert validate_transition(
            CaseStatus.INSTALLATION_PHASE, CaseStatus.ONLINE_COMPLETED
        ) is True

    def test_online_completed_to_case_closed(self):
        """資料同步完成：完成上線 → 已結案"""
        assert validate_transition(
            CaseStatus.ONLINE_COMPLETED, CaseStatus.CASE_CLOSED
        ) is True

    def test_all_valid_transitions_count(self):
        """Verify the total number of valid transitions matches expectations."""
        total = sum(len(targets) for targets in VALID_TRANSITIONS.values())
        assert total == 13


class TestValidTransitionExecution:
    """Test transition_case_status succeeds for all valid paths."""

    @pytest.mark.parametrize(
        "current,target,description",
        [
            (CaseStatus.PENDING_QUESTIONNAIRE, CaseStatus.PENDING_MANUAL_CONFIRM, "新約AI判定完成"),
            (CaseStatus.PENDING_QUESTIONNAIRE, CaseStatus.RENEWAL_PROCESSING, "續約案件分流"),
            (CaseStatus.RENEWAL_PROCESSING, CaseStatus.CASE_CLOSED, "續約完成結案"),
            (CaseStatus.PENDING_MANUAL_CONFIRM, CaseStatus.TAIPOWER_REVIEW, "人工確認合格"),
            (CaseStatus.PENDING_MANUAL_CONFIRM, CaseStatus.INFO_SUPPLEMENT, "人工確認不合格"),
            (CaseStatus.INFO_SUPPLEMENT, CaseStatus.PENDING_MANUAL_CONFIRM, "補件後重新判定"),
            (CaseStatus.TAIPOWER_REVIEW, CaseStatus.PRE_SEND_CONFIRM, "台電回覆進入人工確認"),
            (CaseStatus.PRE_SEND_CONFIRM, CaseStatus.INSTALLATION_PHASE, "人工確認核准"),
            (CaseStatus.PRE_SEND_CONFIRM, CaseStatus.TAIPOWER_SUPPLEMENT, "人工確認需補件"),
            (CaseStatus.TAIPOWER_SUPPLEMENT, CaseStatus.TAIPOWER_REVIEW, "補件完成重新申請"),
            (CaseStatus.INSTALLATION_PHASE, CaseStatus.ONLINE_COMPLETED, "自主檢查通過"),
            (CaseStatus.ONLINE_COMPLETED, CaseStatus.CASE_CLOSED, "資料同步完成結案"),
        ],
    )
    def test_transition_case_status_succeeds(self, current, target, description):
        """transition_case_status returns True and updates store for valid paths."""
        store = _make_mock_store(current)

        result = transition_case_status(
            case_id="CASE-001",
            new_status=target,
            reason=description,
            current_status=current,
            store=store,
        )

        assert result is True
        store.update_case_status.assert_called_once_with("CASE-001", target.value)


# =============================================================================
# Test: Illegal transition rejection
# =============================================================================


class TestIllegalTransitionRejection:
    """Test that illegal transitions are properly rejected."""

    @pytest.mark.parametrize(
        "current,target",
        [
            # Cannot skip stages
            (CaseStatus.PENDING_QUESTIONNAIRE, CaseStatus.TAIPOWER_REVIEW),
            (CaseStatus.PENDING_QUESTIONNAIRE, CaseStatus.INSTALLATION_PHASE),
            (CaseStatus.PENDING_QUESTIONNAIRE, CaseStatus.CASE_CLOSED),
            # Cannot go backwards (except defined loops)
            (CaseStatus.TAIPOWER_REVIEW, CaseStatus.PENDING_QUESTIONNAIRE),
            (CaseStatus.INSTALLATION_PHASE, CaseStatus.TAIPOWER_REVIEW),
            (CaseStatus.CASE_CLOSED, CaseStatus.PENDING_QUESTIONNAIRE),
            # Terminal state cannot transition
            (CaseStatus.CASE_CLOSED, CaseStatus.ONLINE_COMPLETED),
            (CaseStatus.CASE_CLOSED, CaseStatus.RENEWAL_PROCESSING),
            # Renewal cannot go to non-defined targets
            (CaseStatus.RENEWAL_PROCESSING, CaseStatus.TAIPOWER_REVIEW),
            (CaseStatus.RENEWAL_PROCESSING, CaseStatus.INSTALLATION_PHASE),
            # Cross-path transitions not allowed
            (CaseStatus.INFO_SUPPLEMENT, CaseStatus.TAIPOWER_REVIEW),
            (CaseStatus.TAIPOWER_SUPPLEMENT, CaseStatus.INFO_SUPPLEMENT),
        ],
    )
    def test_validate_transition_returns_false(self, current, target):
        """validate_transition returns False for illegal transitions."""
        assert validate_transition(current, target) is False

    @pytest.mark.parametrize(
        "current,target",
        [
            (CaseStatus.PENDING_QUESTIONNAIRE, CaseStatus.CASE_CLOSED),
            (CaseStatus.CASE_CLOSED, CaseStatus.PENDING_QUESTIONNAIRE),
            (CaseStatus.RENEWAL_PROCESSING, CaseStatus.TAIPOWER_REVIEW),
        ],
    )
    def test_transition_case_status_raises_invalid_transition_error(self, current, target):
        """transition_case_status raises InvalidTransitionError for illegal transitions."""
        store = _make_mock_store(current)

        with pytest.raises(InvalidTransitionError) as exc_info:
            transition_case_status(
                case_id="CASE-002",
                new_status=target,
                reason="illegal attempt",
                current_status=current,
                store=store,
            )

        assert exc_info.value.current_status == current.value
        assert exc_info.value.target_status == target.value

    def test_illegal_transition_does_not_update_store(self):
        """Store is not updated when transition is rejected."""
        store = _make_mock_store(CaseStatus.CASE_CLOSED)

        with pytest.raises(InvalidTransitionError):
            transition_case_status(
                case_id="CASE-003",
                new_status=CaseStatus.PENDING_QUESTIONNAIRE,
                reason="should not update",
                current_status=CaseStatus.CASE_CLOSED,
                store=store,
            )

        store.update_case_status.assert_not_called()


# =============================================================================
# Test: Boundary conditions
# =============================================================================


class TestBoundaryConditions:
    """Test boundary conditions: same-state transitions, None values, edge cases."""

    @pytest.mark.parametrize("status", list(CaseStatus))
    def test_same_state_transition_is_invalid(self, status):
        """Transitioning to the same state is always invalid (no self-loops)."""
        assert validate_transition(status, status) is False

    @pytest.mark.parametrize("status", list(CaseStatus))
    def test_same_state_transition_raises_error(self, status):
        """transition_case_status raises InvalidTransitionError for same-state transitions."""
        store = _make_mock_store(status)

        with pytest.raises(InvalidTransitionError) as exc_info:
            transition_case_status(
                case_id="CASE-SELF",
                new_status=status,
                reason="self transition attempt",
                current_status=status,
                store=store,
            )

        assert exc_info.value.current_status == status.value
        assert exc_info.value.target_status == status.value

    def test_case_closed_has_no_outgoing_transitions(self):
        """已結案 is a terminal state with no valid outgoing transitions."""
        assert CaseStatus.CASE_CLOSED not in VALID_TRANSITIONS

    def test_all_non_terminal_states_have_transitions(self):
        """All non-terminal states have at least one valid outgoing transition."""
        terminal_states = {CaseStatus.CASE_CLOSED}
        for status in CaseStatus:
            if status not in terminal_states:
                assert status in VALID_TRANSITIONS, (
                    f"{status.value} should have outgoing transitions"
                )
                assert len(VALID_TRANSITIONS[status]) > 0

    def test_transition_with_store_lookup(self):
        """transition_case_status uses store to look up current status when not provided."""
        store = _make_mock_store(CaseStatus.PENDING_QUESTIONNAIRE)

        result = transition_case_status(
            case_id="CASE-LOOKUP",
            new_status=CaseStatus.PENDING_MANUAL_CONFIRM,
            reason="store lookup test",
            store=store,
        )

        assert result is True
        store.get_case_status.assert_called_once_with("CASE-LOOKUP")
        store.update_case_status.assert_called_once_with(
            "CASE-LOOKUP", CaseStatus.PENDING_MANUAL_CONFIRM.value
        )

    def test_transition_with_empty_case_id(self):
        """transition_case_status works with empty string case_id (no validation on ID)."""
        store = _make_mock_store(CaseStatus.PENDING_QUESTIONNAIRE)

        result = transition_case_status(
            case_id="",
            new_status=CaseStatus.RENEWAL_PROCESSING,
            reason="empty id test",
            current_status=CaseStatus.PENDING_QUESTIONNAIRE,
            store=store,
        )

        assert result is True

    def test_transition_with_empty_reason(self):
        """transition_case_status works with empty reason string."""
        store = _make_mock_store(CaseStatus.INSTALLATION_PHASE)

        result = transition_case_status(
            case_id="CASE-EMPTY-REASON",
            new_status=CaseStatus.ONLINE_COMPLETED,
            reason="",
            current_status=CaseStatus.INSTALLATION_PHASE,
            store=store,
        )

        assert result is True

    def test_valid_transitions_dict_covers_all_non_terminal_statuses(self):
        """VALID_TRANSITIONS keys cover all statuses except terminal ones."""
        defined_sources = set(VALID_TRANSITIONS.keys())
        all_statuses = set(CaseStatus)
        # CASE_CLOSED is the only terminal state
        expected_sources = all_statuses - {CaseStatus.CASE_CLOSED}
        assert defined_sources == expected_sources

    def test_no_transition_targets_undefined_status(self):
        """All transition targets are valid CaseStatus enum members."""
        for source, targets in VALID_TRANSITIONS.items():
            for target in targets:
                assert isinstance(target, CaseStatus), (
                    f"Target {target} from {source.value} is not a CaseStatus"
                )
