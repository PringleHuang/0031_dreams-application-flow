"""Unit tests for Email Service (email_service/app.py).

Covers:
- 各類型郵件發送（6 種）
- 附件發送
- 發送失敗重試
- 配置載入與連結組裝
- Lambda handler 入口

Requirements: 12.1, 12.4
"""

from __future__ import annotations

import base64
import json
import os
from unittest.mock import MagicMock, patch, call

import pytest

os.environ.setdefault("SES_SENDER_EMAIL", "noreply@dreams.example.com")
os.environ.setdefault("EMAIL_LOG_BUCKET", "test-email-logs")
os.environ.setdefault("AWS_REGION", "ap-northeast-1")

from dreams_workflow.shared.exceptions import EmailSendError
from dreams_workflow.shared.models import EmailType
from dreams_workflow.email_service.app import (
    Attachment,
    EmailConfig,
    EmailRequest,
    EmailResult,
    lambda_handler,
    send_email,
    _send_simple_email,
    _send_raw_email,
    get_recipient_email,
)


# =============================================================================
# Helpers
# =============================================================================


def _mock_ses_success(message_id: str = "test-msg-001") -> MagicMock:
    """Create a mock SES client that returns success."""
    mock = MagicMock()
    mock.send_email.return_value = {"MessageId": message_id}
    mock.send_raw_email.return_value = {"MessageId": message_id}
    return mock


def _mock_ses_failure(error_msg: str = "Service unavailable") -> MagicMock:
    """Create a mock SES client that raises an exception."""
    mock = MagicMock()
    mock.send_email.side_effect = Exception(error_msg)
    mock.send_raw_email.side_effect = Exception(error_msg)
    return mock


# =============================================================================
# Test: EmailConfig
# =============================================================================


class TestEmailConfig:
    """Tests for EmailConfig loading and link building."""

    def test_load_config_all_templates_present(self):
        """Config should contain all 6 email type templates."""
        config = EmailConfig()
        for email_type in EmailType:
            tc = config.get_template_config(email_type)
            assert tc is not None, f"Missing config for {email_type.value}"
            assert "subject" in tc
            assert "template_file" in tc

    def test_sender_email_from_env(self):
        """Sender email resolves from environment variable."""
        with patch.dict(os.environ, {"SES_SENDER_EMAIL": "noreply@dreams.example.com"}):
            config = EmailConfig()
            assert config.sender_email == "noreply@dreams.example.com"

    def test_build_link_url_with_static_and_dynamic_params(self):
        """Link URL includes both static and dynamic parameters."""
        config = EmailConfig()
        link_config = {
            "base_url": "https://example.com/form",
            "static_params": {"mode": "embed", "version": "2"},
            "dynamic_params": {"record_id": "case_id", "name": "customer_name"},
        }
        template_data = {"case_id": "CASE-100", "customer_name": "王小明"}

        url = config.build_link_url(link_config, template_data)

        assert "https://example.com/form?" in url
        assert "mode=embed" in url
        assert "version=2" in url
        assert "record_id=CASE-100" in url
        # URL-encoded Chinese characters
        assert "name=" in url

    def test_build_link_url_skips_empty_dynamic_values(self):
        """Dynamic params with empty values are not included in URL."""
        config = EmailConfig()
        link_config = {
            "base_url": "https://example.com/form",
            "static_params": {},
            "dynamic_params": {"id": "case_id", "extra": "missing_key"},
        }
        template_data = {"case_id": "CASE-200"}

        url = config.build_link_url(link_config, template_data)

        assert "id=CASE-200" in url
        assert "extra" not in url

    def test_build_link_url_no_params(self):
        """Link with no params returns base URL only."""
        config = EmailConfig()
        link_config = {
            "base_url": "https://example.com/login",
            "static_params": {},
            "dynamic_params": {},
        }

        url = config.build_link_url(link_config, {})
        assert url == "https://example.com/login"

    def test_render_subject_with_placeholders(self):
        """Subject template renders with template_data values."""
        config = EmailConfig()
        subject = config.render_subject(
            "DREAMS 台電站點申請 - {case_id}",
            {"case_id": "CASE-300"},
        )
        assert subject == "DREAMS 台電站點申請 - CASE-300"

    def test_render_subject_missing_key_returns_original(self):
        """Subject with missing key returns the template unchanged."""
        config = EmailConfig()
        subject = config.render_subject(
            "DREAMS 申請 - {missing_key}",
            {"case_id": "CASE-400"},
        )
        assert subject == "DREAMS 申請 - {missing_key}"

    def test_render_template_questionnaire(self):
        """Questionnaire notification template renders with URL."""
        config = EmailConfig()
        html = config.render_template(
            "questionnaire_notification.html",
            {"questionnaire_url": "https://example.com/form?id=123", "case_id": "C-1"},
        )
        assert "問卷網址連結" in html
        assert "https://example.com/form?id=123" in html

    def test_render_template_supplement_with_failed_items(self):
        """Supplement notification template renders comparison table and document checklist."""
        config = EmailConfig()
        html = config.render_template(
            "supplement_notification.html",
            {
                "case_id": "C-2",
                "questionnaire_url": "https://example.com/supplement",
                "failed_table": [
                    {
                        "field_name": "案場詳細地址",
                        "provided_value": "高雄市大寮區上發一路36號",
                        "doc_values": ["高雄市大寮區大寮段二小段1161地號", "", "", "", ""],
                    },
                    {
                        "field_name": "責任分界點電壓",
                        "provided_value": "11.4kV",
                        "doc_values": ["22.8kV", "22.8kV", "", "", ""],
                    },
                ],
                "doc_columns": ["審訖圖", "細部協商", "縣府同意備案函文", "購售電契約", "併聯審查意見書"],
                "failed_documents": [
                    {"name": "審訖圖", "check": ""},
                    {"name": "細部協商", "check": "V"},
                    {"name": "縣府同意備案函文", "check": "V"},
                    {"name": "購(躉)售電契約書", "check": ""},
                    {"name": "併聯審查意見書", "check": ""},
                ],
            },
        )
        # Comparison table
        assert "案場詳細地址" in html
        assert "責任分界點電壓" in html
        assert "高雄市大寮區上發一路36號" in html
        assert "22.8kV" in html
        # Document checklist
        assert "佐証文件提供" in html
        assert "細部協商" in html
        assert "問卷網址連結" in html


