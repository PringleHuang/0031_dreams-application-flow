"""Integration tests for DREAMS workflow system.

Tests end-to-end flows across multiple components:
1. Webhook -> Lambda trigger chain
2. SES email receipt -> mail_receiver processing chain
3. Complete new contract case flow (creation to closure)
4. Complete renewal case flow (creation to closure)

Uses moto for AWS (SES, S3), unittest.mock for Lambda client and RAGIC client.

Requirements: 1.1~1.7, 2.1~2.9, 16.1~16.4
"""

from __future__ import annotations

import json
import os
from email.mime.text import MIMEText
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from dreams_workflow.shared.models import CaseStatus, CaseType, EmailType, WebhookEventType


# =============================================================================
# Environment setup
# =============================================================================

TEST_ENV = {
    "AI_DETERMINATION_FUNCTION_NAME": "test-ai-determination",
    "WORKFLOW_ENGINE_FUNCTION_NAME": "test-workflow-engine",
    "EMAIL_SERVICE_FUNCTION_NAME": "test-email-service",
    "SES_EMAIL_BUCKET": "test-ses-email-bucket",
    "EMAIL_LOG_BUCKET": "test-email-log-bucket",
    "AWS_REGION": "ap-northeast-1",
    "AWS_DEFAULT_REGION": "ap-northeast-1",
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SECURITY_TOKEN": "testing",
    "AWS_SESSION_TOKEN": "testing",
    "RAGIC_API_KEY": "test-api-key",
    "RAGIC_BASE_URL": "https://ap13.ragic.com",
}


@pytest.fixture(autouse=True)
def set_env(monkeypatch):
    """Set environment variables for all tests."""
    for key, value in TEST_ENV.items():
        monkeypatch.setenv(key, value)


# =============================================================================
# Test 1: Webhook -> Lambda trigger chain
# =============================================================================


