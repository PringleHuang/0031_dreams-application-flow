"""Taipower review flow handler.

Handles the 台電審核 stage: calls the DREAMS Form API to submit the
application, then either sends the review request email to Taipower
(on success) or sends an electricity number creation request (if no number).

Requirements: 5.1, 5.2, 5.3, 5.4
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

import boto3

from dreams_workflow.dreams_client.client import (
    DreamsApiClient,
    DreamsApiResponse,
    ERROR_NO_ELECTRICITY_NUMBER,
)
from dreams_workflow.shared.exceptions import DreamsConnectionError
from dreams_workflow.shared.logger import get_logger, log_operation
from dreams_workflow.shared.models import EmailType
from dreams_workflow.shared.ragic_client import CloudRagicClient

logger = get_logger(__name__)

# Environment variables
EMAIL_SERVICE_FUNCTION = os.environ.get("EMAIL_SERVICE_FUNCTION_NAME", "")

# Lazy-initialized Lambda client
_lambda_client = None


def _get_lambda_client():
    """Get or create the boto3 Lambda client."""
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda")
    return _lambda_client


def handle_taipower_review(case_id: str, payload: dict) -> dict:
    """Handle the 台電審核 flow.

    Flow:
    1. Call DREAMS Form API with case data
    2. If success (case_number + PDF):
       - Write case_number to RAGIC
       - Send review request email to Taipower (with attachments: supporting docs + PDF)
    3. If no electricity number:
       - Send electricity number creation request to Taipower review contact

    Args:
        case_id: The RAGIC case record ID.
        payload: Webhook payload containing case data.

    Returns:
        Result dict with operation outcome.

    Requirements: 5.1, 5.2, 5.3, 5.4
    """
    log_operation(
        logger,
        case_id=case_id,
        operation_type="taipower_review_start",
        message="Starting Taipower review flow",
    )

    # Gather case data for DREAMS API
    case_data = _build_case_data(case_id, payload)

    # Call DREAMS Form API
    dreams_client = DreamsApiClient()
    try:
        response = dreams_client.submit_application(case_id, case_data)
    except DreamsConnectionError as e:
        log_operation(
            logger,
            case_id=case_id,
            operation_type="taipower_review_error",
            message=f"DREAMS API call failed after retries: {e}",
            level="error",
        )
        return {
            "case_id": case_id,
            "action": "taipower_review_failed",
            "error": str(e),
        }

    # Handle API response
    if response.success:
        return _handle_api_success(case_id, payload, response)
    elif response.error_code == ERROR_NO_ELECTRICITY_NUMBER:
        return _handle_no_electricity_number(case_id, payload)
    else:
        log_operation(
            logger,
            case_id=case_id,
            operation_type="taipower_review_api_error",
            message=f"DREAMS API error: {response.error_code} - {response.error_message}",
            level="error",
        )
        return {
            "case_id": case_id,
            "action": "taipower_review_api_error",
            "error_code": response.error_code,
            "error_message": response.error_message,
        }


def _build_case_data(case_id: str, payload: dict) -> dict:
    """Build case data dict for DREAMS Form API submission.

    Extracts relevant fields from the webhook payload or fetches from RAGIC.

    Args:
        case_id: The RAGIC case record ID.
        payload: Webhook payload.

    Returns:
        Dict with case data fields for the DREAMS API.
    """
    # Extract from payload (field IDs from RAGIC case management form)
    case_data = {
        "electricity_number": payload.get("1015407", payload.get("electricity_number", "")),
        "customer_name": payload.get("1015398", payload.get("customer_name", "")),
        "site_address": payload.get("1015399", payload.get("site_address", "")),
        "site_name": payload.get("1014670", payload.get("site_name", "")),
        "capacity_kw": payload.get("1015409", payload.get("capacity_kw", "")),
        "connection_method": payload.get("1015415", payload.get("connection_method", "")),
        "selling_method": payload.get("1015414", payload.get("selling_method", "")),
    }

    # If key fields are missing, try to fetch from RAGIC
    if not case_data["electricity_number"]:
        try:
            ragic_client = CloudRagicClient()
            try:
                record = ragic_client.get_questionnaire_data(case_id)
                case_data["electricity_number"] = record.get("1015407", "")
                if not case_data["customer_name"]:
                    case_data["customer_name"] = record.get("1015398", "")
                if not case_data["site_address"]:
                    case_data["site_address"] = record.get("1015399", "")
            finally:
                ragic_client.close()
        except Exception as e:
            log_operation(
                logger,
                case_id=case_id,
                operation_type="build_case_data_warning",
                message=f"Failed to fetch case data from RAGIC: {e}",
                level="warning",
            )

    return case_data


def _handle_api_success(
    case_id: str, payload: dict, response: DreamsApiResponse
) -> dict:
    """Handle successful DREAMS API response.

    1. Write case_number to RAGIC
    2. Download supporting documents from RAGIC
    3. Send review request email to Taipower with all attachments

    Args:
        case_id: The RAGIC case record ID.
        payload: Webhook payload.
        response: Successful DreamsApiResponse.

    Returns:
        Result dict.
    """
    log_operation(
        logger,
        case_id=case_id,
        operation_type="taipower_review_success",
        message=f"DREAMS API success, case_number: {response.case_number}",
    )

    # Step 1: Write case_number to RAGIC
    ragic_client = CloudRagicClient()
    try:
        ragic_client.update_case_record(case_id, {
            "dreams_case_id": response.case_number,
        })
        log_operation(
            logger,
            case_id=case_id,
            operation_type="case_number_written",
            message=f"DREAMS case number written to RAGIC: {response.case_number}",
        )
    except Exception as e:
        log_operation(
            logger,
            case_id=case_id,
            operation_type="case_number_write_error",
            message=f"Failed to write case number to RAGIC: {e}",
            level="error",
        )
    finally:
        ragic_client.close()

    # Step 2: Get supporting documents from RAGIC
    attachments = _get_supporting_document_attachments(case_id)

    # Step 3: Add the application PDF as attachment
    if response.pdf_base64:
        attachments.append({
            "filename": f"申請資料_{response.case_number}.pdf",
            "content_base64": response.pdf_base64,
            "content_type": "application/pdf",
        })

    # Step 4: Send review request email to Taipower
    taipower_email = payload.get("taipower_contact_email", "")
    if not taipower_email:
        taipower_email = os.environ.get("TAIPOWER_BUSINESS_CONTACT_EMAIL", "")

    if taipower_email:
        _invoke_email_service(
            case_id=case_id,
            email_type=EmailType.TAIPOWER_APPLICATION,
            recipient_email=taipower_email,
            template_data={
                "case_id": case_id,
                "case_number": response.case_number,
                "customer_name": payload.get("customer_name", ""),
                "site_name": payload.get("site_name", ""),
                "electricity_number": payload.get("electricity_number", ""),
            },
            attachments=attachments,
        )
    else:
        log_operation(
            logger,
            case_id=case_id,
            operation_type="taipower_email_skip",
            message="No Taipower contact email configured, skipping email send",
            level="warning",
        )

    return {
        "case_id": case_id,
        "action": "taipower_review_submitted",
        "case_number": response.case_number,
        "email_sent_to": taipower_email,
        "attachments_count": len(attachments),
    }


def _handle_no_electricity_number(case_id: str, payload: dict) -> dict:
    """Handle DREAMS API response indicating no electricity number.

    Sends a notification to the Taipower review contact requesting
    electricity number creation.

    Args:
        case_id: The RAGIC case record ID.
        payload: Webhook payload.

    Returns:
        Result dict.
    """
    log_operation(
        logger,
        case_id=case_id,
        operation_type="no_electricity_number",
        message="Electricity number not found in DREAMS, sending creation request",
    )

    # Send notification to Taipower review contact
    taipower_review_email = os.environ.get("TAIPOWER_REVIEW_CONTACT_EMAIL", "")

    if taipower_review_email:
        _invoke_email_service(
            case_id=case_id,
            email_type=EmailType.TAIPOWER_APPLICATION,
            recipient_email=taipower_review_email,
            template_data={
                "case_id": case_id,
                "customer_name": payload.get("customer_name", ""),
                "electricity_number": payload.get("electricity_number", ""),
                "message": "電號尚未建立於 DREAMS 系統，請協助建立後通知。",
                "is_electricity_number_request": True,
            },
            attachments=None,
        )

    return {
        "case_id": case_id,
        "action": "electricity_number_request_sent",
        "email_sent_to": taipower_review_email,
    }


def _get_supporting_document_attachments(case_id: str) -> list[dict]:
    """Download supporting documents from RAGIC and format as email attachments.

    Args:
        case_id: The RAGIC case record ID.

    Returns:
        List of attachment dicts with filename, content_base64, content_type.
    """
    attachments: list[dict] = []

    try:
        ragic_client = CloudRagicClient()
        try:
            documents = ragic_client.get_supporting_documents(case_id)
            for filename, file_bytes in documents:
                if file_bytes:
                    attachments.append({
                        "filename": filename,
                        "content_base64": base64.b64encode(file_bytes).decode("utf-8"),
                        "content_type": "application/pdf",
                    })
        finally:
            ragic_client.close()
    except Exception as e:
        log_operation(
            logger,
            case_id=case_id,
            operation_type="get_documents_error",
            message=f"Failed to download supporting documents: {e}",
            level="error",
        )

    return attachments


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
        log_operation(
            logger,
            case_id=case_id,
            operation_type="email_invoke_skip",
            message="EMAIL_SERVICE_FUNCTION_NAME not configured",
            level="warning",
        )
        return

    invoke_payload: dict[str, Any] = {
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
            InvocationType="Event",
            Payload=json.dumps(invoke_payload, ensure_ascii=False).encode("utf-8"),
        )
        log_operation(
            logger,
            case_id=case_id,
            operation_type="email_service_invoked",
            message=f"Email service invoked for {email_type.value}, StatusCode: {response['StatusCode']}",
        )
    except Exception as e:
        log_operation(
            logger,
            case_id=case_id,
            operation_type="email_invoke_error",
            message=f"Failed to invoke email service: {e}",
            level="error",
        )
