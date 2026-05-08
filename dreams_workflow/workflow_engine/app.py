"""Workflow Engine Lambda Function.

Routes incoming events from webhook_handler to the appropriate workflow
handler based on event type. Manages case lifecycle including new case
creation, questionnaire responses, and status changes.

Requirements: 1.5, 1.6, 2.4, 2.5, 2.6, 2.7, 2.8
"""

from __future__ import annotations

import json
import os
from typing import Any

import boto3

from dreams_workflow.shared.exceptions import (
    EmailSendError,
    RagicCommunicationError,
)
from dreams_workflow.shared.logger import get_logger, log_operation
from dreams_workflow.shared.models import CaseStatus, CaseType, EmailType, WebhookEventType
from dreams_workflow.shared.ragic_client import CloudRagicClient
from dreams_workflow.shared.state_machine import transition_case_status

logger = get_logger(__name__)

# Environment variables
EMAIL_SERVICE_FUNCTION = os.environ.get("EMAIL_SERVICE_FUNCTION_NAME", "")
AI_DETERMINATION_FUNCTION = os.environ.get("AI_DETERMINATION_FUNCTION_NAME", "")

# Lazy-initialized Lambda client
_lambda_client = None


def _get_lambda_client():
    """Get or create the boto3 Lambda client."""
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda")
    return _lambda_client


def lambda_handler(event: dict, context: Any) -> dict:
    """Workflow Engine Lambda entry point.

    Receives events from webhook_handler and routes to the appropriate
    handler function based on event_type.

    Event format (from webhook_handler):
        {
            "event_type": "NEW_CASE_CREATED",
            "payload": {...},
            "case_id": "..."
        }

    Args:
        event: Event dict containing event_type, payload, and case_id.
        context: Lambda execution context.

    Returns:
        Dict with statusCode and body.
    """
    event_type_str = event.get("event_type", "")
    payload = event.get("payload", {})
    case_id = event.get("case_id", "unknown")

    log_operation(
        logger,
        case_id=case_id,
        operation_type="workflow_engine_invoke",
        message=f"Workflow engine invoked with event_type: {event_type_str}",
    )

    try:
        event_type = WebhookEventType(event_type_str)
    except ValueError:
        logger.error(
            f"Unknown event type: {event_type_str}",
            extra={"case_id": case_id, "operation_type": "workflow_engine_error"},
        )
        return {
            "statusCode": 400,
            "body": json.dumps({"error": f"Unknown event type: {event_type_str}"}),
        }

    try:
        if event_type == WebhookEventType.NEW_CASE_CREATED:
            result = handle_new_case(case_id, payload)
        elif event_type == WebhookEventType.RENEWAL_QUESTIONNAIRE:
            result = handle_questionnaire_response(case_id, payload, is_renewal=True)
        elif event_type == WebhookEventType.CASE_STATUS_CHANGED:
            result = handle_status_change(case_id, payload)
        else:
            # Other event types (NEW_CONTRACT_FULL_QUESTIONNAIRE,
            # SUPPLEMENTARY_QUESTIONNAIRE) are handled by ai_determination
            result = {"message": f"Event type {event_type.value} not handled by workflow_engine"}

        return {
            "statusCode": 200,
            "body": json.dumps(result, ensure_ascii=False),
        }

    except Exception as e:
        logger.error(
            f"Workflow engine error: {e}",
            extra={"case_id": case_id, "operation_type": "workflow_engine_error"},
            exc_info=True,
        )
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(e)}),
        }


# =============================================================================
# Event Handlers
# =============================================================================


