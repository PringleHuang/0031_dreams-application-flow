"""Installation phase flow handler.

Handles the 安裝階段 stage: sends approval notification with self-check
checklist to the customer, executes DREAMS self-regulation check, and
proceeds to online procedure if passed.

Requirements: 8.1, 8.2, 8.3, 8.4, 8.5
"""

from __future__ import annotations

import json
import os
from typing import Any

import boto3

from dreams_workflow.shared.logger import get_logger, log_operation
from dreams_workflow.shared.models import CaseStatus, EmailType
from dreams_workflow.shared.ragic_client import CloudRagicClient
from dreams_workflow.shared.state_machine import transition_case_status

logger = get_logger(__name__)

EMAIL_SERVICE_FUNCTION = os.environ.get("EMAIL_SERVICE_FUNCTION_NAME", "")
DREAMS_API_URL = os.environ.get("DREAMS_API_URL", "")

_lambda_client = None


def _get_lambda_client():
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda")
    return _lambda_client


def handle_installation_phase(case_id: str, payload: dict) -> dict:
    """Handle the 安裝階段 flow.

    Flow:
    1. Send approval notification + self-check checklist email to customer
    2. (Await customer installation and contact — handled externally)
    3. Execute DREAMS self-regulation check (triggered separately)
    4. If passed → execute online procedure → status → 完成上線

    This function handles step 1 (sending the notification).
    Steps 3-4 will be triggered by a separate event when customer contacts support.

    Args:
        case_id: The RAGIC case record ID.
        payload: Webhook payload.

    Returns:
        Result dict.
    """
    log_operation(
        logger,
        case_id=case_id,
        operation_type="installation_phase_start",
        message="Starting installation phase flow",
    )

    # Get customer email
    customer_email = payload.get("customer_email", "")
    if not customer_email:
        try:
            ragic_client = CloudRagicClient()
            try:
                record = ragic_client.get_questionnaire_data(case_id)
                customer_email = record.get("customer_email", record.get("email", ""))
            finally:
                ragic_client.close()
        except Exception as e:
            log_operation(
                logger,
                case_id=case_id,
                operation_type="get_email_error",
                message=f"Failed to get customer email: {e}",
                level="error",
            )

    # Send approval notification with self-check checklist
    if customer_email:
        _invoke_email_service(
            case_id=case_id,
            email_type=EmailType.APPROVAL_NOTIFICATION,
            recipient_email=customer_email,
            template_data={
                "case_id": case_id,
                "customer_name": payload.get("customer_name", ""),
                "site_name": payload.get("site_name", ""),
            },
        )

    log_operation(
        logger,
        case_id=case_id,
        operation_type="installation_phase_notified",
        message="Approval notification sent, awaiting customer installation",
    )

    return {
        "case_id": case_id,
        "action": "installation_notification_sent",
        "email_sent_to": customer_email,
        "next_step": "等待客戶安裝資料收集器並聯繫客服",
    }


