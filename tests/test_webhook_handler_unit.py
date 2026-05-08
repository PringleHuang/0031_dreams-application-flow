"""Unit tests for Webhook Handler (webhook_handler/app.py).

Covers:
- 5 種事件類型的正確分類
- 驗證失敗回傳 401
- Payload 解析（JSON、Base64）
- Lambda 路由邏輯
- 邊界條件與錯誤處理

Requirements: 11.1, 11.2, 11.3
"""

from __future__ import annotations

import base64
import hashlib
import hmac as hmac_mod
import importlib
import json
import os
from unittest.mock import MagicMock, patch

import pytest

# Set environment variables before importing the module
os.environ.setdefault("AI_DETERMINATION_FUNCTION_NAME", "test-ai-determination")
os.environ.setdefault("WORKFLOW_ENGINE_FUNCTION_NAME", "test-workflow-engine")

import dreams_workflow.webhook_handler.app as app_module

importlib.reload(app_module)

from dreams_workflow.shared.models import WebhookEventType
from dreams_workflow.webhook_handler.app import (
    classify_webhook_event,
    lambda_handler,
    validate_webhook_source,
    _get_target_function,
)


# =============================================================================
# Helpers
# =============================================================================


def _make_api_gateway_event(
    body: str | dict,
    headers: dict | None = None,
    is_base64: bool = False,
) -> dict:
    """Build a minimal API Gateway event dict."""
    if isinstance(body, dict):
        body = json.dumps(body, ensure_ascii=False)
    if is_base64:
        body = base64.b64encode(body.encode("utf-8")).decode("utf-8")
    return {
        "headers": headers or {},
        "body": body,
        "isBase64Encoded": is_base64,
    }


