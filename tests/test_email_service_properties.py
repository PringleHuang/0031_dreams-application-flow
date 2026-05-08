"""Property-based tests for email service log completeness.

Property 7: 郵件發送紀錄完整性
Validates: Requirements 12.3

Uses hypothesis to generate random email send scenarios (success/failure),
verifying that:
- Every send attempt (success or failure) creates an EmailLog record
- Successful sends include sent_at and message_id
- Failed sends include error_message
- All logs contain case_id, email_type, recipient, and status
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings, strategies as st, assume

os.environ.setdefault("SES_SENDER_EMAIL", "test@example.com")
os.environ.setdefault("EMAIL_LOG_BUCKET", "test-bucket")
os.environ.setdefault("AWS_REGION", "ap-northeast-1")

from dreams_workflow.shared.models import EmailType
from dreams_workflow.email_service.app import (
    Attachment,
    EmailConfig,
    EmailLog,
    EmailRequest,
    EmailResult,
    send_email,
    _save_email_log,
    _get_email_config,
)


# =============================================================================
# Strategies
# =============================================================================

email_type_strategy = st.sampled_from(list(EmailType))

case_id_strategy = st.text(
    min_size=1,
    max_size=20,
    alphabet=st.characters(whitelist_categories=("L", "N", "Pd")),
)

email_strategy = st.emails()

message_id_strategy = st.text(
    min_size=10,
    max_size=40,
    alphabet=st.characters(whitelist_categories=("L", "N", "Pd")),
)

error_message_strategy = st.text(min_size=1, max_size=100)

retry_count_strategy = st.integers(min_value=0, max_value=3)


# =============================================================================
# Property Tests: EmailLog completeness
# =============================================================================


class TestEmailLogCompleteness:
    """Property 7: 郵件發送紀錄完整性"""

    # Feature: dreams-application-flow, Property 7: 郵件發送紀錄完整性

    @settings(max_examples=100)
    @given(
        case_id=case_id_strategy,
        email_type=email_type_strategy,
        recipient=email_strategy,
        message_id=message_id_strategy,
    )
    def test_successful_send_log_contains_required_fields(
        self,
        case_id: str,
        email_type: EmailType,
        recipient: str,
        message_id: str,
    ):
        """Successful email sends always create a log with sent_at and message_id."""
        mock_s3 = MagicMock()
        sent_at = datetime.now(timezone.utc).isoformat()

        with patch("dreams_workflow.email_service.app._get_s3_client", return_value=mock_s3):
            _save_email_log(
                case_id=case_id,
                email_type=email_type,
                recipient=recipient,
                status="sent",
                message_id=message_id,
                sent_at=sent_at,
            )

        # Verify S3 put_object was called
        mock_s3.put_object.assert_called_once()
        call_kwargs = mock_s3.put_object.call_args[1]

        # Parse the stored JSON
        log_data = json.loads(call_kwargs["Body"])

        # Required fields present
        assert log_data["case_id"] == case_id
        assert log_data["email_type"] == email_type.value
        assert log_data["recipient"] == recipient
        assert log_data["status"] == "sent"

        # Success-specific fields
        assert log_data["sent_at"] == sent_at
        assert log_data["message_id"] == message_id

        # S3 key format
        assert call_kwargs["Key"].startswith(f"email-logs/{case_id}/")
        assert call_kwargs["Key"].endswith(".json")

    @settings(max_examples=100)
    @given(
        case_id=case_id_strategy,
        email_type=email_type_strategy,
        recipient=email_strategy,
        error_message=error_message_strategy,
        retry_count=retry_count_strategy,
    )
    def test_failed_send_log_contains_error_info(
        self,
        case_id: str,
        email_type: EmailType,
        recipient: str,
        error_message: str,
        retry_count: int,
    ):
        """Failed email sends always create a log with error_message."""
        mock_s3 = MagicMock()

        with patch("dreams_workflow.email_service.app._get_s3_client", return_value=mock_s3):
            _save_email_log(
                case_id=case_id,
                email_type=email_type,
                recipient=recipient,
                status="failed",
                error_message=error_message,
                retry_count=retry_count,
            )

        mock_s3.put_object.assert_called_once()
        call_kwargs = mock_s3.put_object.call_args[1]
        log_data = json.loads(call_kwargs["Body"])

        # Required fields present
        assert log_data["case_id"] == case_id
        assert log_data["email_type"] == email_type.value
        assert log_data["recipient"] == recipient
        assert log_data["status"] == "failed"

        # Failure-specific fields
        assert log_data["error_message"] == error_message
        assert log_data["retry_count"] == retry_count

        # sent_at and message_id should be None for failures
        assert log_data["sent_at"] is None
        assert log_data["message_id"] is None

    @settings(max_examples=100)
    @given(
        case_id=case_id_strategy,
        email_type=email_type_strategy,
        recipient=email_strategy,
    )
    def test_every_log_has_unique_id(
        self,
        case_id: str,
        email_type: EmailType,
        recipient: str,
    ):
        """Every email log record has a unique log_id (UUID)."""
        mock_s3 = MagicMock()
        log_ids = []

        with patch("dreams_workflow.email_service.app._get_s3_client", return_value=mock_s3):
            # Create two logs for the same case
            _save_email_log(
                case_id=case_id,
                email_type=email_type,
                recipient=recipient,
                status="sent",
                message_id="msg-1",
                sent_at="2025-01-01T00:00:00+00:00",
            )
            _save_email_log(
                case_id=case_id,
                email_type=email_type,
                recipient=recipient,
                status="failed",
                error_message="timeout",
            )

        # Extract log_ids from both calls
        for call in mock_s3.put_object.call_args_list:
            log_data = json.loads(call[1]["Body"])
            log_ids.append(log_data["log_id"])

        # All log_ids should be unique
        assert len(log_ids) == 2
        assert log_ids[0] != log_ids[1]

    @settings(max_examples=50)
    @given(
        case_id=case_id_strategy,
        email_type=email_type_strategy,
        recipient=email_strategy,
    )
    def test_log_stored_in_correct_s3_path(
        self,
        case_id: str,
        email_type: EmailType,
        recipient: str,
    ):
        """Email logs are stored under email-logs/{case_id}/ in S3."""
        mock_s3 = MagicMock()

        with patch("dreams_workflow.email_service.app._get_s3_client", return_value=mock_s3):
            _save_email_log(
                case_id=case_id,
                email_type=email_type,
                recipient=recipient,
                status="sent",
                message_id="msg-test",
                sent_at="2025-01-01T00:00:00+00:00",
            )

        call_kwargs = mock_s3.put_object.call_args[1]
        assert call_kwargs["Bucket"] == "test-bucket"
        assert call_kwargs["Key"].startswith(f"email-logs/{case_id}/")
        assert call_kwargs["ContentType"] == "application/json"


class TestSendEmailCreatesLog:
    """Property 7: send_email always creates a log regardless of outcome."""

    # Feature: dreams-application-flow, Property 7: 郵件發送紀錄完整性

    @settings(max_examples=50)
    @given(
        case_id=case_id_strategy,
        email_type=st.sampled_from([
            EmailType.QUESTIONNAIRE_NOTIFICATION,
            EmailType.SUPPLEMENT_NOTIFICATION,
            EmailType.APPROVAL_NOTIFICATION,
            EmailType.ACCOUNT_ACTIVATION,
        ]),
        recipient=email_strategy,
    )
    def test_successful_send_creates_log(
        self,
        case_id: str,
        email_type: EmailType,
        recipient: str,
    ):
        """A successful send_email call always persists an EmailLog with status=sent."""
        mock_ses = MagicMock()
        mock_ses.send_email.return_value = {"MessageId": "ses-msg-12345"}
        mock_s3 = MagicMock()

        with (
            patch("dreams_workflow.email_service.app._get_ses_client", return_value=mock_ses),
            patch("dreams_workflow.email_service.app._get_s3_client", return_value=mock_s3),
        ):
            request = EmailRequest(
                email_type=email_type,
                case_id=case_id,
                recipient_email=recipient,
                template_data={"case_id": case_id},
            )
            result = send_email(request)

        assert result.success is True
        assert result.message_id == "ses-msg-12345"
        assert result.sent_at is not None

        # Verify log was saved
        mock_s3.put_object.assert_called_once()
        log_data = json.loads(mock_s3.put_object.call_args[1]["Body"])
        assert log_data["status"] == "sent"
        assert log_data["case_id"] == case_id
        assert log_data["message_id"] == "ses-msg-12345"

    @settings(max_examples=50)
    @given(
        case_id=case_id_strategy,
        recipient=email_strategy,
        error_msg=error_message_strategy,
    )
    def test_failed_send_creates_log(
        self,
        case_id: str,
        recipient: str,
        error_msg: str,
    ):
        """A failed send_email call always persists an EmailLog with status=failed."""
        mock_ses = MagicMock()
        mock_ses.send_email.side_effect = Exception(error_msg)
        mock_s3 = MagicMock()

        with (
            patch("dreams_workflow.email_service.app._get_ses_client", return_value=mock_ses),
            patch("dreams_workflow.email_service.app._get_s3_client", return_value=mock_s3),
            pytest.raises(Exception),
        ):
            request = EmailRequest(
                email_type=EmailType.QUESTIONNAIRE_NOTIFICATION,
                case_id=case_id,
                recipient_email=recipient,
                template_data={"case_id": case_id},
            )
            send_email.__wrapped__(request)  # bypass retry decorator

        # Verify failure log was saved
        mock_s3.put_object.assert_called_once()
        log_data = json.loads(mock_s3.put_object.call_args[1]["Body"])
        assert log_data["status"] == "failed"
        assert log_data["case_id"] == case_id
        assert log_data["error_message"] is not None
