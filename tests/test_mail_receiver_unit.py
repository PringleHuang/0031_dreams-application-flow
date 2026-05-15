"""Unit tests for mail_receiver Lambda function.

Tests email parsing, sender matching, and status update logic.

Requirements: 6.1, 6.2, 6.3, 6.4
"""

import json
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from unittest.mock import MagicMock, patch

import pytest

from dreams_workflow.mail_receiver.app import (
    ParsedEmail,
    lambda_handler,
    match_case_by_sender,
    parse_email_content,
    _extract_s3_info,
    _process_analysis_result,
)


# =============================================================================
# Helper: build raw email bytes
# =============================================================================


def _build_simple_email(
    sender: str = "taipower@example.com",
    subject: str = "Re: 【DREAMS審核】_TEST0011-123",
    body: str = "本案核准通過。",
) -> bytes:
    """Build a simple text email."""
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = sender
    msg["To"] = "dreams-reply@example.com"
    msg["Subject"] = subject
    msg["Message-ID"] = "<test-msg-001@example.com>"
    msg["Date"] = "Wed, 07 May 2026 10:00:00 +0800"
    return msg.as_bytes()


def _build_multipart_email(
    sender: str = "taipower@example.com",
    subject: str = "Re: 【DREAMS審核】_TEST0011-456",
    body_text: str = "審核結果：駁回。原因：地址不符。",
    body_html: str = "<p>審核結果：駁回</p>",
    attachment_name: str = "result.pdf",
    attachment_content: bytes = b"fake pdf",
) -> bytes:
    """Build a multipart email with attachment."""
    msg = MIMEMultipart("mixed")
    msg["From"] = f"Taipower <{sender}>"
    msg["To"] = "dreams-reply@example.com"
    msg["Subject"] = subject
    msg["Message-ID"] = "<test-msg-002@example.com>"
    msg["Date"] = "Wed, 07 May 2026 11:00:00 +0800"

    # Text body
    text_part = MIMEText(body_text, "plain", "utf-8")
    msg.attach(text_part)

    # HTML body
    html_part = MIMEText(body_html, "html", "utf-8")
    msg.attach(html_part)

    # Attachment
    att_part = MIMEApplication(attachment_content)
    att_part.add_header("Content-Disposition", "attachment", filename=attachment_name)
    msg.attach(att_part)

    return msg.as_bytes()


# Mock config for match_case_by_sender tests
_MOCK_MAIL_CONFIG = {
    "allowed_senders": [
        "tp@example.com",
        "tp@taipower.com",
        "pringle.huang@gmail.com",
        "dreams@taipower.com.tw",
    ],
    "subject_patterns": [
        r"【DREAMS[^】]*】[_\s]*([A-Za-z0-9]+-\d+)",
        r"([A-Za-z0-9]+-\d+)",
    ],
    "email_classification": {
        "electricity_number_created": {"keywords": ["電號已手動新增"]},
        "approved": {"keywords": ["已通過審核"]},
        "rejected": {"keywords": ["未通過審核"]},
    },
}


# =============================================================================
# Tests: parse_email_content
# =============================================================================


class TestParseEmailContent:
    """Tests for parse_email_content."""

    def test_simple_text_email(self):
        """Parse a simple text-only email."""
        raw = _build_simple_email(
            sender="tp@taipower.com",
            subject="Test Subject",
            body="Hello World",
        )
        parsed = parse_email_content(raw)

        assert parsed.sender == "tp@taipower.com"
        assert parsed.subject == "Test Subject"
        assert "Hello World" in parsed.body_text
        assert parsed.attachments == []

    def test_multipart_email_with_attachment(self):
        """Parse a multipart email with text, HTML, and attachment."""
        raw = _build_multipart_email(
            sender="review@taipower.com",
            subject="審核結果",
            body_text="駁回原因：地址不符",
            body_html="<p>駁回</p>",
            attachment_name="report.pdf",
            attachment_content=b"pdf content",
        )
        parsed = parse_email_content(raw)

        assert "review@taipower.com" in parsed.sender
        assert parsed.subject == "審核結果"
        assert "駁回原因" in parsed.body_text
        assert "<p>駁回</p>" in parsed.body_html
        assert len(parsed.attachments) == 1
        assert parsed.attachments[0][0] == "report.pdf"
        assert parsed.attachments[0][1] == b"pdf content"

    def test_email_without_attachment(self):
        """Parse email with no attachments returns empty list."""
        raw = _build_simple_email(body="核准通過")
        parsed = parse_email_content(raw)

        assert parsed.attachments == []

    def test_email_with_name_angle_bracket_sender(self):
        """Parse sender in 'Name <email>' format."""
        raw = _build_multipart_email(sender="taipower@example.com")
        parsed = parse_email_content(raw)

        assert "taipower@example.com" in parsed.sender

    def test_empty_body_email(self):
        """Parse email with empty body."""
        msg = MIMEText("", "plain", "utf-8")
        msg["From"] = "test@example.com"
        msg["Subject"] = "Empty"
        raw = msg.as_bytes()

        parsed = parse_email_content(raw)
        assert parsed.body_text == ""
        assert parsed.subject == "Empty"


