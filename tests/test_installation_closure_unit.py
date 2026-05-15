"""Unit tests for installation and closure flows.

Tests self-check pass/fail, SunVeillance sync, and renewal closure.

Requirements: 8.3, 8.4, 9.2, 16.3
"""

from unittest.mock import MagicMock, patch

import pytest

from dreams_workflow.shared.models import CaseStatus
from dreams_workflow.workflow_engine.installation_flow import (
    handle_installation_phase,
    handle_self_check,
)
from dreams_workflow.workflow_engine.closure_flow import handle_case_closure
from dreams_workflow.workflow_engine.renewal_flow import (
    handle_renewal,
    handle_renewal_complete,
)


# =============================================================================
# Tests: Installation Phase
# =============================================================================


class TestInstallationPhase:
    """Tests for handle_installation_phase."""

    @patch("dreams_workflow.workflow_engine.installation_flow._invoke_email_service")
    @patch("dreams_workflow.workflow_engine.installation_flow.CloudRagicClient")
    def test_sends_approval_notification(self, mock_ragic_cls, mock_email):
        """Sends approval notification email to customer."""
        mock_ragic = MagicMock()
        mock_ragic.get_questionnaire_data.return_value = {"customer_email": "c@test.com"}
        mock_ragic_cls.return_value = mock_ragic

        result = handle_installation_phase("CASE-001", {"customer_email": "c@test.com"})

        assert result["action"] == "installation_notification_sent"
        assert result["email_sent_to"] == "c@test.com"
        mock_email.assert_called_once()


class TestSelfCheck:
    """Tests for handle_self_check."""

    @patch("dreams_workflow.workflow_engine.installation_flow.CloudRagicClient")
    @patch("requests.post")
    def test_self_check_passed_goes_online(self, mock_post, mock_ragic_cls):
        """Self-check pass triggers online procedure and status update."""
        # Self-check API returns passed
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"passed": True}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        mock_ragic = MagicMock()
        mock_ragic.get_case_status.return_value = CaseStatus.INSTALLATION_PHASE
        mock_ragic_cls.return_value = mock_ragic

        from dreams_workflow.workflow_engine import installation_flow
        installation_flow.DREAMS_API_URL = "http://test-api"

        result = handle_self_check("CASE-001", {})

        assert result["action"] == "online_procedure_complete"
        assert result["new_status"] == CaseStatus.ONLINE_COMPLETED.value

        # Reset
        installation_flow.DREAMS_API_URL = ""

    @patch("dreams_workflow.workflow_engine.installation_flow._invoke_email_service")
    @patch("requests.post")
    def test_self_check_failed_notifies_customer(self, mock_post, mock_email):
        """Self-check failure notifies customer of issues."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"passed": False, "issues": ["接線異常", "訊號弱"]}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        from dreams_workflow.workflow_engine import installation_flow
        installation_flow.DREAMS_API_URL = "http://test-api"

        result = handle_self_check("CASE-001", {"customer_email": "c@test.com"})

        assert result["action"] == "self_check_failed"
        assert "接線異常" in result["issues"]

        installation_flow.DREAMS_API_URL = ""

    def test_self_check_api_not_configured(self):
        """When API not configured, returns skipped."""
        from dreams_workflow.workflow_engine import installation_flow
        installation_flow.DREAMS_API_URL = ""

        result = handle_self_check("CASE-001", {})

        assert result["action"] == "self_check_skipped"


# =============================================================================
# Tests: Case Closure
# =============================================================================


class TestCaseClosure:
    """Tests for handle_case_closure."""

    @patch("dreams_workflow.workflow_engine.closure_flow._invoke_email_service")
    @patch("dreams_workflow.workflow_engine.closure_flow._sync_to_sunveillance")
    @patch("dreams_workflow.workflow_engine.closure_flow.CloudRagicClient")
    def test_closure_syncs_and_closes(self, mock_ragic_cls, mock_sync, mock_email):
        """Closure flow syncs to SunVeillance and transitions to 已結案."""
        mock_ragic = MagicMock()
        mock_ragic.get_case_status.return_value = CaseStatus.ONLINE_COMPLETED
        mock_ragic_cls.return_value = mock_ragic
        mock_sync.return_value = True

        payload = {
            "customer_email": "c@test.com",
            "site_name": "Test Site",
            "customer_name": "Test Customer",
        }

        result = handle_case_closure("CASE-001", payload)

        assert result["action"] == "case_closed"
        assert result["sunveillance_synced"] is True
        assert result["new_status"] == CaseStatus.CASE_CLOSED.value
        mock_ragic.update_case_status.assert_called_once_with(
            "CASE-001", CaseStatus.CASE_CLOSED.value
        )

    @patch("dreams_workflow.workflow_engine.closure_flow._invoke_email_service")
    @patch("dreams_workflow.workflow_engine.closure_flow._sync_to_sunveillance")
    @patch("dreams_workflow.workflow_engine.closure_flow.CloudRagicClient")
    def test_closure_continues_even_if_sync_fails(self, mock_ragic_cls, mock_sync, mock_email):
        """Closure still proceeds even if SunVeillance sync fails."""
        mock_ragic = MagicMock()
        mock_ragic.get_case_status.return_value = CaseStatus.ONLINE_COMPLETED
        mock_ragic_cls.return_value = mock_ragic
        mock_sync.return_value = False

        result = handle_case_closure("CASE-001", {"customer_email": "c@test.com"})

        assert result["action"] == "case_closed"
        assert result["sunveillance_synced"] is False


# =============================================================================
# Tests: Renewal Flow
# =============================================================================


class TestRenewalFlow:
    """Tests for renewal flow."""

    @patch("dreams_workflow.workflow_engine.renewal_flow._invoke_email_service")
    @patch("dreams_workflow.workflow_engine.renewal_flow.CloudRagicClient")
    def test_renewal_sends_login_info(self, mock_ragic_cls, mock_email):
        """Renewal flow sends SunVeillance login info."""
        mock_ragic = MagicMock()
        mock_ragic.get_questionnaire_data.return_value = {"customer_email": "c@test.com"}
        mock_ragic_cls.return_value = mock_ragic

        result = handle_renewal("CASE-001", {"customer_email": "c@test.com"})

        assert result["action"] == "renewal_login_sent"
        mock_email.assert_called_once()

    @patch("dreams_workflow.workflow_engine.renewal_flow.CloudRagicClient")
    def test_renewal_complete_closes_case(self, mock_ragic_cls):
        """Renewal completion writes site_id and closes case."""
        mock_ragic = MagicMock()
        mock_ragic.get_case_status.return_value = CaseStatus.PENDING_QUESTIONNAIRE
        mock_ragic_cls.return_value = mock_ragic

        result = handle_renewal_complete("CASE-001", {"renewal_site_id": "SITE-ABC"})

        assert result["action"] == "renewal_closed"
        assert result["renewal_site_id"] == "SITE-ABC"
        assert result["new_status"] == CaseStatus.CASE_CLOSED.value
        mock_ragic.update_case_record.assert_called_once()
        mock_ragic.update_case_status.assert_called_once()