def handle_new_case(case_id: str, payload: dict) -> dict:
    """Handle NEW_CASE_CREATED event.

    Flow:
    1. Send questionnaire notification email to customer
    2. Update case status to "待填問卷"

    Note: The case record has already been created in RAGIC by the
    shipment scanner service (Task 13). This handler only sends the
    notification and updates the status.

    Args:
        case_id: The RAGIC case record ID.
        payload: Webhook payload containing case data.

    Returns:
        Result dict with operation outcome.

    Requirements: 1.5, 1.6
    """
    log_operation(
        logger,
        case_id=case_id,
        operation_type="handle_new_case",
        message="Processing new case creation event",
    )

    # Extract customer email from payload
    customer_email = payload.get("customer_email", "")
    if not customer_email:
        # Try to get from RAGIC if not in payload
        customer_email = _get_customer_email(case_id)

    if not customer_email:
        log_operation(
            logger,
            case_id=case_id,
            operation_type="handle_new_case_error",
            message="Cannot send questionnaire notification: no customer email found",
            level="error",
        )
        return {"error": "No customer email found", "case_id": case_id}

    # Step 1: Send questionnaire notification email
    _invoke_email_service(
        case_id=case_id,
        email_type=EmailType.QUESTIONNAIRE_NOTIFICATION,
        recipient_email=customer_email,
        template_data={
            "customer_name": payload.get("customer_name", ""),
            "case_id": case_id,
        },
    )

    # Step 2: Update case status to "待填問卷"
    ragic_client = CloudRagicClient()
    try:
        transition_case_status(
            case_id=case_id,
            new_status=CaseStatus.PENDING_QUESTIONNAIRE,
            reason="問卷通知已發送，等待客戶填寫",
            current_status=CaseStatus.NEW_CASE_CREATED,
            store=ragic_client,
        )
    finally:
        ragic_client.close()

    log_operation(
        logger,
        case_id=case_id,
        operation_type="handle_new_case_complete",
        message="New case processed: questionnaire notification sent, status updated to 待填問卷",
    )

    return {
        "case_id": case_id,
        "action": "new_case_processed",
        "email_sent_to": customer_email,
        "new_status": CaseStatus.PENDING_QUESTIONNAIRE.value,
    }


def handle_questionnaire_response(
    case_id: str, payload: dict, *, is_renewal: bool = False
) -> dict:
    """Handle questionnaire response events.

    Routes based on case type:
    - Renewal (續約): Update status to "續約處理"
    - New contract (新約): Trigger AI determination (handled by ai_determination Lambda)

    Args:
        case_id: The RAGIC case record ID.
        payload: Webhook payload containing questionnaire data.
        is_renewal: Whether this is a renewal questionnaire response.

    Returns:
        Result dict with operation outcome.

    Requirements: 2.4, 2.5, 2.6, 2.7, 2.8
    """
    log_operation(
        logger,
        case_id=case_id,
        operation_type="handle_questionnaire_response",
        message=f"Processing questionnaire response (is_renewal={is_renewal})",
    )

    if is_renewal:
        # Renewal case: update status to "續約處理"
        ragic_client = CloudRagicClient()
        try:
            transition_case_status(
                case_id=case_id,
                new_status=CaseStatus.RENEWAL_PROCESSING,
                reason="續約案件，客戶已填寫電號",
                current_status=CaseStatus.PENDING_QUESTIONNAIRE,
                store=ragic_client,
            )
        finally:
            ragic_client.close()

        log_operation(
            logger,
            case_id=case_id,
            operation_type="handle_questionnaire_response_complete",
            message="Renewal case: status updated to 續約處理",
        )

        return {
            "case_id": case_id,
            "action": "renewal_processing",
            "new_status": CaseStatus.RENEWAL_PROCESSING.value,
        }

    else:
        # New contract case: trigger AI determination
        # This is handled by ai_determination Lambda (invoked by webhook_handler)
        # If we receive it here, invoke ai_determination
        _invoke_ai_determination(case_id, payload)

        log_operation(
            logger,
            case_id=case_id,
            operation_type="handle_questionnaire_response_complete",
            message="New contract case: AI determination triggered",
        )

        return {
            "case_id": case_id,
            "action": "ai_determination_triggered",
        }