class TestWebhookLambdaTriggerChain:
    """Test Webhook reception -> event classification -> Lambda invocation."""

    def test_new_case_webhook_invokes_workflow_engine(self):
        """Webhook with new case event invokes workflow_engine Lambda."""
        mock_lambda = MagicMock()
        mock_lambda.invoke.return_value = {"StatusCode": 202}

        with patch(
            "dreams_workflow.webhook_handler.app._get_lambda_client",
            return_value=mock_lambda,
        ), patch(
            "dreams_workflow.webhook_handler.app.AI_DETERMINATION_FUNCTION",
            "test-ai-determination",
        ), patch(
            "dreams_workflow.webhook_handler.app.WORKFLOW_ENGINE_FUNCTION",
            "test-workflow-engine",
        ):
            from dreams_workflow.webhook_handler.app import lambda_handler

            event = {
                "headers": {},
                "body": json.dumps({
                    "form_path": "business-process2/2",
                    "action": "create",
                    "case_id": "INT-001",
                    "customer_email": "customer@example.com",
                    "case_status": "新開案件",
                }),
                "isBase64Encoded": False,
            }

            response = lambda_handler(event, None)

            assert response["statusCode"] == 200
            body = json.loads(response["body"])
            assert body["event_type"] == "NEW_CASE_CREATED"

            # Verify workflow_engine was invoked
            mock_lambda.invoke.assert_called_once()
            call_kwargs = mock_lambda.invoke.call_args[1]
            assert call_kwargs["FunctionName"] == "test-workflow-engine"
            assert call_kwargs["InvocationType"] == "Event"

            # Verify payload passed to workflow_engine
            invoke_payload = json.loads(call_kwargs["Payload"].decode("utf-8"))
            assert invoke_payload["event_type"] == "NEW_CASE_CREATED"
            assert invoke_payload["case_id"] == "INT-001"
            assert invoke_payload["payload"]["customer_email"] == "customer@example.com"

    def test_new_contract_questionnaire_invokes_ai_determination(self):
        """Webhook with new contract questionnaire invokes ai_determination Lambda."""
        mock_lambda = MagicMock()
        mock_lambda.invoke.return_value = {"StatusCode": 202}

        with patch(
            "dreams_workflow.webhook_handler.app._get_lambda_client",
            return_value=mock_lambda,
        ), patch(
            "dreams_workflow.webhook_handler.app.AI_DETERMINATION_FUNCTION",
            "test-ai-determination",
        ), patch(
            "dreams_workflow.webhook_handler.app.WORKFLOW_ENGINE_FUNCTION",
            "test-workflow-engine",
        ):
            from dreams_workflow.webhook_handler.app import lambda_handler

            event = {
                "headers": {},
                "body": json.dumps({
                    "form_path": "work-survey/7",
                    "case_type": "新約",
                    "case_id": "INT-002",
                    "1016557": "DREAMS-INT-002",
                }),
                "isBase64Encoded": False,
            }

            response = lambda_handler(event, None)

            assert response["statusCode"] == 200
            body = json.loads(response["body"])
            assert body["event_type"] == "NEW_CONTRACT_FULL_QUESTIONNAIRE"

            call_kwargs = mock_lambda.invoke.call_args[1]
            assert call_kwargs["FunctionName"] == "test-ai-determination"

    def test_renewal_questionnaire_invokes_workflow_engine(self):
        """Webhook with renewal questionnaire invokes workflow_engine Lambda."""
        mock_lambda = MagicMock()
        mock_lambda.invoke.return_value = {"StatusCode": 202}

        with patch(
            "dreams_workflow.webhook_handler.app._get_lambda_client",
            return_value=mock_lambda,
        ), patch(
            "dreams_workflow.webhook_handler.app.AI_DETERMINATION_FUNCTION",
            "test-ai-determination",
        ), patch(
            "dreams_workflow.webhook_handler.app.WORKFLOW_ENGINE_FUNCTION",
            "test-workflow-engine",
        ):
            from dreams_workflow.webhook_handler.app import lambda_handler

            event = {
                "headers": {},
                "body": json.dumps({
                    "form_path": "work-survey/7",
                    "case_type": "續約",
                    "case_id": "INT-003",
                }),
                "isBase64Encoded": False,
            }

            response = lambda_handler(event, None)

            assert response["statusCode"] == 200
            body = json.loads(response["body"])
            assert body["event_type"] == "RENEWAL_QUESTIONNAIRE"

            call_kwargs = mock_lambda.invoke.call_args[1]
            assert call_kwargs["FunctionName"] == "test-workflow-engine"

    def test_status_change_webhook_invokes_workflow_engine(self):
        """Webhook with status change event invokes workflow_engine Lambda."""
        mock_lambda = MagicMock()
        mock_lambda.invoke.return_value = {"StatusCode": 202}

        with patch(
            "dreams_workflow.webhook_handler.app._get_lambda_client",
            return_value=mock_lambda,
        ), patch(
            "dreams_workflow.webhook_handler.app.AI_DETERMINATION_FUNCTION",
            "test-ai-determination",
        ), patch(
            "dreams_workflow.webhook_handler.app.WORKFLOW_ENGINE_FUNCTION",
            "test-workflow-engine",
        ):
            from dreams_workflow.webhook_handler.app import lambda_handler

            event = {
                "headers": {},
                "body": json.dumps({
                    "form_path": "business-process2/2",
                    "action": "update",
                    "case_id": "INT-004",
                    "case_status": "台電審核",
                }),
                "isBase64Encoded": False,
            }

            response = lambda_handler(event, None)

            assert response["statusCode"] == 200
            body = json.loads(response["body"])
            assert body["event_type"] == "CASE_STATUS_CHANGED"

            call_kwargs = mock_lambda.invoke.call_args[1]
            assert call_kwargs["FunctionName"] == "test-workflow-engine"


# =============================================================================
# Test 2: SES email receipt -> mail_receiver processing chain
# =============================================================================