# =============================================================================
# Test: 各類型郵件發送 (Requirements 12.1)
# =============================================================================


class TestSendEmailTypes:
    """Tests for sending each of the 6 email types."""

    def _send_with_mock(self, email_type: EmailType, template_data: dict | None = None):
        """Helper to send an email with mocked SES and S3."""
        mock_ses = _mock_ses_success("msg-type-test")
        mock_s3 = MagicMock()

        with (
            patch("dreams_workflow.email_service.app._get_ses_client", return_value=mock_ses),
            patch("dreams_workflow.email_service.app._get_s3_client", return_value=mock_s3),
        ):
            request = EmailRequest(
                email_type=email_type,
                case_id="TYPE-TEST-001",
                recipient_email="customer@example.com",
                template_data=template_data or {"case_id": "TYPE-TEST-001"},
            )
            result = send_email(request)

        return result, mock_ses

    def test_send_questionnaire_notification(self):
        """問卷通知 email sends successfully."""
        result, mock_ses = self._send_with_mock(EmailType.QUESTIONNAIRE_NOTIFICATION)
        assert result.success is True
        assert result.message_id == "msg-type-test"
        mock_ses.send_email.assert_called_once()

    def test_send_supplement_notification(self):
        """補件通知 email sends successfully."""
        result, mock_ses = self._send_with_mock(
            EmailType.SUPPLEMENT_NOTIFICATION,
            {"case_id": "TYPE-TEST-001", "failed_items": ["文件A", "文件B"]},
        )
        assert result.success is True
        mock_ses.send_email.assert_called_once()

    def test_send_taipower_application(self):
        """台電審核申請 email sends successfully."""
        result, mock_ses = self._send_with_mock(
            EmailType.TAIPOWER_APPLICATION,
            {
                "case_id": "TYPE-TEST-001",
                "electricity_number": "06-1234-5678",
                "customer_name": "測試客戶",
                "document_count": 5,
            },
        )
        assert result.success is True
        mock_ses.send_email.assert_called_once()

    def test_send_taipower_supplement(self):
        """台電補件通知 email sends successfully."""
        result, mock_ses = self._send_with_mock(
            EmailType.TAIPOWER_SUPPLEMENT,
            {"case_id": "TYPE-TEST-001", "rejection_reason": "電號資料不符"},
        )
        assert result.success is True
        mock_ses.send_email.assert_called_once()

    def test_send_approval_notification(self):
        """核准通知 email sends successfully."""
        result, mock_ses = self._send_with_mock(EmailType.APPROVAL_NOTIFICATION)
        assert result.success is True
        mock_ses.send_email.assert_called_once()

    def test_send_account_activation(self):
        """帳號啟用通知 email sends successfully."""
        result, mock_ses = self._send_with_mock(
            EmailType.ACCOUNT_ACTIVATION,
            {
                "case_id": "TYPE-TEST-001",
                "account": "user@sunveillance.com",
                "initial_password": "Temp1234!",
                "login_url": "https://sunveillance.example.com/login",
            },
        )
        assert result.success is True
        mock_ses.send_email.assert_called_once()


