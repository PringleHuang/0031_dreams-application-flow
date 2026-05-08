"""Unit tests for CloudRagicClient.

Tests cover:
- Successful API interactions (GET/POST)
- Failure scenarios (HTTP errors, connection errors)
- Retry mechanism triggering via tenacity
- Questionnaire data retrieval
- Supporting document downloads
- Case status updates
- Determination result writes
- Supplement questionnaire creation
- Case record updates

Requirements: 15.3
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
import requests
import requests.exceptions

from dreams_workflow.shared.exceptions import RagicCommunicationError
from dreams_workflow.shared.models import CaseStatus
from dreams_workflow.shared.ragic_client import CloudRagicClient


@pytest.fixture
def client():
    """Create a CloudRagicClient with test configuration."""
    return CloudRagicClient(
        base_url="https://ap13.ragic.com",
        account_name="solarcs",
        api_key="test-api-key",
        timeout=5,
    )


@pytest.fixture
def mock_session(client):
    """Patch the client's internal session for mocking HTTP calls."""
    mock = MagicMock()
    client._session = mock
    return mock


# =============================================================================
# Test: get_questionnaire_data
# =============================================================================


class TestGetQuestionnaireData:
    """Tests for CloudRagicClient.get_questionnaire_data."""

    def test_success_returns_record_data(self, client, mock_session):
        """Successful GET returns parsed questionnaire data."""
        expected_data = {
            "customer_name": "王小明",
            "electricity_number": "06-1234-5678",
            "1014650": "key1@審訖圖.pdf",
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = expected_data
        mock_resp.raise_for_status.return_value = None
        mock_session.get.return_value = mock_resp

        result = client.get_questionnaire_data("12345")

        assert result == expected_data
        mock_session.get.assert_called_once()
        call_url = mock_session.get.call_args[0][0]
        assert "work-survey/7/12345" in call_url

    def test_http_error_raises_ragic_communication_error(self, client, mock_session):
        """HTTP error response raises RagicCommunicationError."""
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        http_error = requests.exceptions.HTTPError(response=mock_resp)
        mock_resp.raise_for_status.side_effect = http_error
        mock_session.get.return_value = mock_resp

        with pytest.raises(RagicCommunicationError) as exc_info:
            client.get_questionnaire_data("12345")

        assert "RAGIC" in str(exc_info.value)

    def test_connection_error_raises_ragic_communication_error(
        self, client, mock_session
    ):
        """Connection failure raises RagicCommunicationError."""
        mock_session.get.side_effect = requests.exceptions.ConnectionError(
            "Connection refused"
        )

        with pytest.raises(RagicCommunicationError) as exc_info:
            client.get_questionnaire_data("12345")

        assert "RAGIC" in str(exc_info.value)

    def test_timeout_raises_ragic_communication_error(self, client, mock_session):
        """Request timeout raises RagicCommunicationError."""
        mock_session.get.side_effect = requests.exceptions.Timeout("Read timed out")

        with pytest.raises(RagicCommunicationError) as exc_info:
            client.get_questionnaire_data("12345")

        assert "RAGIC" in str(exc_info.value)


# =============================================================================
# Test: get_supporting_documents
# =============================================================================


class TestGetSupportingDocuments:
    """Tests for CloudRagicClient.get_supporting_documents."""

    def test_success_downloads_all_attachments(self, client, mock_session):
        """Successfully downloads all 5 attachment files."""
        questionnaire_data = {
            "1014650": "filekey1@審訖圖.pdf",
            "1014651": "filekey2@縣府同意備案函文.pdf",
            "1014652": "filekey3@細部協商.pdf",
            "1014653": "filekey4@購售電契約.pdf",
            "1014654": "filekey5@併聯審查意見書.pdf",
        }

        # First call: get questionnaire data (to find attachment fields)
        mock_questionnaire_resp = MagicMock()
        mock_questionnaire_resp.json.return_value = questionnaire_data
        mock_questionnaire_resp.raise_for_status.return_value = None

        # Subsequent calls: download each file
        mock_file_resp = MagicMock()
        mock_file_resp.content = b"fake-pdf-content"
        mock_file_resp.raise_for_status.return_value = None

        mock_session.get.side_effect = [mock_questionnaire_resp] + [
            mock_file_resp
        ] * 5

        result = client.get_supporting_documents("12345")

        assert len(result) == 5
        assert result[0][0] == "審訖圖.pdf"
        assert result[0][1] == b"fake-pdf-content"

    def test_missing_attachment_fields_returns_partial(self, client, mock_session):
        """Records with missing attachment fields return only available docs."""
        questionnaire_data = {
            "1014650": "filekey1@審訖圖.pdf",
            "1014651": "",  # empty
            "1014652": "filekey3@細部協商.pdf",
            # 1014653 and 1014654 not present
        }

        mock_questionnaire_resp = MagicMock()
        mock_questionnaire_resp.json.return_value = questionnaire_data
        mock_questionnaire_resp.raise_for_status.return_value = None

        mock_file_resp = MagicMock()
        mock_file_resp.content = b"pdf-bytes"
        mock_file_resp.raise_for_status.return_value = None

        mock_session.get.side_effect = [mock_questionnaire_resp, mock_file_resp, mock_file_resp]

        result = client.get_supporting_documents("12345")

        assert len(result) == 2

    def test_download_failure_skips_file(self, client, mock_session):
        """Failed file download is skipped gracefully."""
        questionnaire_data = {
            "1014650": "filekey1@審訖圖.pdf",
            "1014651": "filekey2@函文.pdf",
        }

        mock_questionnaire_resp = MagicMock()
        mock_questionnaire_resp.json.return_value = questionnaire_data
        mock_questionnaire_resp.raise_for_status.return_value = None

        # First file download fails, second succeeds
        mock_fail_resp = MagicMock()
        mock_fail_resp.side_effect = requests.exceptions.ConnectionError("timeout")

        mock_success_resp = MagicMock()
        mock_success_resp.content = b"pdf-bytes"
        mock_success_resp.raise_for_status.return_value = None

        mock_session.get.side_effect = [
            mock_questionnaire_resp,
            requests.exceptions.ConnectionError("timeout"),
            mock_success_resp,
        ]

        result = client.get_supporting_documents("12345")

        # Only the second file should be returned
        assert len(result) == 1
        assert result[0][0] == "函文.pdf"

    def test_empty_content_skips_file(self, client, mock_session):
        """Empty file content is treated as download failure."""
        questionnaire_data = {"1014650": "filekey1@審訖圖.pdf"}

        mock_questionnaire_resp = MagicMock()
        mock_questionnaire_resp.json.return_value = questionnaire_data
        mock_questionnaire_resp.raise_for_status.return_value = None

        mock_empty_resp = MagicMock()
        mock_empty_resp.content = b""
        mock_empty_resp.raise_for_status.return_value = None

        mock_session.get.side_effect = [mock_questionnaire_resp, mock_empty_resp]

        result = client.get_supporting_documents("12345")

        assert len(result) == 0


# =============================================================================
# Test: update_case_status
# =============================================================================


class TestUpdateCaseStatus:
    """Tests for CloudRagicClient.update_case_status."""

    def test_success_posts_status_update(self, client, mock_session):
        """Successful status update sends POST with correct data."""
        mock_resp = MagicMock()
        mock_resp.text = ""
        mock_resp.raise_for_status.return_value = None
        mock_session.post.return_value = mock_resp

        client.update_case_status("case-001", "台電審核")

        mock_session.post.assert_called_once()
        call_kwargs = mock_session.post.call_args
        call_url = call_kwargs[0][0]
        assert "business-process2/2/case-001" in call_url
        assert call_kwargs[1]["json"] == {
            "status": "台電審核",
            "doLinkLoad": "first",
            "doFormula": True,
            "doDefaultValue": True,
        }

    def test_http_error_raises_ragic_communication_error(self, client, mock_session):
        """HTTP error on POST raises RagicCommunicationError."""
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.text = "Forbidden"
        http_error = requests.exceptions.HTTPError(response=mock_resp)
        mock_resp.raise_for_status.side_effect = http_error
        mock_session.post.return_value = mock_resp

        with pytest.raises(RagicCommunicationError):
            client.update_case_status("case-001", "台電審核")


# =============================================================================
# Test: write_determination_result
# =============================================================================


class TestWriteDeterminationResult:
    """Tests for CloudRagicClient.write_determination_result."""

    def test_success_writes_json_result(self, client, mock_session):
        """Successful write serializes result as JSON and posts."""
        mock_resp = MagicMock()
        mock_resp.text = ""
        mock_resp.raise_for_status.return_value = None
        mock_session.post.return_value = mock_resp

        result = {
            "case_id": "case-001",
            "overall_status": "all_pass",
            "results": [
                {"document_id": "d1", "status": "pass", "reason": "OK"}
            ],
        }

        client.write_determination_result("case-001", result)

        mock_session.post.assert_called_once()
        call_kwargs = mock_session.post.call_args
        posted_data = call_kwargs[1]["json"]
        assert "ai_determination_result" in posted_data
        parsed = json.loads(posted_data["ai_determination_result"])
        assert parsed["overall_status"] == "all_pass"

    def test_http_error_raises_ragic_communication_error(self, client, mock_session):
        """HTTP error on write raises RagicCommunicationError."""
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Server Error"
        http_error = requests.exceptions.HTTPError(response=mock_resp)
        mock_resp.raise_for_status.side_effect = http_error
        mock_session.post.return_value = mock_resp

        with pytest.raises(RagicCommunicationError):
            client.write_determination_result("case-001", {"test": True})


# =============================================================================
# Test: create_supplement_questionnaire
# =============================================================================


class TestCreateSupplementQuestionnaire:
    """Tests for CloudRagicClient.create_supplement_questionnaire."""

    def test_success_returns_questionnaire_url(self, client, mock_session):
        """Successful creation returns the new questionnaire URL."""
        mock_resp = MagicMock()
        mock_resp.text = '{"ragicTempRecordKey": "99999"}'
        mock_resp.json.return_value = {"ragicTempRecordKey": "99999"}
        mock_resp.raise_for_status.return_value = None
        mock_session.post.return_value = mock_resp

        url = client.create_supplement_questionnaire(
            "case-001", ["審訖圖不符", "契約封面缺漏"]
        )

        assert "99999" in url
        assert "work-survey/7" in url
        # Verify POST data includes failed items
        call_kwargs = mock_session.post.call_args
        posted_data = call_kwargs[1]["json"]
        assert posted_data["case_id"] == "case-001"
        assert "審訖圖不符" in posted_data["supplement_items"]
        assert "契約封面缺漏" in posted_data["supplement_items"]
        assert posted_data["is_supplement"] == "Y"

    def test_http_error_raises_ragic_communication_error(self, client, mock_session):
        """HTTP error on creation raises RagicCommunicationError."""
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "Bad Request"
        http_error = requests.exceptions.HTTPError(response=mock_resp)
        mock_resp.raise_for_status.side_effect = http_error
        mock_session.post.return_value = mock_resp

        with pytest.raises(RagicCommunicationError):
            client.create_supplement_questionnaire("case-001", ["item1"])


# =============================================================================
# Test: update_case_record
# =============================================================================


class TestUpdateCaseRecord:
    """Tests for CloudRagicClient.update_case_record."""

    def test_success_posts_update_data(self, client, mock_session):
        """Successful update posts the provided field data."""
        mock_resp = MagicMock()
        mock_resp.text = ""
        mock_resp.raise_for_status.return_value = None
        mock_session.post.return_value = mock_resp

        update_data = {
            "renewal_site_id": "SITE-123",
            "closure_reason": "續約完成",
        }
        client.update_case_record("case-002", update_data)

        mock_session.post.assert_called_once()
        call_kwargs = mock_session.post.call_args
        posted_json = call_kwargs[1]["json"]
        # Verify original data is included
        assert posted_json["renewal_site_id"] == "SITE-123"
        assert posted_json["closure_reason"] == "續約完成"
        # Verify RAGIC write parameters are included
        assert posted_json["doLinkLoad"] == "first"
        assert posted_json["doFormula"] is True
        assert posted_json["doDefaultValue"] is True
        call_url = call_kwargs[0][0]
        assert "business-process2/2/case-002" in call_url


# =============================================================================
# Test: get_case_status
# =============================================================================


class TestGetCaseStatus:
    """Tests for CloudRagicClient.get_case_status."""

    def test_success_returns_case_status_enum(self, client, mock_session):
        """Successful GET returns the correct CaseStatus enum."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "台電審核"}
        mock_resp.raise_for_status.return_value = None
        mock_session.get.return_value = mock_resp

        result = client.get_case_status("case-001")

        assert result == CaseStatus.TAIPOWER_REVIEW

    def test_unknown_status_raises_value_error(self, client, mock_session):
        """Unknown status value raises ValueError."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "未知狀態"}
        mock_resp.raise_for_status.return_value = None
        mock_session.get.return_value = mock_resp

        with pytest.raises(ValueError, match="Unknown case status"):
            client.get_case_status("case-001")

    def test_alternative_field_name(self, client, mock_session):
        """Status can be read from '案件狀態' field as fallback."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"案件狀態": "已結案"}
        mock_resp.raise_for_status.return_value = None
        mock_session.get.return_value = mock_resp

        result = client.get_case_status("case-001")

        assert result == CaseStatus.CASE_CLOSED


# =============================================================================
# Test: Retry mechanism
# =============================================================================


class TestRetryMechanism:
    """Tests for tenacity retry behavior on CloudRagicClient methods.

    The @retry_ragic decorator retries up to 3 times on RagicCommunicationError
    with 5-second wait intervals.
    """

    @patch("dreams_workflow.shared.ragic_client.CloudRagicClient._get")
    def test_get_questionnaire_retries_on_ragic_error(self, mock_get, client):
        """get_questionnaire_data retries on RagicCommunicationError then succeeds."""
        mock_get.side_effect = [
            RagicCommunicationError("RAGIC", "Connection reset"),
            RagicCommunicationError("RAGIC", "Connection reset"),
            {"customer_name": "王小明"},  # Third attempt succeeds
        ]

        result = client.get_questionnaire_data("12345")

        assert result == {"customer_name": "王小明"}
        assert mock_get.call_count == 3

    @patch("dreams_workflow.shared.ragic_client.CloudRagicClient._get")
    def test_get_questionnaire_raises_after_max_retries(self, mock_get, client):
        """get_questionnaire_data raises after exhausting all retry attempts."""
        mock_get.side_effect = RagicCommunicationError(
            "RAGIC", "Service unavailable"
        )

        with pytest.raises(RagicCommunicationError):
            client.get_questionnaire_data("12345")

        # Should have tried 3 times (initial + 2 retries = 3 total attempts)
        assert mock_get.call_count == 3

    @patch("dreams_workflow.shared.ragic_client.CloudRagicClient._post")
    def test_update_case_status_retries_on_ragic_error(self, mock_post, client):
        """update_case_status retries on RagicCommunicationError then succeeds."""
        mock_post.side_effect = [
            RagicCommunicationError("RAGIC", "Timeout"),
            {},  # Second attempt succeeds
        ]

        client.update_case_status("case-001", "安裝階段")

        assert mock_post.call_count == 2

    @patch("dreams_workflow.shared.ragic_client.CloudRagicClient._post")
    def test_write_determination_retries_on_ragic_error(self, mock_post, client):
        """write_determination_result retries on failure."""
        mock_post.side_effect = [
            RagicCommunicationError("RAGIC", "502 Bad Gateway"),
            {},  # Second attempt succeeds
        ]

        client.write_determination_result("case-001", {"status": "all_pass"})

        assert mock_post.call_count == 2

    @patch("dreams_workflow.shared.ragic_client.CloudRagicClient._post")
    def test_create_supplement_retries_on_ragic_error(self, mock_post, client):
        """create_supplement_questionnaire retries on failure."""
        mock_post.side_effect = [
            RagicCommunicationError("RAGIC", "Connection refused"),
            RagicCommunicationError("RAGIC", "Connection refused"),
            {"ragicTempRecordKey": "88888"},  # Third attempt succeeds
        ]

        url = client.create_supplement_questionnaire("case-001", ["item1"])

        assert "88888" in url
        assert mock_post.call_count == 3

    @patch("dreams_workflow.shared.ragic_client.CloudRagicClient._post")
    def test_update_case_record_raises_after_max_retries(self, mock_post, client):
        """update_case_record raises after exhausting retries."""
        mock_post.side_effect = RagicCommunicationError(
            "RAGIC", "Service unavailable"
        )

        with pytest.raises(RagicCommunicationError):
            client.update_case_record("case-001", {"field": "value"})

        assert mock_post.call_count == 3

    @patch("dreams_workflow.shared.ragic_client.CloudRagicClient._get")
    def test_no_retry_on_non_ragic_error(self, mock_get, client):
        """Non-RagicCommunicationError exceptions are not retried."""
        mock_get.side_effect = ValueError("Unexpected error")

        with pytest.raises(ValueError):
            client.get_questionnaire_data("12345")

        # Should only be called once - no retry for ValueError
        assert mock_get.call_count == 1


# =============================================================================
# Test: Context manager and session lifecycle
# =============================================================================


class TestClientLifecycle:
    """Tests for client initialization and cleanup."""

    def test_context_manager_closes_session(self):
        """Using client as context manager closes the session on exit."""
        with patch.object(CloudRagicClient, "_create_session") as mock_create:
            mock_session = MagicMock()
            mock_create.return_value = mock_session

            with CloudRagicClient(api_key="test") as client:
                pass

            mock_session.close.assert_called_once()

    def test_default_configuration_from_env(self):
        """Client reads configuration from environment variables."""
        env = {
            "RAGIC_BASE_URL": "https://custom.ragic.com",
            "RAGIC_ACCOUNT_NAME": "myaccount",
            "RAGIC_API_KEY": "my-secret-key",
            "RAGIC_TIMEOUT": "60",
        }
        with patch.dict("os.environ", env):
            client = CloudRagicClient()

        assert client.base_url == "https://custom.ragic.com"
        assert client.account_name == "myaccount"
        assert client.api_key == "my-secret-key"
        assert client.timeout == 60

    def test_explicit_params_override_env(self):
        """Explicit constructor params take precedence over env vars."""
        env = {
            "RAGIC_BASE_URL": "https://env.ragic.com",
            "RAGIC_API_KEY": "env-key",
        }
        with patch.dict("os.environ", env):
            client = CloudRagicClient(
                base_url="https://explicit.ragic.com",
                api_key="explicit-key",
            )

        assert client.base_url == "https://explicit.ragic.com"
        assert client.api_key == "explicit-key"

    def test_session_has_auth_header(self, client):
        """Session is configured with Authorization header."""
        assert "Authorization" in client._session.headers
        assert client._session.headers["Authorization"] == "Basic test-api-key"