def handle_self_check(case_id: str, payload: dict) -> dict:
    """Execute DREAMS self-regulation check and proceed if passed.

    Called when customer contacts support after installation.

    Flow:
    1. Call DREAMS API for self-regulation check
    2. If passed → execute online procedure → transition to 完成上線
    3. If failed → notify customer of issues

    Args:
        case_id: The RAGIC case record ID.
        payload: Webhook payload.

    Returns:
        Result dict.
    """
    log_operation(
        logger,
        case_id=case_id,
        operation_type="self_check_start",
        message="Executing DREAMS self-regulation check",
    )

    # Call DREAMS API for self-check
    # Note: This uses the same DREAMS API endpoint with a different action
    import requests
    from dreams_workflow.shared.exceptions import DreamsConnectionError
    from dreams_workflow.shared.retry_config import retry_dreams

    if not DREAMS_API_URL:
        log_operation(
            logger,
            case_id=case_id,
            operation_type="self_check_skip",
            message="DREAMS_API_URL not configured, skipping self-check",
            level="warning",
        )
        return {"case_id": case_id, "action": "self_check_skipped", "reason": "API not configured"}

    try:
        resp = requests.post(
            f"{DREAMS_API_URL}/self-check",
            json={"case_id": case_id},
            timeout=120,
        )
        resp.raise_for_status()
        result = resp.json()
    except Exception as e:
        log_operation(
            logger,
            case_id=case_id,
            operation_type="self_check_error",
            message=f"Self-check API call failed: {e}",
            level="error",
        )
        return {"case_id": case_id, "action": "self_check_failed", "error": str(e)}

    passed = result.get("passed", False)
    issues = result.get("issues", [])

    if passed:
        # Execute online procedure
        return _execute_online_procedure(case_id, payload)
    else:
        # Notify customer of issues
        customer_email = payload.get("customer_email", "")
        if customer_email:
            _invoke_email_service(
                case_id=case_id,
                email_type=EmailType.APPROVAL_NOTIFICATION,
                recipient_email=customer_email,
                template_data={
                    "case_id": case_id,
                    "is_self_check_failed": True,
                    "issues": issues,
                },
            )

        log_operation(
            logger,
            case_id=case_id,
            operation_type="self_check_failed",
            message=f"Self-check failed: {issues}",
        )

        return {
            "case_id": case_id,
            "action": "self_check_failed",
            "issues": issues,
            "next_step": "等待客戶解決問題後重新執行自主檢查",
        }


def _execute_online_procedure(case_id: str, payload: dict) -> dict:
    """Execute DREAMS online procedure and transition to 完成上線.

    Args:
        case_id: The RAGIC case record ID.
        payload: Webhook payload.

    Returns:
        Result dict.
    """
    log_operation(
        logger,
        case_id=case_id,
        operation_type="online_procedure_start",
        message="Executing DREAMS online procedure",
    )

    # Call DREAMS API for online procedure
    if DREAMS_API_URL:
        try:
            import requests
            resp = requests.post(
                f"{DREAMS_API_URL}/go-online",
                json={"case_id": case_id},
                timeout=120,
            )
            resp.raise_for_status()
        except Exception as e:
            log_operation(
                logger,
                case_id=case_id,
                operation_type="online_procedure_error",
                message=f"Online procedure API call failed: {e}",
                level="error",
            )
            return {"case_id": case_id, "action": "online_procedure_failed", "error": str(e)}

    # Transition status to 完成上線
    ragic_client = CloudRagicClient()
    try:
        transition_case_status(
            case_id=case_id,
            new_status=CaseStatus.ONLINE_COMPLETED,
            reason="自主檢查通過，DREAMS 上線程序完成",
            current_status=CaseStatus.INSTALLATION_PHASE,
            store=ragic_client,
        )
    finally:
        ragic_client.close()

    log_operation(
        logger,
        case_id=case_id,
        operation_type="online_procedure_complete",
        message="Online procedure complete, status → 完成上線",
    )

    return {
        "case_id": case_id,
        "action": "online_procedure_complete",
        "new_status": CaseStatus.ONLINE_COMPLETED.value,
    }


def _invoke_email_service(
    case_id: str,
    email_type: EmailType,
    recipient_email: str,
    template_data: dict,
) -> None:
    """Invoke the email service Lambda."""
    if not EMAIL_SERVICE_FUNCTION:
        return

    invoke_payload = {
        "email_type": email_type.value,
        "case_id": case_id,
        "recipient_email": recipient_email,
        "template_data": template_data,
    }

    try:
        _get_lambda_client().invoke(
            FunctionName=EMAIL_SERVICE_FUNCTION,
            InvocationType="Event",
            Payload=json.dumps(invoke_payload, ensure_ascii=False).encode("utf-8"),
        )
    except Exception as e:
        log_operation(
            logger,
            case_id=case_id,
            operation_type="email_invoke_error",
            message=f"Failed to invoke email service: {e}",
            level="error",
        )