# =============================================================================
# Test: 附件發送 (Requirements 12.1)
# =============================================================================


class TestSendEmailWithAttachments:
    """Tests for email sending with file attachments."""

    def test_send_with_single_attachment(self):
        """Email with one attachment uses raw MIME sending."""
        mock_ses = _mock_ses_success("msg-att-001")
        mock_s3 = MagicMock()

        with (
            patch("dreams_workflow.email_service.app._get_ses_client", return_value=mock_ses),
            patch("dreams_workflow.email_service.app._get_s3_client", return_value=mock_s3),
        ):
            request = EmailRequest(
                email_type=EmailType.TAIPOWER_APPLICATION,
                case_id="ATT-001",
                recipient_email="taipower@example.com",
                template_data={
                    "case_id": "ATT-001",
                    "electricity_number": "06-9999-0000",
                    "customer_name": "附件測試",
                    "document_count": 1,
                },
                attachments=[
                    Attachment(
                        filename="application.pdf",
                        content=b"%PDF-1.4 fake content",
                        content_type="application/pdf",
                    )
                ],
            )
            result = send_email(request)

        assert result.success is True
        # Should use send_raw_email for attachments
        mock_ses.send_raw_email.assert_called_once()
        mock_ses.send_email.assert_not_called()

    def test_send_with_multiple_attachments(self):
        """Email with multiple attachments includes all in MIME message."""
        mock_ses = _mock_ses_success("msg-att-002")
        mock_s3 = MagicMock()

        with (
            patch("dreams_workflow.email_service.app._get_ses_client", return_value=mock_ses),
            patch("dreams_workflow.email_service.app._get_s3_client", return_value=mock_s3),
        ):
            request = EmailRequest(
                email_type=EmailType.TAIPOWER_APPLICATION,
                case_id="ATT-002",
                recipient_email="taipower@example.com",
                template_data={
                    "case_id": "ATT-002",
                    "electricity_number": "06-1111-2222",
                    "customer_name": "多附件測試",
                    "document_count": 3,
                },
                attachments=[
                    Attachment(filename="doc1.pdf", content=b"pdf1"),
                    Attachment(filename="doc2.pdf", content=b"pdf2"),
                    Attachment(filename="doc3.pdf", content=b"pdf3"),
                ],
            )
            result = send_email(request)

        assert result.success is True
        mock_ses.send_raw_email.assert_called_once()

        # Verify raw message contains all attachment filenames
        raw_msg = mock_ses.send_raw_email.call_args[1]["RawMessage"]["Data"]
        assert "doc1.pdf" in raw_msg
        assert "doc2.pdf" in raw_msg
        assert "doc3.pdf" in raw_msg


# =============================================================================
# Test: 發送失敗重試 (Requirements 12.4)
# =============================================================================