def handle_status_change(case_id: str, payload: dict) -> dict:
    """Handle CASE_STATUS_CHANGED event.

    Routes to the appropriate sub-flow based on the new status value:
    - 台電審核: Taipower review flow (Task 9)
    - 資訊補件: Supplement flow (Task 8.3)
    - 發送前人工確認: Taipower reply analyzed, waiting for manual confirm
    - 台電補件: Taipower supplement flow (Task 9.3)
    - 安裝階段: Installation flow (Task 12)

    Also resolves the RAGIC record ID from DREAMS_APPLY_ID in the payload.

    Args:
        case_id: The RAGIC case record ID.
        payload: Webhook payload containing status change data.

    Returns:
        Result dict with operation outcome.

    Requirements: 3.4, 3.5, 3.6, 10.4
    """
    new_status_value = payload.get("case_status", payload.get("status", ""))

    # Resolve RAGIC record ID from DREAMS_APPLY_ID if available
    dreams_apply_id = payload.get("1016557", payload.get("dreams_apply_id", ""))
    if dreams_apply_id and "-" in dreams_apply_id:
        # Split by "-" and take the last segment as record ID
        parts = dreams_apply_id.split("-")
        resolved_record_id = parts[-1] if len(parts) >= 2 else case_id
        log_operation(
            logger,
            case_id=case_id,
            operation_type="resolve_record_id",
            message=f"Resolved record ID from DREAMS_APPLY_ID: {resolved_record_id}",
        )
    else:
        resolved_record_id = case_id

    log_operation(
        logger,
        case_id=case_id,
        operation_type="handle_status_change",
        message=f"Processing status change to: {new_status_value}",
    )

    # Route based on new status
    if new_status_value == CaseStatus.INFO_SUPPLEMENT.value:
        return _handle_info_supplement(resolved_record_id, payload)
    elif new_status_value == CaseStatus.TAIPOWER_REVIEW.value:
        return _handle_taipower_review_trigger(resolved_record_id, payload)
    elif new_status_value == CaseStatus.PRE_SEND_CONFIRM.value:
        return _handle_pre_send_confirm(resolved_record_id, payload)
    elif new_status_value == CaseStatus.TAIPOWER_SUPPLEMENT.value:
        return _handle_taipower_supplement_trigger(resolved_record_id, payload)
    elif new_status_value == CaseStatus.INSTALLATION_PHASE.value:
        return _handle_installation_trigger(resolved_record_id, payload)
    else:
        log_operation(
            logger,
            case_id=case_id,
            operation_type="handle_status_change",
            message=f"No specific handler for status: {new_status_value}",
            level="warning",
        )
        return {
            "case_id": case_id,
            "action": "status_change_acknowledged",
            "new_status": new_status_value,
        }


# =============================================================================
# Status Change Sub-Handlers (stubs for future tasks)
# =============================================================================


def _handle_info_supplement(case_id: str, payload: dict) -> dict:
    """Handle transition to 資訊補件 status.

    Reads the questionnaire result fields from the case management form,
    identifies fields with "Fail" or "Yes" values, builds the supplement
    parameter codes (A~Q joined by |), and sends a supplement notification
    email with the supplement questionnaire link containing pfv params.

    Args:
        case_id: The RAGIC case record ID.
        payload: Webhook payload.

    Returns:
        Result dict.

    Requirements: 4.1, 4.2, 4.3
    """
    from dreams_workflow.ai_determination.field_mapping_loader import (
        build_supplement_params,
        get_questionnaire_result_mapping,
        get_supplement_params_separator,
    )

    log_operation(
        logger,
        case_id=case_id,
        operation_type="handle_info_supplement",
        message="Processing info supplement flow",
    )

    # Read questionnaire result fields from payload or fetch from RAGIC
    result_mapping = get_questionnaire_result_mapping()
    result_fields: dict[str, str] = {}

    # Try to get result values from payload first
    for q_field_id, result_field_id in result_mapping.items():
        value = payload.get(result_field_id, "")
        if value:
            result_fields[result_field_id] = value

    # If not in payload, fetch from RAGIC
    if not result_fields:
        ragic_client = CloudRagicClient()
        try:
            case_data = ragic_client.get_questionnaire_data(case_id)
            for q_field_id, result_field_id in result_mapping.items():
                value = case_data.get(result_field_id, "")
                if value:
                    result_fields[result_field_id] = value
        finally:
            ragic_client.close()

    # Build supplement params string (A|B|F format)
    supplement_params = build_supplement_params(result_fields, result_type="questionnaire")

    if not supplement_params:
        log_operation(
            logger,
            case_id=case_id,
            operation_type="handle_info_supplement_warning",
            message="No Fail/Yes fields found in questionnaire results",
            level="warning",
        )

    # Get case info for email
    shipment_order_id = payload.get("shipment_order_id", payload.get("1015021", ""))
    dreams_apply_id = payload.get("dreams_apply_id", payload.get("1016557", ""))
    customer_email = payload.get("customer_email", "")

    if not customer_email:
        customer_email = _get_customer_email(case_id)

    # Send supplement notification email with pfv params
    _invoke_email_service(
        case_id=case_id,
        email_type=EmailType.SUPPLEMENT_NOTIFICATION,
        recipient_email=customer_email,
        template_data={
            "case_id": case_id,
            "supplement_params": supplement_params,
            "shipment_order_id": shipment_order_id,
            "dreams_apply_id": dreams_apply_id,
            "customer_name": payload.get("customer_name", ""),
        },
    )

    log_operation(
        logger,
        case_id=case_id,
        operation_type="handle_info_supplement_complete",
        message=f"Supplement notification sent, params: {supplement_params}",
    )

    return {
        "case_id": case_id,
        "action": "info_supplement_processed",
        "supplement_params": supplement_params,
        "email_sent_to": customer_email,
    }