class TestSESEmailReceiptChain:
    """Test SES email receipt -> S3 storage -> mail_receiver parsing -> AI analysis."""

    @mock_aws
    def test_ses_email_receipt_to_mail_receiver_processing(self):
        """Full chain: SES stores email in S3 -> mail_receiver reads and processes."""
        # Setup: Create S3 bucket and store a raw email
        s3_client = boto3.client("s3", region_name="ap-northeast-1")
        s3_client.create_bucket(
            Bucket="test-ses-email-bucket",
            CreateBucketConfiguration={"LocationConstraint": "ap-northeast-1"},
        )

        # Build a raw email from "Taipower"
        raw_email = MIMEText("本案核准通過，請進行後續安裝作業。", "plain", "utf-8")
        raw_email["From"] = "taipower@taipower.com.tw"
        raw_email["To"] = "dreams-reply@company.com"
        raw_email["Subject"] = "Re: 台電站點申請 - CASE-100"
        raw_email["Message-ID"] = "<msg-ses-001@taipower.com.tw>"
        raw_email["Date"] = "Mon, 01 Jun 2026 09:00:00 +0800"

        # Store in S3 (simulating SES receipt rule)
        s3_client.put_object(
            Bucket="test-ses-email-bucket",
            Key="emails/msg-ses-001",
            Body=raw_email.as_bytes(),
        )

        # Mock the Lambda client for AI determination invocation
        mock_lambda = MagicMock()
        mock_lambda.invoke.return_value = {
            "StatusCode": 200,
            "Payload": MagicMock(
                read=MagicMock(return_value=json.dumps({
                    "statusCode": 200,
                    "body": json.dumps({
                        "category": "approved",
                        "field_results": {},
                        "rejection_reason_summary": "",
                    }),
                }).encode("utf-8"))
            ),
        }

        # Mock RAGIC client for status update
        mock_ragic = MagicMock()
        mock_ragic.update_case_record = MagicMock()
        mock_ragic.close = MagicMock()

        with patch(
            "dreams_workflow.mail_receiver.app._get_s3_client",
            return_value=s3_client,
        ), patch(
            "dreams_workflow.mail_receiver.app._get_lambda_client",
            return_value=mock_lambda,
        ), patch(
            "dreams_workflow.mail_receiver.app.S3_BUCKET",
            "test-ses-email-bucket",
        ), patch(
            "dreams_workflow.mail_receiver.app.AI_DETERMINATION_FUNCTION",
            "test-ai-determination",
        ), patch(
            "dreams_workflow.shared.ragic_client.CloudRagicClient",
            return_value=mock_ragic,
        ):
            from dreams_workflow.mail_receiver.app import lambda_handler

            # Simulate SES notification event
            ses_event = {
                "Records": [{
                    "ses": {
                        "mail": {
                            "messageId": "msg-ses-001",
                            "source": "taipower@taipower.com.tw",
                            "commonHeaders": {
                                "subject": "Re: 台電站點申請 - CASE-100",
                            },
                        },
                        "receipt": {},
                    }
                }]
            }

            result = lambda_handler(ses_event, None)

            assert result["statusCode"] == 200
            body = json.loads(result["body"])
            assert body["case_id"] == "100"
            assert body["action"] == "email_processed"

            # Verify AI determination was invoked
            mock_lambda.invoke.assert_called_once()
            call_kwargs = mock_lambda.invoke.call_args[1]
            assert call_kwargs["FunctionName"] == "test-ai-determination"
            assert call_kwargs["InvocationType"] == "RequestResponse"

    @mock_aws
    def test_ses_email_no_matching_case(self):
        """Email from unknown sender with no case ID returns no match."""
        s3_client = boto3.client("s3", region_name="ap-northeast-1")
        s3_client.create_bucket(
            Bucket="test-ses-email-bucket",
            CreateBucketConfiguration={"LocationConstraint": "ap-northeast-1"},
        )

        raw_email = MIMEText("Random email content", "plain", "utf-8")
        raw_email["From"] = "unknown@random.com"
        raw_email["To"] = "dreams-reply@company.com"
        raw_email["Subject"] = "Hello there"
        raw_email["Message-ID"] = "<msg-unknown@random.com>"

        s3_client.put_object(
            Bucket="test-ses-email-bucket",
            Key="emails/msg-unknown-001",
            Body=raw_email.as_bytes(),
        )

        with patch(
            "dreams_workflow.mail_receiver.app._get_s3_client",
            return_value=s3_client,
        ), patch(
            "dreams_workflow.mail_receiver.app.S3_BUCKET",
            "test-ses-email-bucket",
        ):
            from dreams_workflow.mail_receiver.app import lambda_handler

            ses_event = {
                "Records": [{
                    "ses": {
                        "mail": {
                            "messageId": "msg-unknown-001",
                            "source": "unknown@random.com",
                            "commonHeaders": {"subject": "Hello there"},
                        },
                        "receipt": {},
                    }
                }]
            }

            result = lambda_handler(ses_event, None)

            assert result["statusCode"] == 200
            body = json.loads(result["body"])
            assert body["message"] == "No matching case found"