class TestSendEmailRetry:
    """Tests for email send failure and retry behavior."""

    def test_send_failure_raises_email_send_error(self):
        """SES failure raises EmailSendError."""
        mock_ses = _mock_ses_failure("Throttling")
        mock_s3 = MagicMock()

        with (
            patch("dreams_workflow.email_service.app._get_ses_client", return_value=mock_ses),
            patch("dreams_workflow.email_service.app._get_s3_client", return_value=mock_s3),
            pytest.raises(EmailSendError) as exc_info,
        ):
            request = EmailRequest(
                email_type=EmailType.QUESTIONNAIRE_NOTIFICATION,
                case_id="RETRY-001",
                recipient_email="customer@example.com",
                template_data={"case_id": "RETRY-001"},
            )
            # Call the unwrapped function to avoid actual retry delays
            send_email.__wrapped__(request)

        assert "SES" in str(exc_info.value)

    def test_send_failure_logs_to_s3(self):
        """Failed send still creates an email log record."""
        mock_ses = _mock_ses_failure("Connection timeout")
        mock_s3 = MagicMock()

        with (
            patch("dreams_workflow.email_service.app._get_ses_client", return_value=mock_ses),
            patch("dreams_workflow.email_service.app._get_s3_client", return_value=mock_s3),
            pytest.raises(EmailSendError),
        ):
            request = EmailRequest(
                email_type=EmailType.SUPPLEMENT_NOTIFICATION,
                case_id="RETRY-002",
                recipient_email="customer@example.com",
                template_data={"case_id": "RETRY-002", "failed_items": ["item1"]},
            )
            send_email.__wrapped__(request)

        # Verify failure log was saved
        mock_s3.put_object.assert_called_once()
        log_data = json.loads(mock_s3.put_object.call_args[1]["Body"])
        assert log_data["status"] == "failed"
        assert log_data["case_id"] == "RETRY-002"
        assert "Connection timeout" in log_data["error_message"]

    def test_s3_log_failure_does_not_break_email_send(self):
        """S3 log persistence failure does not affect email send result."""
        mock_ses = _mock_ses_success("msg-s3-fail")
        mock_s3 = MagicMock()
        mock_s3.put_object.side_effect = Exception("S3 unavailable")

        with (
            patch("dreams_workflow.email_service.app._get_ses_client", return_value=mock_ses),
            patch("dreams_workflow.email_service.app._get_s3_client", return_value=mock_s3),
        ):
            request = EmailRequest(
                email_type=EmailType.QUESTIONNAIRE_NOTIFICATION,
                case_id="S3-FAIL-001",
                recipient_email="customer@example.com",
                template_data={"case_id": "S3-FAIL-001"},
            )
            result = send_email(request)

        # Email should still succeed even if S3 log fails
        assert result.success is True
        assert result.message_id == "msg-s3-fail"


# =============================================================================
# Test: Lambda Handler
# =============================================================================


class TestLambdaHandler:
    """Tests for the email service Lambda handler entry point."""

    def test_handler_success(self):
        """Lambda handler returns 200 on successful send."""
        mock_ses = _mock_ses_success("handler-msg-001")
        mock_s3 = MagicMock()

        with (
            patch("dreams_workflow.email_service.app._get_ses_client", return_value=mock_ses),
            patch("dreams_workflow.email_service.app._get_s3_client", return_value=mock_s3),
        ):
            event = {
                "email_type": "問卷通知",
                "case_id": "HANDLER-001",
                "recipient_email": "test@example.com",
                "template_data": {"case_id": "HANDLER-001"},
            }
            response = lambda_handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["success"] is True
        assert body["message_id"] == "handler-msg-001"

    def test_handler_with_attachments(self):
        """Lambda handler handles base64-encoded attachments."""
        mock_ses = _mock_ses_success("handler-msg-002")
        mock_s3 = MagicMock()

        content_b64 = base64.b64encode(b"fake pdf content").decode()

        with (
            patch("dreams_workflow.email_service.app._get_ses_client", return_value=mock_ses),
            patch("dreams_workflow.email_service.app._get_s3_client", return_value=mock_s3),
        ):
            event = {
                "email_type": "台電審核申請",
                "case_id": "HANDLER-002",
                "recipient_email": "taipower@example.com",
                "template_data": {
                    "case_id": "HANDLER-002",
                    "electricity_number": "06-0000-1111",
                    "customer_name": "Handler測試",
                    "document_count": 1,
                },
                "attachments": [
                    {
                        "filename": "test.pdf",
                        "content_base64": content_b64,
                        "content_type": "application/pdf",
                    }
                ],
            }
            response = lambda_handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["success"] is True

    def test_handler_unknown_email_type(self):
        """Lambda handler returns 400 for unknown email type."""
        event = {
            "email_type": "不存在的類型",
            "case_id": "HANDLER-003",
            "recipient_email": "test@example.com",
            "template_data": {},
        }
        response = lambda_handler(event, None)
        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert "Unknown email type" in body["error"]

    def test_handler_ses_failure_returns_500(self):
        """Lambda handler returns 500 when SES fails after retries."""
        mock_ses = _mock_ses_failure("SES down")
        mock_s3 = MagicMock()

        with (
            patch("dreams_workflow.email_service.app._get_ses_client", return_value=mock_ses),
            patch("dreams_workflow.email_service.app._get_s3_client", return_value=mock_s3),
            patch("dreams_workflow.email_service.app.send_email") as mock_send,
        ):
            mock_send.side_effect = EmailSendError("SES", "SES down")
            event = {
                "email_type": "問卷通知",
                "case_id": "HANDLER-004",
                "recipient_email": "test@example.com",
                "template_data": {"case_id": "HANDLER-004"},
            }
            response = lambda_handler(event, None)

        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert body["success"] is False