def _compute_hmac_signature(secret: str, body: str) -> str:
    """Compute HMAC-SHA256 hex digest for a given secret and body."""
    return hmac_mod.new(
        key=secret.encode("utf-8"),
        msg=body.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()


# =============================================================================
# Test: 5 種事件類型的正確分類 (Requirements 11.2)
# =============================================================================


class TestClassifyWebhookEvent:
    """Tests for classify_webhook_event covering all 5 event types."""

    # --- NEW_CASE_CREATED ---

    def test_new_case_created_via_action_create(self):
        """Case management form with action=create → NEW_CASE_CREATED."""
        payload = {
            "form_path": "business-process2/2",
            "action": "create",
            "case_id": "CASE-001",
        }
        assert classify_webhook_event(payload) == WebhookEventType.NEW_CASE_CREATED

    def test_new_case_created_via_is_new_record_flag(self):
        """Case management form with is_new_record=True → NEW_CASE_CREATED."""
        payload = {
            "form_path": "business-process2/2",
            "is_new_record": True,
            "case_id": "CASE-002",
        }
        assert classify_webhook_event(payload) == WebhookEventType.NEW_CASE_CREATED

    def test_new_case_created_with_full_form_path(self):
        """Full URL-like form path containing the case management path."""
        payload = {
            "form_path": "/solarcs/business-process2/2",
            "action": "create",
            "case_id": "CASE-003",
        }
        assert classify_webhook_event(payload) == WebhookEventType.NEW_CASE_CREATED

    # --- CASE_STATUS_CHANGED ---

    def test_case_status_changed_via_update(self):
        """Case management form with action=update → CASE_STATUS_CHANGED."""
        payload = {
            "form_path": "business-process2/2",
            "action": "update",
            "case_id": "CASE-010",
            "case_status": "台電審核",
        }
        assert classify_webhook_event(payload) == WebhookEventType.CASE_STATUS_CHANGED

    def test_case_status_changed_no_action(self):
        """Case management form without action field → CASE_STATUS_CHANGED."""
        payload = {
            "form_path": "business-process2/2",
            "case_id": "CASE-011",
        }
        assert classify_webhook_event(payload) == WebhookEventType.CASE_STATUS_CHANGED

    def test_case_status_changed_with_edit_action(self):
        """Case management form with action=edit → CASE_STATUS_CHANGED."""
        payload = {
            "form_path": "business-process2/2",
            "action": "edit",
            "case_id": "CASE-012",
        }
        assert classify_webhook_event(payload) == WebhookEventType.CASE_STATUS_CHANGED

    # --- RENEWAL_QUESTIONNAIRE ---

    def test_renewal_questionnaire(self):
        """Questionnaire form with case_type=續約 → RENEWAL_QUESTIONNAIRE."""
        payload = {
            "form_path": "work-survey/7",
            "case_type": "續約",
            "case_id": "CASE-020",
        }
        assert classify_webhook_event(payload) == WebhookEventType.RENEWAL_QUESTIONNAIRE

    def test_renewal_questionnaire_with_full_path(self):
        """Full path containing questionnaire form path with renewal type."""
        payload = {
            "form_path": "/solarcs/work-survey/7",
            "case_type": "續約",
            "case_id": "CASE-021",
        }
        assert classify_webhook_event(payload) == WebhookEventType.RENEWAL_QUESTIONNAIRE

    # --- NEW_CONTRACT_FULL_QUESTIONNAIRE ---

    def test_new_contract_questionnaire(self):
        """Questionnaire form with case_type=新約 → NEW_CONTRACT_FULL_QUESTIONNAIRE."""
        payload = {
            "form_path": "work-survey/7",
            "case_type": "新約",
            "case_id": "CASE-030",
        }
        assert classify_webhook_event(payload) == WebhookEventType.NEW_CONTRACT_FULL_QUESTIONNAIRE

    def test_new_contract_questionnaire_no_case_type(self):
        """Questionnaire form without case_type defaults to NEW_CONTRACT_FULL_QUESTIONNAIRE."""
        payload = {
            "form_path": "work-survey/7",
            "case_id": "CASE-031",
        }
        assert classify_webhook_event(payload) == WebhookEventType.NEW_CONTRACT_FULL_QUESTIONNAIRE

    def test_new_contract_questionnaire_unknown_case_type(self):
        """Questionnaire form with unrecognized case_type → NEW_CONTRACT_FULL_QUESTIONNAIRE."""
        payload = {
            "form_path": "work-survey/7",
            "case_type": "其他",
            "case_id": "CASE-032",
        }
        assert classify_webhook_event(payload) == WebhookEventType.NEW_CONTRACT_FULL_QUESTIONNAIRE

    # --- SUPPLEMENTARY_QUESTIONNAIRE ---

    def test_supplementary_questionnaire(self):
        """Payload with is_supplement=True → SUPPLEMENTARY_QUESTIONNAIRE."""
        payload = {
            "form_path": "work-survey/7",
            "is_supplement": True,
            "case_id": "CASE-040",
        }
        assert classify_webhook_event(payload) == WebhookEventType.SUPPLEMENTARY_QUESTIONNAIRE

    def test_supplementary_takes_priority_over_case_management_form(self):
        """is_supplement flag takes priority over case management form path."""
        payload = {
            "form_path": "business-process2/2",
            "is_supplement": True,
            "action": "create",
            "case_id": "CASE-041",
        }
        assert classify_webhook_event(payload) == WebhookEventType.SUPPLEMENTARY_QUESTIONNAIRE

    def test_supplementary_takes_priority_over_questionnaire_form(self):
        """is_supplement flag takes priority over questionnaire form classification."""
        payload = {
            "form_path": "work-survey/7",
            "is_supplement": True,
            "case_type": "新約",
            "case_id": "CASE-042",
        }
        assert classify_webhook_event(payload) == WebhookEventType.SUPPLEMENTARY_QUESTIONNAIRE

    # --- Edge cases ---

    def test_empty_payload_defaults_to_status_changed(self):
        """Empty payload defaults to CASE_STATUS_CHANGED."""
        assert classify_webhook_event({}) == WebhookEventType.CASE_STATUS_CHANGED

    def test_unknown_form_path_with_create_action(self):
        """Unknown form path with action=create → NEW_CASE_CREATED."""
        payload = {
            "form_path": "unknown/form/99",
            "action": "create",
            "case_id": "CASE-050",
        }
        assert classify_webhook_event(payload) == WebhookEventType.NEW_CASE_CREATED

    def test_unknown_form_path_without_create_action(self):
        """Unknown form path without create action → CASE_STATUS_CHANGED."""
        payload = {
            "form_path": "unknown/form/99",
            "action": "update",
            "case_id": "CASE-051",
        }
        assert classify_webhook_event(payload) == WebhookEventType.CASE_STATUS_CHANGED

    def test_is_supplement_false_does_not_trigger(self):
        """is_supplement=False should NOT classify as SUPPLEMENTARY_QUESTIONNAIRE."""
        payload = {
            "form_path": "work-survey/7",
            "is_supplement": False,
            "case_type": "新約",
            "case_id": "CASE-052",
        }
        assert classify_webhook_event(payload) == WebhookEventType.NEW_CONTRACT_FULL_QUESTIONNAIRE


# =============================================================================
# Test: 驗證失敗回傳 401 (Requirements 11.1, 11.3)
# =============================================================================


class TestValidateWebhookSource:
    """Tests for validate_webhook_source and 401 responses."""

    def test_no_secret_configured_passes_validation(self):
        """Without WEBHOOK_SECRET, all requests pass (dev mode)."""
        with patch.object(app_module, "WEBHOOK_SECRET", ""):
            assert validate_webhook_source({}, "any body") is True

    def test_missing_signature_header_fails(self):
        """With secret configured but no signature header → fails."""
        with patch.object(app_module, "WEBHOOK_SECRET", "secret123"):
            assert validate_webhook_source({}, '{"data": 1}') is False

    def test_empty_signature_header_fails(self):
        """With secret configured but empty signature → fails."""
        with patch.object(app_module, "WEBHOOK_SECRET", "secret123"):
            headers = {"x-ragic-signature": ""}
            assert validate_webhook_source(headers, '{"data": 1}') is False

    def test_invalid_signature_fails(self):
        """With incorrect HMAC signature → fails."""
        with patch.object(app_module, "WEBHOOK_SECRET", "secret123"):
            headers = {"x-ragic-signature": "deadbeef1234567890abcdef"}
            assert validate_webhook_source(headers, '{"data": 1}') is False

    def test_valid_signature_passes(self):
        """With correct HMAC-SHA256 signature → passes."""
        secret = "my-webhook-secret"
        body = '{"case_id": "CASE-100", "action": "create"}'
        signature = _compute_hmac_signature(secret, body)

        with patch.object(app_module, "WEBHOOK_SECRET", secret):
            headers = {"x-ragic-signature": signature}
            assert validate_webhook_source(headers, body) is True

    def test_signature_header_case_insensitive(self):
        """Header key lookup should be case-insensitive."""
        secret = "case-test-secret"
        body = '{"test": true}'
        signature = _compute_hmac_signature(secret, body)

        with patch.object(app_module, "WEBHOOK_SECRET", secret):
            headers = {"X-Ragic-Signature": signature}
            assert validate_webhook_source(headers, body) is True

    def test_lambda_handler_returns_401_on_validation_failure(self):
        """Full lambda_handler returns 401 when validation fails."""
        with patch.object(app_module, "WEBHOOK_SECRET", "real-secret"):
            event = _make_api_gateway_event(
                body={"form_path": "business-process2/2", "action": "create"},
                headers={"x-ragic-signature": "wrong-sig"},
            )
            response = lambda_handler(event, None)
            assert response["statusCode"] == 401
            body = json.loads(response["body"])
            assert body["error"] == "Unauthorized"

    def test_lambda_handler_returns_401_no_signature_header(self):
        """lambda_handler returns 401 when secret is set but no header provided."""
        with patch.object(app_module, "WEBHOOK_SECRET", "real-secret"):
            event = _make_api_gateway_event(
                body={"form_path": "business-process2/2"},
                headers={},
            )
            response = lambda_handler(event, None)
            assert response["statusCode"] == 401


# =============================================================================
# Test: Payload 解析（JSON、Base64）(Requirements 11.1)
# =============================================================================


class TestPayloadParsing:
    """Tests for JSON and Base64 payload parsing."""

    def test_plain_json_body(self):
        """Standard JSON body is parsed correctly."""
        mock_client = MagicMock()
        mock_client.invoke.return_value = {"StatusCode": 202}

        payload = {"form_path": "business-process2/2", "action": "create", "case_id": "P-001"}

        with patch("dreams_workflow.webhook_handler.app._get_lambda_client", return_value=mock_client):
            event = _make_api_gateway_event(body=payload, is_base64=False)
            response = lambda_handler(event, None)
            assert response["statusCode"] == 200

            # Verify the payload was correctly passed to downstream Lambda
            invoke_call = mock_client.invoke.call_args
            invoke_payload = json.loads(invoke_call[1]["Payload"].decode("utf-8"))
            assert invoke_payload["payload"]["case_id"] == "P-001"
            assert invoke_payload["event_type"] == "NEW_CASE_CREATED"

    def test_base64_encoded_body(self):
        """Base64-encoded body is decoded and parsed correctly."""
        mock_client = MagicMock()
        mock_client.invoke.return_value = {"StatusCode": 202}

        payload = {"form_path": "work-survey/7", "case_type": "續約", "case_id": "P-002"}

        with patch("dreams_workflow.webhook_handler.app._get_lambda_client", return_value=mock_client):
            event = _make_api_gateway_event(body=payload, is_base64=True)
            response = lambda_handler(event, None)
            assert response["statusCode"] == 200

            invoke_call = mock_client.invoke.call_args
            invoke_payload = json.loads(invoke_call[1]["Payload"].decode("utf-8"))
            assert invoke_payload["payload"]["case_type"] == "續約"
            assert invoke_payload["event_type"] == "RENEWAL_QUESTIONNAIRE"

    def test_base64_encoded_with_unicode(self):
        """Base64-encoded body with Chinese characters is handled correctly."""
        mock_client = MagicMock()
        mock_client.invoke.return_value = {"StatusCode": 202}

        payload = {
            "form_path": "business-process2/2",
            "action": "update",
            "case_id": "P-003",
            "case_status": "台電審核",
            "customer_name": "王小明",
        }

        with patch("dreams_workflow.webhook_handler.app._get_lambda_client", return_value=mock_client):
            event = _make_api_gateway_event(body=payload, is_base64=True)
            response = lambda_handler(event, None)
            assert response["statusCode"] == 200

            invoke_call = mock_client.invoke.call_args
            invoke_payload = json.loads(invoke_call[1]["Payload"].decode("utf-8"))
            assert invoke_payload["payload"]["customer_name"] == "王小明"

    def test_invalid_json_returns_400(self):
        """Invalid JSON body returns 400 error."""
        event = _make_api_gateway_event(body="not-valid-json{{{", is_base64=False)
        # Override body directly since helper would try to json.dumps a dict
        event["body"] = "not-valid-json{{{"
        response = lambda_handler(event, None)
        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert "Invalid JSON" in body["error"]

    def test_empty_body_treated_as_empty_dict(self):
        """Empty body is treated as empty dict (defaults to CASE_STATUS_CHANGED)."""
        mock_client = MagicMock()
        mock_client.invoke.return_value = {"StatusCode": 202}

        with patch("dreams_workflow.webhook_handler.app._get_lambda_client", return_value=mock_client):
            event = {
                "headers": {},
                "body": "",
                "isBase64Encoded": False,
            }
            response = lambda_handler(event, None)
            assert response["statusCode"] == 200
            body = json.loads(response["body"])
            assert body["event_type"] == "CASE_STATUS_CHANGED"

    def test_malformed_base64_returns_error(self):
        """Malformed base64 body that decodes to invalid JSON returns 400."""
        # Valid base64 but decodes to non-JSON text
        non_json_text = "this is not json"
        encoded = base64.b64encode(non_json_text.encode("utf-8")).decode("utf-8")

        event = {
            "headers": {},
            "body": encoded,
            "isBase64Encoded": True,
        }
        response = lambda_handler(event, None)
        assert response["statusCode"] == 400


# =============================================================================
# Test: Lambda 路由邏輯 (Requirements 11.1, 11.2)
# =============================================================================


class TestLambdaRouting:
    """Tests for downstream Lambda invocation routing."""

    def test_new_case_routes_to_workflow_engine(self):
        """NEW_CASE_CREATED routes to workflow_engine function."""
        assert _get_target_function(WebhookEventType.NEW_CASE_CREATED) == os.environ.get(
            "WORKFLOW_ENGINE_FUNCTION_NAME", ""
        )

    def test_status_changed_routes_to_workflow_engine(self):
        """CASE_STATUS_CHANGED routes to workflow_engine function."""
        assert _get_target_function(WebhookEventType.CASE_STATUS_CHANGED) == os.environ.get(
            "WORKFLOW_ENGINE_FUNCTION_NAME", ""
        )

    def test_renewal_routes_to_workflow_engine(self):
        """RENEWAL_QUESTIONNAIRE routes to workflow_engine function."""
        assert _get_target_function(WebhookEventType.RENEWAL_QUESTIONNAIRE) == os.environ.get(
            "WORKFLOW_ENGINE_FUNCTION_NAME", ""
        )

    def test_new_contract_routes_to_ai_determination(self):
        """NEW_CONTRACT_FULL_QUESTIONNAIRE routes to ai_determination function."""
        assert _get_target_function(WebhookEventType.NEW_CONTRACT_FULL_QUESTIONNAIRE) == os.environ.get(
            "AI_DETERMINATION_FUNCTION_NAME", ""
        )

    def test_supplement_routes_to_ai_determination(self):
        """SUPPLEMENTARY_QUESTIONNAIRE routes to ai_determination function."""
        assert _get_target_function(WebhookEventType.SUPPLEMENTARY_QUESTIONNAIRE) == os.environ.get(
            "AI_DETERMINATION_FUNCTION_NAME", ""
        )

    def test_invoke_uses_async_invocation_type(self):
        """Downstream Lambda is invoked asynchronously (InvocationType=Event)."""
        mock_client = MagicMock()
        mock_client.invoke.return_value = {"StatusCode": 202}

        with patch("dreams_workflow.webhook_handler.app._get_lambda_client", return_value=mock_client):
            event = _make_api_gateway_event(
                body={"form_path": "business-process2/2", "action": "create", "case_id": "R-001"}
            )
            lambda_handler(event, None)

            call_kwargs = mock_client.invoke.call_args[1]
            assert call_kwargs["InvocationType"] == "Event"

    def test_invoke_payload_contains_event_type_and_case_id(self):
        """Downstream Lambda receives event_type, payload, and case_id."""
        mock_client = MagicMock()
        mock_client.invoke.return_value = {"StatusCode": 202}

        with patch("dreams_workflow.webhook_handler.app._get_lambda_client", return_value=mock_client):
            event = _make_api_gateway_event(
                body={
                    "form_path": "work-survey/7",
                    "case_type": "新約",
                    "case_id": "R-002",
                }
            )
            lambda_handler(event, None)

            call_kwargs = mock_client.invoke.call_args[1]
            invoke_payload = json.loads(call_kwargs["Payload"].decode("utf-8"))
            assert invoke_payload["event_type"] == "NEW_CONTRACT_FULL_QUESTIONNAIRE"
            assert invoke_payload["case_id"] == "R-002"
            assert invoke_payload["payload"]["case_type"] == "新約"

    def test_invoke_failure_returns_200_to_prevent_retry_storm(self):
        """Lambda invoke failure still returns 200 to prevent RAGIC retry storms."""
        mock_client = MagicMock()
        mock_client.invoke.side_effect = Exception("Lambda throttled")

        with patch("dreams_workflow.webhook_handler.app._get_lambda_client", return_value=mock_client):
            event = _make_api_gateway_event(
                body={"form_path": "business-process2/2", "action": "create", "case_id": "R-003"}
            )
            response = lambda_handler(event, None)
            assert response["statusCode"] == 200
            body = json.loads(response["body"])
            assert "failed" in body["message"]

    def test_no_target_function_configured(self):
        """When target function env var is empty, returns 200 with warning."""
        with patch.object(app_module, "WORKFLOW_ENGINE_FUNCTION", ""):
            mock_client = MagicMock()
            with patch("dreams_workflow.webhook_handler.app._get_lambda_client", return_value=mock_client):
                event = _make_api_gateway_event(
                    body={"form_path": "business-process2/2", "action": "create", "case_id": "R-004"}
                )
                response = lambda_handler(event, None)
                assert response["statusCode"] == 200
                body = json.loads(response["body"])
                assert "no handler configured" in body["message"]
                mock_client.invoke.assert_not_called()


# =============================================================================
# Test: 完整端到端場景 (Integration-style unit tests)
# =============================================================================


class TestEndToEndScenarios:
    """Integration-style unit tests covering full request lifecycle."""

    def test_full_flow_new_case_json(self):
        """Full flow: JSON new case → validate → classify → invoke workflow_engine."""
        mock_client = MagicMock()
        mock_client.invoke.return_value = {"StatusCode": 202}

        with patch("dreams_workflow.webhook_handler.app._get_lambda_client", return_value=mock_client):
            event = _make_api_gateway_event(
                body={
                    "form_path": "business-process2/2",
                    "action": "create",
                    "case_id": "E2E-001",
                    "customer_name": "測試客戶",
                }
            )
            response = lambda_handler(event, None)
            assert response["statusCode"] == 200
            body = json.loads(response["body"])
            assert body["event_type"] == "NEW_CASE_CREATED"
            assert body["message"] == "Webhook received, processing started"

    def test_full_flow_supplement_base64(self):
        """Full flow: Base64 supplement → validate → classify → invoke ai_determination."""
        mock_client = MagicMock()
        mock_client.invoke.return_value = {"StatusCode": 202}

        with patch("dreams_workflow.webhook_handler.app._get_lambda_client", return_value=mock_client):
            event = _make_api_gateway_event(
                body={
                    "form_path": "work-survey/7",
                    "is_supplement": True,
                    "case_id": "E2E-002",
                },
                is_base64=True,
            )
            response = lambda_handler(event, None)
            assert response["statusCode"] == 200
            body = json.loads(response["body"])
            assert body["event_type"] == "SUPPLEMENTARY_QUESTIONNAIRE"

    def test_full_flow_with_valid_hmac(self):
        """Full flow with HMAC validation enabled and correct signature."""
        secret = "production-secret-key"
        payload_dict = {
            "form_path": "work-survey/7",
            "case_type": "續約",
            "case_id": "E2E-003",
        }
        body_str = json.dumps(payload_dict, ensure_ascii=False)
        signature = _compute_hmac_signature(secret, body_str)

        mock_client = MagicMock()
        mock_client.invoke.return_value = {"StatusCode": 202}

        with (
            patch.object(app_module, "WEBHOOK_SECRET", secret),
            patch("dreams_workflow.webhook_handler.app._get_lambda_client", return_value=mock_client),
        ):
            event = {
                "headers": {"x-ragic-signature": signature},
                "body": body_str,
                "isBase64Encoded": False,
            }
            response = lambda_handler(event, None)
            assert response["statusCode"] == 200
            body = json.loads(response["body"])
            assert body["event_type"] == "RENEWAL_QUESTIONNAIRE"

    def test_case_id_fallback_to_ragic_id(self):
        """When case_id is missing, ragic_id is used as fallback."""
        mock_client = MagicMock()
        mock_client.invoke.return_value = {"StatusCode": 202}

        with patch("dreams_workflow.webhook_handler.app._get_lambda_client", return_value=mock_client):
            event = _make_api_gateway_event(
                body={
                    "form_path": "business-process2/2",
                    "action": "create",
                    "ragic_id": "RAGIC-999",
                }
            )
            response = lambda_handler(event, None)
            assert response["statusCode"] == 200

            invoke_call = mock_client.invoke.call_args
            invoke_payload = json.loads(invoke_call[1]["Payload"].decode("utf-8"))
            assert invoke_payload["case_id"] == "RAGIC-999"