# =============================================================================
# Tests: match_case_by_sender
# =============================================================================


class TestMatchCaseBySender:
    """Tests for match_case_by_sender.

    The function now uses _get_mail_config() to load allowed_senders and
    subject_patterns. We mock _get_mail_config to return test config.
    """

    @patch("dreams_workflow.mail_receiver.app._get_mail_config", return_value=_MOCK_MAIL_CONFIG)
    def test_match_dreams_apply_id_in_subject(self, mock_config):
        """Match DREAMS_APPLY_ID pattern (e.g. TEST0011-123) in subject, returns ragicId=123."""
        result = match_case_by_sender("tp@example.com", "Re: 【DREAMS審核】_TEST0011-123")
        assert result == "123"

    @patch("dreams_workflow.mail_receiver.app._get_mail_config", return_value=_MOCK_MAIL_CONFIG)
    def test_match_apply_id_without_prefix(self, mock_config):
        """Match APPLY_ID pattern directly (e.g. CASE-456) in subject."""
        result = match_case_by_sender("tp@example.com", "Re: CASE-456 審核結果")
        assert result == "456"

    @patch("dreams_workflow.mail_receiver.app._get_mail_config", return_value=_MOCK_MAIL_CONFIG)
    def test_match_apply_id_with_multiple_dashes(self, mock_config):
        """Match pattern with multiple dashes, takes last segment as ragicId."""
        result = match_case_by_sender("tp@example.com", "【DREAMS審核】_ABC-DEF-789")
        assert result == "789"

    @patch("dreams_workflow.mail_receiver.app._get_mail_config", return_value=_MOCK_MAIL_CONFIG)
    def test_sender_not_in_whitelist_returns_none(self, mock_config):
        """Return None when sender is not in allowed_senders whitelist."""
        result = match_case_by_sender("random@example.com", "【DREAMS審核】_TEST0011-123")
        assert result is None

    @patch("dreams_workflow.mail_receiver.app._get_mail_config", return_value=_MOCK_MAIL_CONFIG)
    def test_no_match_in_subject_returns_none(self, mock_config):
        """Return None when no DREAMS_APPLY_ID pattern found in subject."""
        result = match_case_by_sender("tp@example.com", "Hello World no pattern here")
        assert result is None

    @patch("dreams_workflow.mail_receiver.app._get_mail_config", return_value=_MOCK_MAIL_CONFIG)
    def test_sender_with_angle_brackets(self, mock_config):
        """Handle sender in 'Name <email>' format."""
        result = match_case_by_sender(
            "Taipower <tp@taipower.com>", "Re: 【DREAMS審核】_SITE-100"
        )
        assert result == "100"

    @patch("dreams_workflow.mail_receiver.app._get_mail_config", return_value=_MOCK_MAIL_CONFIG)
    def test_sender_case_insensitive(self, mock_config):
        """Sender matching is case-insensitive."""
        result = match_case_by_sender("TP@EXAMPLE.COM", "Re: 【DREAMS審核】_TEST-200")
        assert result == "200"


# =============================================================================
# Tests: _extract_s3_info
# =============================================================================


class TestExtractS3Info:
    """Tests for _extract_s3_info."""

    def test_ses_notification_format(self):
        """Extract from SES notification event. Uses incoming/{messageId} path."""
        event = {
            "Records": [{
                "ses": {
                    "mail": {
                        "messageId": "msg-abc-123",
                        "source": "tp@example.com",
                        "commonHeaders": {"subject": "Test"},
                    },
                    "receipt": {},
                }
            }]
        }

        from dreams_workflow.mail_receiver import app
        original_bucket = app.S3_BUCKET
        app.S3_BUCKET = "my-email-bucket"
        try:
            bucket, key = _extract_s3_info(event)
        finally:
            app.S3_BUCKET = original_bucket

        assert bucket == "my-email-bucket"
        assert key == "incoming/msg-abc-123"

    def test_s3_event_format(self):
        """Extract from direct S3 event."""
        event = {
            "Records": [{
                "s3": {
                    "bucket": {"name": "email-bucket"},
                    "object": {"key": "incoming/email-001.eml"},
                }
            }]
        }

        bucket, key = _extract_s3_info(event)
        assert bucket == "email-bucket"
        assert key == "incoming/email-001.eml"

    def test_empty_event_returns_empty(self):
        """Empty event returns empty strings."""
        bucket, key = _extract_s3_info({})
        assert bucket == ""
        assert key == ""


# =============================================================================
# Tests: _process_analysis_result
# =============================================================================


