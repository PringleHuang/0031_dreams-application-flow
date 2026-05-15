"""Property-based tests for renewal case flow completeness.

# Feature: dreams-application-flow, Property 10: 續約案件流程完整性

Validates that renewal cases do not trigger AI determination, go directly
from 待填問卷 to 已結案 (via SunVeillance website), and close directly
without intermediate states.
"""

from unittest.mock import MagicMock, patch

from hypothesis import given, settings, strategies as st

from dreams_workflow.shared.models import CaseStatus, CaseType
from dreams_workflow.shared.state_machine import VALID_TRANSITIONS, validate_transition
from dreams_workflow.workflow_engine.renewal_flow import handle_renewal_complete


# Strategy: generate random renewal case data
case_id_strategy = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N")),
    min_size=1,
    max_size=20,
)

site_id_strategy = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P")),
    min_size=1,
    max_size=30,
)


class TestRenewalFlowCompleteness:
    """Property 10: 續約案件流程完整性"""

    @settings(max_examples=100)
    @given(case_id=case_id_strategy, site_id=site_id_strategy)
    def test_renewal_does_not_pass_through_taipower_review(self, case_id, site_id):
        """Renewal cases must not transition through 台電審核 or 安裝階段.

        PENDING_QUESTIONNAIRE can go to CASE_CLOSED (renewal direct close)
        but not to TAIPOWER_REVIEW or INSTALLATION_PHASE directly.
        """
        allowed = VALID_TRANSITIONS.get(CaseStatus.PENDING_QUESTIONNAIRE, [])
        assert CaseStatus.CASE_CLOSED in allowed
        assert CaseStatus.TAIPOWER_REVIEW not in allowed
        assert CaseStatus.INSTALLATION_PHASE not in allowed

    @settings(max_examples=100)
    @given(case_id=case_id_strategy, site_id=site_id_strategy)
    def test_renewal_transitions_directly_to_closed(self, case_id, site_id):
        """Renewal cases transition directly from 待填問卷 to 已結案."""
        assert validate_transition(
            CaseStatus.PENDING_QUESTIONNAIRE, CaseStatus.CASE_CLOSED
        ) is True

    @settings(max_examples=100)
    @given(case_id=case_id_strategy, site_id=site_id_strategy)
    def test_renewal_complete_writes_site_id_and_closes(self, case_id, site_id):
        """handle_renewal_complete writes site_id to RAGIC and transitions to 已結案."""
        mock_ragic = MagicMock()
        mock_ragic.get_case_status.return_value = CaseStatus.PENDING_QUESTIONNAIRE

        with patch(
            "dreams_workflow.workflow_engine.renewal_flow.CloudRagicClient",
            return_value=mock_ragic,
        ):
            result = handle_renewal_complete(case_id, {"renewal_site_id": site_id})

        assert result["action"] == "renewal_closed"
        assert result["new_status"] == CaseStatus.CASE_CLOSED.value
        assert result["renewal_site_id"] == site_id

        # Verify RAGIC was updated
        mock_ragic.update_case_record.assert_called_once()
        update_data = mock_ragic.update_case_record.call_args[0][1]
        assert update_data["renewal_site_id"] == site_id
        assert update_data["closure_reason"] == "續約完成"

        # Verify status was updated
        mock_ragic.update_case_status.assert_called_once_with(
            case_id, CaseStatus.CASE_CLOSED.value
        )

    @settings(max_examples=100)
    @given(case_id=case_id_strategy)
    def test_renewal_questionnaire_returns_redirect(self, case_id):
        """Renewal questionnaire response returns renewal_redirect action (no status change)."""
        from dreams_workflow.workflow_engine.app import handle_questionnaire_response

        with patch(
            "dreams_workflow.workflow_engine.app.CloudRagicClient",
        ), patch(
            "dreams_workflow.shared.case_resolver.resolve_ragic_id_from_payload",
            return_value=None,
        ), patch(
            "dreams_workflow.shared.case_resolver.resolve_case_context",
            return_value={"resolved": False},
        ):
            result = handle_questionnaire_response(
                case_id, {"electricity_number": "06-1234-5678"}, is_renewal=True
            )

        assert result["action"] == "renewal_redirect"
        assert "new_status" not in result
