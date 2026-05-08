"""Smoke tests for webhook_handler.app module."""

import importlib
import json
import os
from unittest.mock import MagicMock, patch

os.environ["AI_DETERMINATION_FUNCTION_NAME"] = "test-ai-function"
os.environ["WORKFLOW_ENGINE_FUNCTION_NAME"] = "test-workflow-function"

import dreams_workflow.webhook_handler.app as app_module

importlib.reload(app_module)

from dreams_workflow.webhook_handler.app import (
    classify_webhook_event,
    lambda_handler,
    validate_webhook_source,
)
from dreams_workflow.shared.models import WebhookEventType


def test_classify_new_case_created():
    payload = {"form_path": "business-process2/2", "action": "create", "case_id": "123"}
    assert classify_webhook_event(payload) == WebhookEventType.NEW_CASE_CREATED


def test_classify_case_status_changed():
    payload = {"form_path": "business-process2/2", "action": "update", "case_id": "123"}
    assert classify_webhook_event(payload) == WebhookEventType.CASE_STATUS_CHANGED


def test_classify_renewal_questionnaire():
    payload = {"form_path": "work-survey/7", "case_type": "續約", "case_id": "123"}
    assert classify_webhook_event(payload) == WebhookEventType.RENEWAL_QUESTIONNAIRE


def test_classify_new_contract_questionnaire():
    payload = {"form_path": "work-survey/7", "case_type": "新約", "case_id": "123"}
    assert classify_webhook_event(payload) == WebhookEventType.NEW_CONTRACT_FULL_QUESTIONNAIRE


def test_classify_supplementary_questionnaire():
    payload = {"form_path": "work-survey/7", "is_supplement": True, "case_id": "123"}
    assert classify_webhook_event(payload) == WebhookEventType.SUPPLEMENTARY_QUESTIONNAIRE


def test_classify_supplement_takes_priority():
    """Supplement flag should take priority over form path classification."""
    payload = {
        "form_path": "business-process2/2",
        "is_supplement": True,
        "case_id": "123",
    }
    assert classify_webhook_event(payload) == WebhookEventType.SUPPLEMENTARY_QUESTIONNAIRE


def test_classify_new_record_flag():
    """is_new_record flag should classify as NEW_CASE_CREATED."""
    payload = {"form_path": "business-process2/2", "is_new_record": True, "case_id": "123"}
    assert classify_webhook_event(payload) == WebhookEventType.NEW_CASE_CREATED


def test_validate_webhook_source_no_secret():
    """Without WEBHOOK_SECRET, validation should pass (dev mode)."""
    assert validate_webhook_source({}, "") is True


def test_validate_webhook_source_with_secret_missing_header():
    """With WEBHOOK_SECRET but no signature header, validation should fail."""
    with patch.object(app_module, "WEBHOOK_SECRET", "my-secret"):
        assert validate_webhook_source({}, "body") is False


def test_validate_webhook_source_with_valid_signature():
    """With correct HMAC signature, validation should pass."""
    import hashlib
    import hmac as hmac_mod

    secret = "test-secret"
    body = '{"test": "data"}'
    expected_sig = hmac_mod.new(
        key=secret.encode("utf-8"),
        msg=body.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()

    with patch.object(app_module, "WEBHOOK_SECRET", secret):
        headers = {"x-ragic-signature": expected_sig}
        assert validate_webhook_source(headers, body) is True


def test_validate_webhook_source_with_invalid_signature():
    """With incorrect signature, validation should fail."""
    with patch.object(app_module, "WEBHOOK_SECRET", "my-secret"):
        headers = {"x-ragic-signature": "wrong-signature"}
        assert validate_webhook_source(headers, "body") is False


def test_lambda_handler_valid_new_case():
    """lambda_handler should route new case events to workflow_engine."""
    mock_client = MagicMock()
    mock_client.invoke.return_value = {"StatusCode": 202}

    with patch("dreams_workflow.webhook_handler.app._get_lambda_client", return_value=mock_client):
        event = {
            "headers": {},
            "body": json.dumps({"form_path": "business-process2/2", "action": "create", "case_id": "CASE-001"}),
            "isBase64Encoded": False,
        }
        response = lambda_handler(event, None)
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["event_type"] == "NEW_CASE_CREATED"
        mock_client.invoke.assert_called_once()
        call_args = mock_client.invoke.call_args
        assert call_args[1]["FunctionName"] == "test-workflow-function"
        assert call_args[1]["InvocationType"] == "Event"


def test_lambda_handler_routes_questionnaire_to_ai():
    """lambda_handler should route new contract questionnaire to ai_determination."""
    mock_client = MagicMock()
    mock_client.invoke.return_value = {"StatusCode": 202}

    with patch("dreams_workflow.webhook_handler.app._get_lambda_client", return_value=mock_client):
        event = {
            "headers": {},
            "body": json.dumps({"form_path": "work-survey/7", "case_type": "新約", "case_id": "CASE-002"}),
            "isBase64Encoded": False,
        }
        response = lambda_handler(event, None)
        assert response["statusCode"] == 200
        call_args = mock_client.invoke.call_args
        assert call_args[1]["FunctionName"] == "test-ai-function"


def test_lambda_handler_validation_failure():
    """lambda_handler should return 401 when validation fails."""
    with patch.object(app_module, "WEBHOOK_SECRET", "my-secret"):
        event = {
            "headers": {"x-ragic-signature": "invalid"},
            "body": json.dumps({"form_path": "business-process2/2"}),
            "isBase64Encoded": False,
        }
        response = lambda_handler(event, None)
        assert response["statusCode"] == 401


def test_lambda_handler_invalid_json():
    """lambda_handler should return 400 for invalid JSON body."""
    event = {
        "headers": {},
        "body": "not-valid-json{{{",
        "isBase64Encoded": False,
    }
    response = lambda_handler(event, None)
    assert response["statusCode"] == 400


def test_lambda_handler_base64_encoded():
    """lambda_handler should decode base64-encoded body."""
    import base64

    mock_client = MagicMock()
    mock_client.invoke.return_value = {"StatusCode": 202}

    payload = json.dumps({"form_path": "business-process2/2", "action": "create", "case_id": "CASE-003"})
    encoded_body = base64.b64encode(payload.encode("utf-8")).decode("utf-8")

    with patch("dreams_workflow.webhook_handler.app._get_lambda_client", return_value=mock_client):
        event = {
            "headers": {},
            "body": encoded_body,
            "isBase64Encoded": True,
        }
        response = lambda_handler(event, None)
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["event_type"] == "NEW_CASE_CREATED"


def test_lambda_handler_invoke_failure():
    """lambda_handler should return 200 even if downstream invoke fails."""
    mock_client = MagicMock()
    mock_client.invoke.side_effect = Exception("Connection timeout")

    with patch("dreams_workflow.webhook_handler.app._get_lambda_client", return_value=mock_client):
        event = {
            "headers": {},
            "body": json.dumps({"form_path": "business-process2/2", "action": "create", "case_id": "CASE-004"}),
            "isBase64Encoded": False,
        }
        response = lambda_handler(event, None)
        # Should still return 200 to prevent RAGIC retry storms
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert "failed" in body["message"]
