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

# Simple in-memory deduplication cache (per Lambda instance)
# Stores (case_id, event_type) → timestamp to prevent processing the same
# event multiple times within a short window (e.g., RAGIC retry storms)
_recent_events: dict[str, float] = {}
_DEDUP_WINDOW_SECONDS = 60  # Ignore duplicate events within 60 seconds


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

# RAGIC form path constants (matched against payload "path" + "sheetIndex")
CASE_MANAGEMENT_FORM_PATH = "/business-process2"
CASE_MANAGEMENT_SHEET_INDEX = 2
QUESTIONNAIRE_FORM_PATH = "/work-survey"
QUESTIONNAIRE_SHEET_INDEX = 7
SUPPLEMENT_FORM_PATH = "/work-survey"
SUPPLEMENT_SHEET_INDEX = 9

# Field identifiers used for event classification
# Read from ragic_fields.yaml to avoid hardcoding RAGIC field IDs
def _get_case_status_field_id() -> str:
    """Get case status field ID from config (lazy loaded)."""
    try:
        from dreams_workflow.shared.ragic_fields_config import get_field_id
        return get_field_id("case_management", "case_status", "1015456")
    except Exception:
        return "1015456"

CASE_TYPE_FIELD = "case_type"

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

    # DEBUG: Log raw payload for troubleshooting RAGIC webhook format
    logger.info(
        f"DEBUG raw_body: {raw_body[:2000]}",
        extra={"case_id": "N/A", "operation_type": "webhook_debug_payload"},
    )

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

    # Extract RAGIC webhook structure:
    # {
    #   "data": [{ "_ragicId": 13, "1015456": "新開案件", ... }],
    #   "path": "/business-process2",
    #   "sheetIndex": 2,
    #   "eventType": "update" | "create",
    #   "apname": "solarcs",
    #   ...
    # }
    ragic_meta = {
        "path": payload.get("path", ""),
        "sheetIndex": payload.get("sheetIndex", 0),
        "eventType": payload.get("eventType", ""),
        "apname": payload.get("apname", ""),
    }

    # Extract the first record from data array (RAGIC sends one record per webhook)
    data_list = payload.get("data", [])
    record_data = data_list[0] if data_list else {}

    # Get case_id from _ragicId
    case_id = str(record_data.get("_ragicId", "unknown"))

    # Classify event type using metadata + record data
    event_type = classify_webhook_event(ragic_meta, record_data)

    log_operation(
        logger,
        case_id=case_id,
        operation_type="webhook_classified",
        message=f"Event classified as {event_type.value} (path={ragic_meta['path']}, sheet={ragic_meta['sheetIndex']}, ragicId={case_id})",
    )

    # Deduplication: skip if we already processed this exact event recently
    # For CASE_STATUS_CHANGED, include the status value in the key so
    # different status changes don't block each other
    import time

    if event_type == WebhookEventType.CASE_STATUS_CHANGED:
        case_status_field = _get_case_status_field_id()
        status_value = record_data.get(case_status_field, "")
        dedup_key = f"{case_id}:{event_type.value}:{status_value}"
    else:
        dedup_key = f"{case_id}:{event_type.value}"
    now = time.time()
    last_processed = _recent_events.get(dedup_key)
    if last_processed and (now - last_processed) < _DEDUP_WINDOW_SECONDS:
        logger.info(
            f"Duplicate event skipped: {dedup_key} (last processed {now - last_processed:.1f}s ago)",
            extra={"case_id": case_id, "operation_type": "webhook_dedup_skip"},
        )
        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "Duplicate event skipped",
                "event_type": event_type.value,
                "case_id": case_id,
            }),
        }
    _recent_events[dedup_key] = now

    # Cleanup old entries to prevent memory leak
    cutoff = now - _DEDUP_WINDOW_SECONDS * 2
    _recent_events.update(
        {k: v for k, v in _recent_events.items() if v > cutoff}
    )

    # Route to appropriate downstream Lambda
    target_function = _get_target_function(event_type)
    if not target_function:
        logger.error(
            f"No target function configured for event type: {event_type.value}",
            extra={"case_id": case_id, "operation_type": "webhook_routing_error"},
        )
        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "Webhook received, but no handler configured",
                "event_type": event_type.value,
            }),
        }

    # Build downstream payload with flattened record data
    invoke_payload = {
        "event_type": event_type.value,
        "payload": record_data,
        "case_id": case_id,
        "ragic_meta": ragic_meta,
    }

    try:
        response = _get_lambda_client().invoke(
            FunctionName=target_function,
            InvocationType="Event",  # Asynchronous invocation
            Payload=json.dumps(invoke_payload, ensure_ascii=False).encode("utf-8"),
        )
        log_operation(
            logger,
            case_id=case_id,
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
                "case_id": case_id,
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
            "case_id": case_id,
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
    if not WEBHOOK_SECRET or WEBHOOK_SECRET == "skip":
        # No secret configured or set to "skip" — skip validation (development/testing mode)
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


def classify_webhook_event(ragic_meta: dict, record_data: dict) -> WebhookEventType:
    """Classify a RAGIC webhook payload into one of 5 event types.

    RAGIC webhook format:
    - ragic_meta: {"path": "/business-process2", "sheetIndex": 2, "eventType": "update"}
    - record_data: {"_ragicId": 13, "1015456": "新開案件", ...}

    Classification logic:
    - Case Management Form (path=/business-process2, sheetIndex=2):
      - If status field (from config: case_status) = "新開案件" → NEW_CASE_CREATED
      - Otherwise → CASE_STATUS_CHANGED
    - Questionnaire Form (path=/work-survey, sheetIndex=7):
      - If case_type field indicates renewal → RENEWAL_QUESTIONNAIRE
      - Otherwise → NEW_CONTRACT_FULL_QUESTIONNAIRE
    - Supplement Form (path=/work-survey, sheetIndex=9):
      - → SUPPLEMENTARY_QUESTIONNAIRE

    Args:
        ragic_meta: RAGIC webhook metadata (path, sheetIndex, eventType).
        record_data: The first record from the data array.

    Returns:
        The classified WebhookEventType.
    """
    path = ragic_meta.get("path", "")
    sheet_index = ragic_meta.get("sheetIndex", 0)
    event_type = ragic_meta.get("eventType", "")

    # Supplement Form (work-survey/9)
    if SUPPLEMENT_FORM_PATH in path and sheet_index == SUPPLEMENT_SHEET_INDEX:
        return WebhookEventType.SUPPLEMENTARY_QUESTIONNAIRE

    # Case Management Form (business-process2/2)
    if CASE_MANAGEMENT_FORM_PATH in path and sheet_index == CASE_MANAGEMENT_SHEET_INDEX:
        case_status_field = _get_case_status_field_id()
        case_status = record_data.get(case_status_field, "")
        if case_status == NEW_CASE_STATUS_VALUE:
            return WebhookEventType.NEW_CASE_CREATED
        return WebhookEventType.CASE_STATUS_CHANGED

    # Questionnaire Form (work-survey/7)
    if QUESTIONNAIRE_FORM_PATH in path and sheet_index == QUESTIONNAIRE_SHEET_INDEX:
        # Use field 1016556 (DREAMS流程) to determine new/renewal
        dreams_flow_field = record_data.get("1016556", "")
        if dreams_flow_field == "案場續約":
            return WebhookEventType.RENEWAL_QUESTIONNAIRE
        return WebhookEventType.NEW_CONTRACT_FULL_QUESTIONNAIRE

    # Default: treat as status change
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