def _handle_taipower_review_trigger(case_id: str, payload: dict) -> dict:
    """Handle transition to 台電審核 status.

    Delegates to the taipower_flow module which calls the DREAMS Form API
    and handles the response (success → send email, no number → notify).
    """
    from dreams_workflow.workflow_engine.taipower_flow import handle_taipower_review

    return handle_taipower_review(case_id, payload)


def _handle_pre_send_confirm(case_id: str, payload: dict) -> dict:
    """Handle transition to 發送前人工確認 status.

    This status is set automatically after taipower reply semantic analysis.
    No action needed from workflow_engine — the company contact will review
    in RAGIC and manually change to 安裝階段 or 台電補件.
    """
    log_operation(
        logger,
        case_id=case_id,
        operation_type="handle_pre_send_confirm",
        message="Pre-send confirm status acknowledged, waiting for manual decision",
    )
    return {
        "case_id": case_id,
        "action": "pre_send_confirm_acknowledged",
        "message": "等待人工在 RAGIC 確認台電審核結果",
    }


def _handle_taipower_supplement_trigger(case_id: str, payload: dict) -> dict:
    """Handle transition to 台電補件 status.

    Reads the taipower review result fields from the case management form,
    identifies fields with "Fail" or "Yes" values, builds the supplement
    parameter codes (A~Q joined by |), and sends a taipower supplement
    notification email with the supplement questionnaire link.

    Args:
        case_id: The RAGIC case record ID.
        payload: Webhook payload.

    Returns:
        Result dict.

    Requirements: 6.5, 7.1, 7.2, 7.3
    """
    from dreams_workflow.ai_determination.field_mapping_loader import (
        build_supplement_params,
        get_taipower_result_mapping,
    )

    log_operation(
        logger,
        case_id=case_id,
        operation_type="handle_taipower_supplement",
        message="Processing taipower supplement flow",
    )

    # Read taipower result fields from payload or fetch from RAGIC
    result_mapping = get_taipower_result_mapping()
    result_fields: dict[str, str] = {}

    for q_field_id, result_field_id in result_mapping.items():
        value = payload.get(result_field_id, "")
        if value:
            result_fields[result_field_id] = value

    if not result_fields:
        ragic_client = CloudRagicClient()
        try:
            case_data = ragic_client.get_questionnaire_data(case_id)
            for q_field_id, result_field_id in result_mapping.items():
                value = case_data.get(result_field_id, "")
                if value:
                    result_fields[result_field_id] = value
        finally:
            ragic_client.close()

    # Build supplement params string (A|B|F format)
    supplement_params = build_supplement_params(result_fields, result_type="taipower")

    if not supplement_params:
        log_operation(
            logger,
            case_id=case_id,
            operation_type="handle_taipower_supplement_warning",
            message="No Fail/Yes fields found in taipower results",
            level="warning",
        )

    # Get case info for email
    shipment_order_id = payload.get("shipment_order_id", payload.get("1015021", ""))
    dreams_apply_id = payload.get("dreams_apply_id", payload.get("1016557", ""))
    customer_email = payload.get("customer_email", "")

    if not customer_email:
        customer_email = _get_customer_email(case_id)

    # Send taipower supplement notification email
    _invoke_email_service(
        case_id=case_id,
        email_type=EmailType.TAIPOWER_SUPPLEMENT,
        recipient_email=customer_email,
        template_data={
            "case_id": case_id,
            "supplement_params": supplement_params,
            "shipment_order_id": shipment_order_id,
            "dreams_apply_id": dreams_apply_id,
            "customer_name": payload.get("customer_name", ""),
        },
    )

    log_operation(
        logger,
        case_id=case_id,
        operation_type="handle_taipower_supplement_complete",
        message=f"Taipower supplement notification sent, params: {supplement_params}",
    )

    return {
        "case_id": case_id,
        "action": "taipower_supplement_processed",
        "supplement_params": supplement_params,
        "email_sent_to": customer_email,
    }