class TestProcessAnalysisResult:
    """Tests for _process_analysis_result (status update after analysis)."""

    @patch("dreams_workflow.shared.ragic_client.CloudRagicClient")
    @patch("dreams_workflow.ai_determination.field_mapping_loader.get_taipower_result_mapping", return_value={})
    @patch("dreams_workflow.ai_determination.field_mapping_loader.get_status_field_id", return_value="1015456")
    def test_approved_updates_status_to_pre_send_confirm(self, mock_field_id, mock_mapping, mock_ragic_cls):
        """Approved result updates status to 發送前人工確認."""
        mock_ragic = MagicMock()
        mock_ragic_cls.return_value = mock_ragic

        analysis_result = {
            "category": "approved",
            "field_results": {},
            "rejection_reason_summary": "",
        }

        _process_analysis_result("CASE-001", analysis_result)

        mock_ragic.update_case_record.assert_called_once()
        call_args = mock_ragic.update_case_record.call_args[0]
        update_data = call_args[1]
        assert update_data["1015456"] == "發送前人工確認"

    @patch("dreams_workflow.shared.ragic_client.CloudRagicClient")
    @patch("dreams_workflow.ai_determination.field_mapping_loader.get_taipower_result_mapping", return_value={})
    @patch("dreams_workflow.ai_determination.field_mapping_loader.get_status_field_id", return_value="1015456")
    def test_rejected_writes_reason_and_updates_status(self, mock_field_id, mock_mapping, mock_ragic_cls):
        """Rejected result writes rejection reason and updates status."""
        mock_ragic = MagicMock()
        mock_ragic_cls.return_value = mock_ragic

        analysis_result = {
            "category": "rejected",
            "field_results": {},
            "rejection_reason_summary": "地址不符合",
        }

        _process_analysis_result("CASE-002", analysis_result)

        mock_ragic.update_case_record.assert_called_once()
        call_args = mock_ragic.update_case_record.call_args[0]
        update_data = call_args[1]
        assert update_data["1015456"] == "發送前人工確認"
        assert update_data["taipower_rejection_reason"] == "地址不符合"

    def test_empty_result_does_nothing(self):
        """Empty analysis result does not update RAGIC."""
        # Should not raise any exception
        _process_analysis_result("CASE-003", {})


# =============================================================================
# Tests: lambda_handler integration
# =============================================================================


class TestLambdaHandler:
    """Integration tests for the lambda_handler."""

    @patch("dreams_workflow.mail_receiver.app._handle_case_approved")
    @patch("dreams_workflow.mail_receiver.app._classify_email", return_value="approved")
    @patch("dreams_workflow.shared.ragic_client.CloudRagicClient")
    @patch("dreams_workflow.mail_receiver.app.match_case_by_sender")
    @patch("dreams_workflow.mail_receiver.app._read_email_from_s3")
    def test_full_flow_success(
        self, mock_read, mock_match, mock_ragic_cls, mock_classify, mock_handle_approved
    ):
        """Full flow: S3 → parse → match → classify → handle."""
        raw_email = _build_simple_email(
            sender="tp@taipower.com",
            subject="Re: 【DREAMS審核】_TEST-555",
            body="案場已通過審核",
        )
        mock_read.return_value = raw_email
        mock_match.return_value = "555"

        # Mock RAGIC status check
        mock_ragic = MagicMock()
        mock_ragic.get_case_record.return_value = {"1015456": "台電審核"}
        mock_ragic_cls.return_value = mock_ragic

        mock_handle_approved.return_value = {
            "statusCode": 200,
            "body": json.dumps({"case_id": "555", "action": "case_approved"}),
        }

        event = {
            "Records": [{
                "s3": {
                    "bucket": {"name": "bucket"},
                    "object": {"key": "email.eml"},
                }
            }]
        }

        result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        mock_handle_approved.assert_called_once()

    @patch("dreams_workflow.mail_receiver.app._get_mail_config", return_value=_MOCK_MAIL_CONFIG)
    @patch("dreams_workflow.mail_receiver.app._read_email_from_s3")
    def test_no_matching_case(self, mock_read, mock_config):
        """When no case matches, returns 200 with message."""
        raw_email = _build_simple_email(
            sender="unknown@example.com",
            subject="Random email",
            body="Not related",
        )
        mock_read.return_value = raw_email

        event = {
            "Records": [{
                "s3": {
                    "bucket": {"name": "bucket"},
                    "object": {"key": "email.eml"},
                }
            }]
        }

        result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["message"] == "No matching case found"

    @patch("dreams_workflow.mail_receiver.app._read_email_from_s3")
    def test_s3_read_failure(self, mock_read):
        """When S3 read fails, returns 404."""
        mock_read.return_value = None

        event = {
            "Records": [{
                "s3": {
                    "bucket": {"name": "bucket"},
                    "object": {"key": "missing.eml"},
                }
            }]
        }

        result = lambda_handler(event, None)
        assert result["statusCode"] == 404