# =============================================================================
# Test 3: Complete new contract case flow (creation to closure)
# =============================================================================


class TestNewContractCaseFlow:
    """Test complete new contract case flow through all state transitions.

    State transitions:
    新開案件 → 待填問卷 → 待人工確認 → 台電審核 → 發送前人工確認 → 安裝階段 → 完成上線 → 已結案
    """

    def test_new_contract_full_flow(self):
        """Test the complete new contract case lifecycle."""
        mock_lambda = MagicMock()
        mock_lambda.invoke.return_value = {"StatusCode": 202}

        mock_ragic = MagicMock()
        mock_ragic.get_questionnaire_data.return_value = {
            "customer_email": "customer@example.com",
            "customer_name": "王小明",
            "case_type": "新約",
        }
        mock_ragic.update_case_status = MagicMock()
        mock_ragic.update_case_record = MagicMock()
        mock_ragic.get_case_status = MagicMock()
        mock_ragic.close = MagicMock()

        with patch(
            "dreams_workflow.workflow_engine.app._get_lambda_client",
            return_value=mock_lambda,
        ), patch(
            "dreams_workflow.workflow_engine.app.CloudRagicClient",
            return_value=mock_ragic,
        ):
            # ---- Step 1: 新開案件 → 待填問卷 ----
            from dreams_workflow.workflow_engine.app import handle_new_case

            result = handle_new_case("CASE-NEW-001", {
                "customer_email": "customer@example.com",
                "customer_name": "王小明",
            })

            assert result["new_status"] == "待填問卷"
            assert result["email_sent_to"] == "customer@example.com"
            # Verify update_case_status was called with 待填問卷
            mock_ragic.update_case_status.assert_called_with("CASE-NEW-001", "待填問卷")

            # Verify email service was invoked for questionnaire notification
            mock_lambda.invoke.assert_called()
            email_call = mock_lambda.invoke.call_args_list[0]
            email_payload = json.loads(email_call[1]["Payload"].decode("utf-8"))
            assert email_payload["email_type"] == "問卷通知"

        # ---- Step 2: 待填問卷 → 待人工確認 (AI determination) ----
        # This is handled by ai_determination Lambda, which writes results
        # to RAGIC and sets status to 待人工確認. We verify the state machine
        # allows this transition.
        from dreams_workflow.shared.state_machine import validate_transition

        assert validate_transition(
            CaseStatus.PENDING_QUESTIONNAIRE,
            CaseStatus.PENDING_MANUAL_CONFIRM,
        )

        # ---- Step 3: 待人工確認 → 台電審核 (manual status change) ----
        assert validate_transition(
            CaseStatus.PENDING_MANUAL_CONFIRM,
            CaseStatus.TAIPOWER_REVIEW,
        )

        # ---- Step 4: 台電審核 → 發送前人工確認 (after Taipower reply) ----
        assert validate_transition(
            CaseStatus.TAIPOWER_REVIEW,
            CaseStatus.PRE_SEND_CONFIRM,
        )

        # ---- Step 5: 發送前人工確認 → 安裝階段 (manual confirm approval) ----
        assert validate_transition(
            CaseStatus.PRE_SEND_CONFIRM,
            CaseStatus.INSTALLATION_PHASE,
        )

        # ---- Step 6: 安裝階段 → 完成上線 (self-check passed) ----
        assert validate_transition(
            CaseStatus.INSTALLATION_PHASE,
            CaseStatus.ONLINE_COMPLETED,
        )

        # ---- Step 7: 完成上線 → 已結案 (data sync complete) ----
        assert validate_transition(
            CaseStatus.ONLINE_COMPLETED,
            CaseStatus.CASE_CLOSED,
        )

    def test_new_contract_handle_new_case_then_closure(self):
        """Test new case creation and final closure with actual handler calls."""
        mock_lambda = MagicMock()
        mock_lambda.invoke.return_value = {"StatusCode": 202}

        mock_ragic = MagicMock()
        mock_ragic.update_case_status = MagicMock()
        mock_ragic.update_case_record = MagicMock()
        mock_ragic.get_questionnaire_data.return_value = {
            "customer_email": "customer@example.com",
            "customer_name": "王小明",
            "1014670": "太陽能站點A",
        }
        mock_ragic.close = MagicMock()

        with patch(
            "dreams_workflow.workflow_engine.app._get_lambda_client",
            return_value=mock_lambda,
        ), patch(
            "dreams_workflow.workflow_engine.app.CloudRagicClient",
            return_value=mock_ragic,
        ), patch(
            "dreams_workflow.workflow_engine.closure_flow._get_lambda_client",
            return_value=mock_lambda,
        ), patch(
            "dreams_workflow.workflow_engine.closure_flow.CloudRagicClient",
            return_value=mock_ragic,
        ), patch(
            "dreams_workflow.workflow_engine.closure_flow._sync_to_sunveillance",
            return_value=True,
        ):
            # Step 1: Handle new case (新開案件 → 待填問卷)
            from dreams_workflow.workflow_engine.app import handle_new_case

            result = handle_new_case("CASE-FULL-001", {
                "customer_email": "customer@example.com",
                "customer_name": "王小明",
            })
            assert result["new_status"] == "待填問卷"

            # Step 7: Handle closure (完成上線 → 已結案)
            from dreams_workflow.workflow_engine.closure_flow import handle_case_closure

            mock_ragic.update_case_status.reset_mock()
            result = handle_case_closure("CASE-FULL-001", {
                "customer_email": "customer@example.com",
                "customer_name": "王小明",
                "site_name": "太陽能站點A",
            })

            assert result["new_status"] == "已結案"
            assert result["sunveillance_synced"] is True
            # Verify status was updated to 已結案
            mock_ragic.update_case_status.assert_called_with(
                "CASE-FULL-001", "已結案"
            )

    def test_new_contract_invalid_transition_rejected(self):
        """Verify invalid state transitions are rejected in new contract flow."""
        from dreams_workflow.shared.exceptions import InvalidTransitionError
        from dreams_workflow.shared.state_machine import validate_transition

        # Cannot skip from 新開案件 directly to 台電審核
        assert not validate_transition(
            CaseStatus.NEW_CASE_CREATED,
            CaseStatus.TAIPOWER_REVIEW,
        )

        # Cannot go from 待填問卷 directly to 已結案 (new contract)
        assert not validate_transition(
            CaseStatus.PENDING_QUESTIONNAIRE,
            CaseStatus.CASE_CLOSED,
        )

        # Cannot go backwards from 安裝階段 to 待人工確認
        assert not validate_transition(
            CaseStatus.INSTALLATION_PHASE,
            CaseStatus.PENDING_MANUAL_CONFIRM,
        )


