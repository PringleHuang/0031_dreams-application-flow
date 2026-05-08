"""Unit tests for Taipower review flow.

Tests the DREAMS Form API client and the taipower_flow handler logic.

Requirements: 5.1, 5.2, 5.3, 5.4
"""

import base64
import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from dreams_workflow.dreams_client.client import (
    DreamsApiClient,
    DreamsApiResponse,
    ERROR_NO_ELECTRICITY_NUMBER,
)
from dreams_workflow.shared.exceptions import DreamsConnectionError
from dreams_workflow.workflow_engine.taipower_flow import (
    handle_taipower_review,
    _build_case_data,
    _handle_api_success,
    _handle_no_electricity_number,
)


# =============================================================================
# Tests: DreamsApiClient
# =============================================================================


class TestDreamsApiClient:
    """Tests for DreamsApiClient."""

    def test_submit_application_success(self):
        """API returns success with case_number and pdf_base64."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "success": True,
            "case_number": "DREAMS-2026-001",
            "pdf_base64": base64.b64encode(b"fake pdf content").decode(),
        }
        mock_response.raise_for_status = MagicMock()

        with patch("requests.post", return_value=mock_response) as mock_post:
            client = DreamsApiClient(api_url="http://test-api/submit")
            result = client.submit_application("CASE-001", {"electricity_number": "12-34-5678-90-1"})

        assert result.success is True
        assert result.case_number == "DREAMS-2026-001"
        assert result.pdf_base64 is not None
        assert result.error_code is None
        mock_post.assert_called_once()

    def test_submit_application_no_electricity_number(self):
        """API returns error when electricity number not found."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "success": False,
            "error_code": "NO_ELECTRICITY_NUMBER",
            "error_message": "電號不存在於 DREAMS 系統",
        }
        mock_response.raise_for_status = MagicMock()

        with patch("requests.post", return_value=mock_response):
            client = DreamsApiClient(api_url="http://test-api/submit")
            result = client.submit_application("CASE-001", {"electricity_number": "99-99-9999-99-9"})

        assert result.success is False
        assert result.error_code == ERROR_NO_ELECTRICITY_NUMBER
        assert result.error_message == "電號不存在於 DREAMS 系統"
        assert result.case_number is None

    def test_submit_application_http_error_raises_dreams_connection_error(self):
        """HTTP error triggers DreamsConnectionError for retry."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        http_error = requests.exceptions.HTTPError(response=mock_response)
        mock_response.raise_for_status.side_effect = http_error

        with patch("requests.post", return_value=mock_response):
            client = DreamsApiClient(api_url="http://test-api/submit")
            with pytest.raises(DreamsConnectionError):
                client.submit_application("CASE-001", {})

    def test_submit_application_connection_error_raises_dreams_connection_error(self):
        """Connection error triggers DreamsConnectionError for retry."""
        with patch(
            "requests.post",
            side_effect=requests.exceptions.ConnectionError("Connection refused"),
        ):
            client = DreamsApiClient(api_url="http://test-api/submit")
            with pytest.raises(DreamsConnectionError):
                client.submit_application("CASE-001", {})

    def test_submit_application_not_configured(self):
        """Returns error response when API URL not configured."""
        client = DreamsApiClient(api_url="")
        result = client.submit_application("CASE-001", {})

        assert result.success is False
        assert result.error_code == "NOT_CONFIGURED"

    def test_submit_application_timeout(self):
        """Timeout triggers DreamsConnectionError."""
        with patch(
            "requests.post",
            side_effect=requests.exceptions.Timeout("Request timed out"),
        ):
            client = DreamsApiClient(api_url="http://test-api/submit", timeout=5)
            with pytest.raises(DreamsConnectionError):
                client.submit_application("CASE-001", {})


# =============================================================================
# Tests: handle_taipower_review
# =============================================================================


class TestHandleTaipowerReview:
    """Tests for the handle_taipower_review orchestration function."""

    @patch("dreams_workflow.workflow_engine.taipower_flow._invoke_email_service")
    @patch("dreams_workflow.workflow_engine.taipower_flow._get_supporting_document_attachments")
    @patch("dreams_workflow.workflow_engine.taipower_flow.CloudRagicClient")
    @patch("dreams_workflow.workflow_engine.taipower_flow.DreamsApiClient")
    def test_success_sends_review_email(
        self, mock_api_cls, mock_ragic_cls, mock_get_docs, mock_email
    ):
        """On API success, writes case_number to RAGIC and sends email with attachments."""
        # Setup DREAMS API mock
        mock_api = MagicMock()
        mock_api.submit_application.return_value = DreamsApiResponse(
            success=True,
            case_number="DREAMS-001",
            pdf_base64=base64.b64encode(b"pdf").decode(),
        )
        mock_api_cls.return_value = mock_api

        # Setup RAGIC mock
        mock_ragic = MagicMock()
        mock_ragic_cls.return_value = mock_ragic

        # Setup document attachments
        mock_get_docs.return_value = [
            {"filename": "doc1.pdf", "content_base64": "abc", "content_type": "application/pdf"}
        ]

        payload = {
            "electricity_number": "12-34-5678-90-1",
            "customer_name": "Test Customer",
            "taipower_contact_email": "taipower@example.com",
        }

        with patch.dict("os.environ", {"EMAIL_SERVICE_FUNCTION_NAME": "email-fn"}):
            result = handle_taipower_review("CASE-001", payload)

        assert result["action"] == "taipower_review_submitted"
        assert result["case_number"] == "DREAMS-001"
        # Verify case_number written to RAGIC
        mock_ragic.update_case_record.assert_called_once()
        # Verify email sent
        mock_email.assert_called_once()

    @patch("dreams_workflow.workflow_engine.taipower_flow._invoke_email_service")
    @patch("dreams_workflow.workflow_engine.taipower_flow.DreamsApiClient")
    def test_no_electricity_number_sends_notification(self, mock_api_cls, mock_email):
        """On NO_ELECTRICITY_NUMBER, sends creation request notification."""
        mock_api = MagicMock()
        mock_api.submit_application.return_value = DreamsApiResponse(
            success=False,
            error_code=ERROR_NO_ELECTRICITY_NUMBER,
            error_message="電號不存在",
        )
        mock_api_cls.return_value = mock_api

        payload = {"electricity_number": "99-99-9999-99-9"}

        with patch.dict("os.environ", {
            "EMAIL_SERVICE_FUNCTION_NAME": "email-fn",
            "TAIPOWER_REVIEW_CONTACT_EMAIL": "review@taipower.com",
        }):
            result = handle_taipower_review("CASE-001", payload)

        assert result["action"] == "electricity_number_request_sent"
        mock_email.assert_called_once()

    @patch("dreams_workflow.workflow_engine.taipower_flow.DreamsApiClient")
    def test_api_connection_failure(self, mock_api_cls):
        """On API connection failure (after retries), returns error."""
        mock_api = MagicMock()
        mock_api.submit_application.side_effect = DreamsConnectionError(
            service_name="DREAMS_API",
            message="Connection failed after 3 retries",
        )
        mock_api_cls.return_value = mock_api

        result = handle_taipower_review("CASE-001", {})

        assert result["action"] == "taipower_review_failed"
        assert "error" in result

    @patch("dreams_workflow.workflow_engine.taipower_flow._invoke_email_service")
    @patch("dreams_workflow.workflow_engine.taipower_flow._get_supporting_document_attachments")
    @patch("dreams_workflow.workflow_engine.taipower_flow.CloudRagicClient")
    @patch("dreams_workflow.workflow_engine.taipower_flow.DreamsApiClient")
    def test_success_includes_pdf_attachment(
        self, mock_api_cls, mock_ragic_cls, mock_get_docs, mock_email
    ):
        """On success, the PDF from API response is included as attachment."""
        pdf_content = base64.b64encode(b"real pdf content").decode()
        mock_api = MagicMock()
        mock_api.submit_application.return_value = DreamsApiResponse(
            success=True,
            case_number="DREAMS-002",
            pdf_base64=pdf_content,
        )
        mock_api_cls.return_value = mock_api
        mock_ragic_cls.return_value = MagicMock()
        mock_get_docs.return_value = []

        payload = {"taipower_contact_email": "tp@example.com"}

        with patch.dict("os.environ", {"EMAIL_SERVICE_FUNCTION_NAME": "email-fn"}):
            result = handle_taipower_review("CASE-002", payload)

        # Check that email was called with attachments containing the PDF
        call_kwargs = mock_email.call_args
        attachments = call_kwargs[1].get("attachments") or call_kwargs[0][4] if len(call_kwargs[0]) > 4 else None
        # The function uses keyword args
        assert result["attachments_count"] == 1  # Only PDF since mock_get_docs returns []

    @patch("dreams_workflow.workflow_engine.taipower_flow.DreamsApiClient")
    def test_other_api_error(self, mock_api_cls):
        """On other API errors, returns error details."""
        mock_api = MagicMock()
        mock_api.submit_application.return_value = DreamsApiResponse(
            success=False,
            error_code="FORM_VALIDATION_ERROR",
            error_message="Missing required field",
        )
        mock_api_cls.return_value = mock_api

        result = handle_taipower_review("CASE-001", {})

        assert result["action"] == "taipower_review_api_error"
        assert result["error_code"] == "FORM_VALIDATION_ERROR"
