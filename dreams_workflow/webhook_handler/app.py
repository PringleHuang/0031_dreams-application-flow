"""Webhook Handler Lambda Function.

Receives RAGIC Webhook POST requests via API Gateway, validates the source,
classifies the event type, and asynchronously invokes the appropriate
downstream Lambda function (ai_determination or workflow_engine).

Requirements: 11.1, 11.2, 11.3
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from typing import Any

import boto3

from dreams_workflow.shared.logger import get_logger, log_operation
from dreams_workflow.shared.models import WebhookEventType

logger = get_logger(__name__)

# Lazy-initialized Lambda client (created on first invocation)
_lambda_client = None


def _get_lambda_client():
    """Get or create the boto3 Lambda client (lazy initialization)."""
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda")
    return _lambda_client


# Environment variables for downstream Lambda function names
AI_DETERMINATION_FUNCTION = os.environ.get("AI_DETERMINATION_FUNCTION_NAME", "")
WORKFLOW_ENGINE_FUNCTION = os.environ.get("WORKFLOW_ENGINE_FUNCTION_NAME", "")

# Webhook validation secret (shared with RAGIC configuration)
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

# RAGIC form path constants
CASE_MANAGEMENT_FORM_PATH = "business-process2/2"
QUESTIONNAIRE_FORM_PATH = "work-survey/7"

# Field identifiers used for event classification
CASE_STATUS_FIELD = "case_status"
CASE_TYPE_FIELD = "case_type"
IS_SUPPLEMENT_FIELD = "is_supplement"

# Status value that indicates a newly created case (set by shipment scanner)
NEW_CASE_STATUS_VALUE = "新開案件"


def lambda_handler(event: dict, context: Any) -> dict:
    """Receive and process RAGIC Webhook events from API Gateway.

    Validates the request source, parses the payload, classifies the event
    type, and invokes the appropriate downstream Lambda function.

    Args:
        event: API Gateway event containing headers and body.
        context: Lambda execution context.

    Returns:
        HTTP response dict with statusCode and body.
    """
    logger.info(
        "Webhook received",
        extra={"case_id": "N/A", "operation_type": "webhook_receive"},
    )

    headers = event.get("headers", {})
    raw_body = event.get("body", "")

    # Decode base64-encoded body if needed
    if event.get("isBase64Encoded"):
        raw_body = base64.b64decode(raw_body).decode("utf-8")

    # Validate webhook source
    if not validate_webhook_source(headers, raw_body):
        logger.warning(
            "Webhook validation failed",
            extra={"case_id": "N/A", "operation_type": "webhook_validation_failed"},
        )
        return {
            "statusCode": 401,
            "body": json.dumps({"error": "Unauthorized"}),
        }

    # Parse payload
    try:
        payload = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        logger.warning(
            "Invalid JSON body received",
            extra={"case_id": "N/A", "operation_type": "webhook_parse_error"},
        )
        return {
            "statusCode": 400,
            "body": json.dumps({"error": "Invalid JSON payload"}),
        }

    # Classify event type
    event_type = classify_webhook_event(payload)
    case_id = payload.get("case_id", payload.get("ragic_id", "unknown"))

    log_operation(
        logger,
        case_id=str(case_id),
        operation_type="webhook_classified",
        message=f"Event classified as {event_type.value}",
    )

    # Route to appropriate downstream Lambda
    target_function = _get_target_function(event_type)
    if not target_function:
        logger.error(
            f"No target function configured for event type: {event_type.value}",
            extra={"case_id": str(case_id), "operation_type": "webhook_routing_error"},
        )
        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "Webhook received, but no handler configured",
                "event_type": event_type.value,
            }),
        }

    # Asynchronously invoke downstream Lambda
    invoke_payload = {
        "event_type": event_type.value,
        "payload": payload,
        "case_id": str(case_id),
    }

    try:
        response = _get_lambda_client().invoke(
            FunctionName=target_function,
            InvocationType="Event",  # Asynchronous invocation
            Payload=json.dumps(invoke_payload, ensure_ascii=False).encode("utf-8"),
        )
        log_operation(
            logger,
            case_id=str(case_id),
            operation_type="lambda_invoke",
            message=(
                f"Invoked {target_function} for {event_type.value}, "
                f"StatusCode: {response['StatusCode']}"
            ),
        )
    except Exception as e:
        logger.error(
            f"Failed to invoke downstream Lambda: {e}",
            extra={
                "case_id": str(case_id),
                "operation_type": "lambda_invoke_error",
            },
        )
        # Return 200 to RAGIC to prevent webhook retry storms
        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "Webhook received, but downstream invocation failed",
                "error": str(e),
            }),
        }

    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "Webhook received, processing started",
            "event_type": event_type.value,
        }),
    }


def validate_webhook_source(headers: dict, body: str) -> bool:
    """Validate that the webhook request originates from RAGIC.

    Verification is performed using HMAC-SHA256 signature comparison when
    a WEBHOOK_SECRET is configured. If no secret is configured, validation
    is skipped (development mode).

    Args:
        headers: HTTP request headers (case-insensitive keys).
        body: Raw request body string.

    Returns:
        True if the request is valid, False otherwise.
    """
    if not WEBHOOK_SECRET:
        # No secret configured — skip validation (development/testing mode)
        logger.warning(
            "WEBHOOK_SECRET not configured, skipping validation",
            extra={"case_id": "N/A", "operation_type": "webhook_validation_skip"},
        )
        return True

    # Normalize header keys to lowercase for case-insensitive lookup
    normalized_headers = {k.lower(): v for k, v in headers.items()}

    signature = normalized_headers.get("x-ragic-signature", "")
    if not signature:
        return False

    # Compute expected HMAC-SHA256 signature
    expected_signature = hmac.new(
        key=WEBHOOK_SECRET.encode("utf-8"),
        msg=body.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(signature, expected_signature)


def classify_webhook_event(payload: dict) -> WebhookEventType:
    """Classify a RAGIC webhook payload into one of 5 event types.

    Classification logic is based on the form path (sheet) and field content:
    - business-process2/2 (Case Management Form):
      - If record is newly created → NEW_CASE_CREATED
      - If status field changed → CASE_STATUS_CHANGED
    - work-survey/7 (Questionnaire Form):
      - If case_type is "續約" → RENEWAL_QUESTIONNAIRE
      - If case_type is "新約" → NEW_CONTRACT_FULL_QUESTIONNAIRE
    - Supplement form (is_supplement flag):
      - → SUPPLEMENTARY_QUESTIONNAIRE

    Args:
        payload: Parsed webhook payload dict.

    Returns:
        The classified WebhookEventType.
    """
    form_path = payload.get("form_path", "")
    action = payload.get("action", "")

    # Check for supplement questionnaire first (highest specificity)
    if payload.get(IS_SUPPLEMENT_FIELD):
        return WebhookEventType.SUPPLEMENTARY_QUESTIONNAIRE

    # Case Management Form events (business-process2/2)
    if CASE_MANAGEMENT_FORM_PATH in form_path:
        # Check if status field indicates a newly created case
        case_status = payload.get(CASE_STATUS_FIELD, "")
        if case_status == NEW_CASE_STATUS_VALUE:
            return WebhookEventType.NEW_CASE_CREATED
        if action == "create" or payload.get("is_new_record"):
            return WebhookEventType.NEW_CASE_CREATED
        return WebhookEventType.CASE_STATUS_CHANGED

    # Questionnaire Form events (work-survey/7)
    if QUESTIONNAIRE_FORM_PATH in form_path:
        case_type = payload.get(CASE_TYPE_FIELD, "")
        if case_type == "續約":
            return WebhookEventType.RENEWAL_QUESTIONNAIRE
        return WebhookEventType.NEW_CONTRACT_FULL_QUESTIONNAIRE

    # Default: treat as status change if form path is unrecognized
    # This handles edge cases where RAGIC sends events from related forms
    if action == "create" or payload.get("is_new_record"):
        return WebhookEventType.NEW_CASE_CREATED

    return WebhookEventType.CASE_STATUS_CHANGED


def _get_target_function(event_type: WebhookEventType) -> str:
    """Determine the downstream Lambda function name for a given event type.

    Args:
        event_type: The classified webhook event type.

    Returns:
        The Lambda function name to invoke, or empty string if not configured.
    """
    # AI determination handles questionnaire-related events
    ai_events = {
        WebhookEventType.NEW_CONTRACT_FULL_QUESTIONNAIRE,
        WebhookEventType.SUPPLEMENTARY_QUESTIONNAIRE,
    }

    # Workflow engine handles case lifecycle events
    workflow_events = {
        WebhookEventType.NEW_CASE_CREATED,
        WebhookEventType.CASE_STATUS_CHANGED,
        WebhookEventType.RENEWAL_QUESTIONNAIRE,
    }

    if event_type in ai_events:
        return AI_DETERMINATION_FUNCTION
    if event_type in workflow_events:
        return WORKFLOW_ENGINE_FUNCTION

    return ""