def _handle_installation_trigger(case_id: str, payload: dict) -> dict:
    """Handle transition to 安裝階段 status.

    Delegates to the installation_flow module.
    """
    from dreams_workflow.workflow_engine.installation_flow import handle_installation_phase

    return handle_installation_phase(case_id, payload)


# =============================================================================
# Helper Functions
# =============================================================================


def _get_customer_email(case_id: str) -> str:
    """Retrieve customer email from RAGIC case record.

    Args:
        case_id: The RAGIC case record ID.

    Returns:
        Customer email string, or empty string if not found.
    """
    try:
        ragic_client = CloudRagicClient()
        try:
            record = ragic_client.get_questionnaire_data(case_id)
            return record.get("customer_email", record.get("email", ""))
        finally:
            ragic_client.close()
    except RagicCommunicationError as e:
        logger.error(
            f"Failed to get customer email for case {case_id}: {e}",
            extra={"case_id": case_id, "operation_type": "get_customer_email_error"},
        )
        return ""


def _invoke_email_service(
    case_id: str,
    email_type: EmailType,
    recipient_email: str,
    template_data: dict,
    attachments: list[dict] | None = None,
) -> None:
    """Invoke the email service Lambda function.

    Args:
        case_id: Case identifier.
        email_type: Type of email to send.
        recipient_email: Recipient email address.
        template_data: Template rendering data.
        attachments: Optional list of attachment dicts.
    """
    if not EMAIL_SERVICE_FUNCTION:
        logger.warning(
            "EMAIL_SERVICE_FUNCTION_NAME not configured, skipping email send",
            extra={"case_id": case_id, "operation_type": "email_invoke_skip"},
        )
        return

    invoke_payload = {
        "email_type": email_type.value,
        "case_id": case_id,
        "recipient_email": recipient_email,
        "template_data": template_data,
    }
    if attachments:
        invoke_payload["attachments"] = attachments

    try:
        response = _get_lambda_client().invoke(
            FunctionName=EMAIL_SERVICE_FUNCTION,
            InvocationType="Event",  # Asynchronous
            Payload=json.dumps(invoke_payload, ensure_ascii=False).encode("utf-8"),
        )
        log_operation(
            logger,
            case_id=case_id,
            operation_type="email_service_invoked",
            message=f"Email service invoked for {email_type.value}, StatusCode: {response['StatusCode']}",
        )
    except Exception as e:
        logger.error(
            f"Failed to invoke email service: {e}",
            extra={"case_id": case_id, "operation_type": "email_invoke_error"},
        )


def _invoke_ai_determination(case_id: str, payload: dict) -> None:
    """Invoke the AI determination Lambda function.

    Args:
        case_id: Case identifier.
        payload: Questionnaire data payload.
    """
    if not AI_DETERMINATION_FUNCTION:
        logger.warning(
            "AI_DETERMINATION_FUNCTION_NAME not configured, skipping AI determination",
            extra={"case_id": case_id, "operation_type": "ai_invoke_skip"},
        )
        return

    invoke_payload = {
        "event_type": WebhookEventType.NEW_CONTRACT_FULL_QUESTIONNAIRE.value,
        "payload": payload,
        "case_id": case_id,
    }

    try:
        response = _get_lambda_client().invoke(
            FunctionName=AI_DETERMINATION_FUNCTION,
            InvocationType="Event",  # Asynchronous
            Payload=json.dumps(invoke_payload, ensure_ascii=False).encode("utf-8"),
        )
        log_operation(
            logger,
            case_id=case_id,
            operation_type="ai_determination_invoked",
            message=f"AI determination invoked, StatusCode: {response['StatusCode']}",
        )
    except Exception as e:
        logger.error(
            f"Failed to invoke AI determination: {e}",
            extra={"case_id": case_id, "operation_type": "ai_invoke_error"},
        )