# =============================================================================
# Test 4: Complete renewal case flow (creation to closure)
# =============================================================================


class TestRenewalCaseFlow:
    """Test complete renewal case flow through all state transitions.

    State transitions:
    新開案件 → 待填問卷 → 續約處理 → 已結案
    """

    def test_renewal_full_flow(self):
        """Test the complete renewal case lifecycle."""
        mock_lambda = MagicMock()
        mock_lambda.invoke.return_value = {"StatusCode": 202}

        mock_ragic = MagicMock()
        mock_ragic.get_questionnaire_data.return_value = {
            "customer_email": "renewal@example.com",
            "customer_name": "李大華",
            "case_type": "續約",
            "electricity_number": "06-1234-5678",
        }
        mock_ragic.update_case_status = MagicMock()
        mock_ragic.update_case_record = MagicMock()
        mock_ragic.close = MagicMock()

        with patch(
            "dreams_workflow.workflow_engine.app._get_lambda_client",
            return_value=mock_lambda,
        ), patch(
            "dreams_workflow.workflow_engine.app.CloudRagicClient",
            return_value=mock_ragic,
        ), patch(
            "dreams_workflow.workflow_engine.renewal_flow._get_lambda_client",
            return_value=mock_lambda,
        ), patch(
            "dreams_workflow.workflow_engine.renewal_flow.CloudRagicClient",
            return_value=mock_ragic,
        ):
            # ---- Step 1: 新開案件 → 待填問卷 ----
            from dreams_workflow.workflow_engine.app import handle_new_case

            result = handle_new_case("CASE-RENEW-001", {
                "customer_email": "renewal@example.com",
                "customer_name": "李大華",
            })

            assert result["new_status"] == "待填問卷"
            mock_ragic.update_case_status.assert_called_with(
                "CASE-RENEW-001", "待填問卷"
            )

            # ---- Step 2: 待填問卷 → 續約處理 ----
            from dreams_workflow.workflow_engine.app import handle_questionnaire_response

            mock_ragic.update_case_status.reset_mock()
            result = handle_questionnaire_response(
                "CASE-RENEW-001",
                {"electricity_number": "06-1234-5678"},
                is_renewal=True,
            )

            assert result["new_status"] == "續約處理"
            mock_ragic.update_case_status.assert_called_with(
                "CASE-RENEW-001", "續約處理"
            )

            # ---- Step 3: 續約處理 → 已結案 ----
            from dreams_workflow.workflow_engine.renewal_flow import handle_renewal_complete

            mock_ragic.update_case_status.reset_mock()
            result = handle_renewal_complete("CASE-RENEW-001", {
                "renewal_site_id": "SITE-ABC-123",
            })

            assert result["new_status"] == "已結案"
            assert result["renewal_site_id"] == "SITE-ABC-123"
            mock_ragic.update_case_status.assert_called_with(
                "CASE-RENEW-001", "已結案"
            )

    def test_renewal_does_not_trigger_ai_determination(self):
        """Verify renewal flow does NOT invoke AI determination Lambda."""
        mock_lambda = MagicMock()
        mock_lambda.invoke.return_value = {"StatusCode": 202}

        mock_ragic = MagicMock()
        mock_ragic.update_case_status = MagicMock()
        mock_ragic.close = MagicMock()

        with patch(
            "dreams_workflow.workflow_engine.app._get_lambda_client",
            return_value=mock_lambda,
        ), patch(
            "dreams_workflow.workflow_engine.app.CloudRagicClient",
            return_value=mock_ragic,
        ):
            from dreams_workflow.workflow_engine.app import handle_questionnaire_response

            result = handle_questionnaire_response(
                "CASE-RENEW-002",
                {"electricity_number": "06-9999-0000"},
                is_renewal=True,
            )

            assert result["action"] == "renewal_processing"

            # Verify AI determination was NOT invoked
            # The Lambda client should NOT have been called with ai_determination
            for call in mock_lambda.invoke.call_args_list:
                call_kwargs = call[1] if call[1] else {}
                if "FunctionName" in call_kwargs:
                    assert call_kwargs["FunctionName"] != "test-ai-determination"

    def test_renewal_state_transitions_valid(self):
        """Verify all renewal state transitions are valid."""
        from dreams_workflow.shared.state_machine import validate_transition

        # 新開案件 → 待填問卷
        assert validate_transition(
            CaseStatus.NEW_CASE_CREATED,
            CaseStatus.PENDING_QUESTIONNAIRE,
        )

        # 待填問卷 → 續約處理
        assert validate_transition(
            CaseStatus.PENDING_QUESTIONNAIRE,
            CaseStatus.RENEWAL_PROCESSING,
        )

        # 續約處理 → 已結案
        assert validate_transition(
            CaseStatus.RENEWAL_PROCESSING,
            CaseStatus.CASE_CLOSED,
        )

    def test_renewal_cannot_enter_taipower_review(self):
        """Verify renewal cases cannot transition to Taipower review states."""
        from dreams_workflow.shared.state_machine import validate_transition

        # 續約處理 cannot go to 台電審核
        assert not validate_transition(
            CaseStatus.RENEWAL_PROCESSING,
            CaseStatus.TAIPOWER_REVIEW,
        )

        # 續約處理 cannot go to 安裝階段
        assert not validate_transition(
            CaseStatus.RENEWAL_PROCESSING,
            CaseStatus.INSTALLATION_PHASE,
        )

        # 續約處理 can only go to 已結案
        assert not validate_transition(
            CaseStatus.RENEWAL_PROCESSING,
            CaseStatus.PENDING_MANUAL_CONFIRM,
        )
