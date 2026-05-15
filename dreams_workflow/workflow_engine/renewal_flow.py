"""Renewal case flow handler.

Handles the simplified renewal flow: provides SunVeillance login info,
waits for customer to select renewal site, writes back to RAGIC, and closes.

Requirements: 16.1, 16.2, 16.3, 16.4
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

_lambda_client = None


def _get_lambda_client():
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda")
    return _lambda_client


def handle_renewal(case_id: str, payload: dict) -> dict:
    """Handle the renewal case flow.

    Flow:
    1. Send SunVeillance login info to customer
    2. (Customer selects renewal site in SunVeillance — handled externally)
    3. Write renewal result back to RAGIC
    4. Transition status to 已結案

    This function handles step 1 (sending login info).
    Steps 3-4 are triggered when the renewal selection is complete.

    Args:
        case_id: The RAGIC case record ID.
        payload: Webhook payload.

    Returns:
        Result dict.

    Requirements: 16.1, 16.2
    """
    log_operation(
        logger,
        case_id=case_id,
        operation_type="renewal_flow_start",
        message="Starting renewal flow",
    )

    customer_email = payload.get("customer_email", "")
    if not customer_email:
        try:
            ragic_client = CloudRagicClient()
            try:
                record = ragic_client.get_questionnaire_data(case_id)
                customer_email = record.get("customer_email", record.get("email", ""))
            finally:
                ragic_client.close()
        except Exception:
            pass

    # Send SunVeillance login info
    if customer_email:
        _invoke_email_service(
            case_id=case_id,
            email_type=EmailType.ACCOUNT_ACTIVATION,
            recipient_email=customer_email,
            template_data={
                "case_id": case_id,
                "customer_name": payload.get("customer_name", ""),
                "is_renewal": True,
                "message": "請登入 SunVeillance 系統選擇續約案場",
            },
        )

    log_operation(
        logger,
        case_id=case_id,
        operation_type="renewal_login_sent",
        message="SunVeillance login info sent to customer",
    )

    return {
        "case_id": case_id,
        "action": "renewal_login_sent",
        "email_sent_to": customer_email,
        "next_step": "等待客戶在 SunVeillance 選擇續約案場",
    }


def handle_renewal_complete(case_id: str, payload: dict) -> dict:
    """Handle renewal completion (customer selected site).

    Called when customer completes renewal site selection in SunVeillance.

    Flow:
    1. Write renewal result (site_id) back to RAGIC
    2. Transition status to 已結案

    Args:
        case_id: The RAGIC case record ID.
        payload: Contains renewal_site_id from SunVeillance.

    Returns:
        Result dict.

    Requirements: 16.3, 16.4
    """
    renewal_site_id = payload.get("renewal_site_id", "")

    log_operation(
        logger,
        case_id=case_id,
        operation_type="renewal_complete_start",
        message=f"Processing renewal completion, site_id: {renewal_site_id}",
    )

    ragic_client = CloudRagicClient()
    try:
        # Write renewal result to RAGIC
        ragic_client.update_case_record(case_id, {
            "renewal_site_id": renewal_site_id,
            "closure_reason": "續約完成",
        })

        # Transition to 已結案
        transition_case_status(
            case_id=case_id,
            new_status=CaseStatus.CASE_CLOSED,
            reason="續約完成，案件結案",
            current_status=CaseStatus.PENDING_QUESTIONNAIRE,
            store=ragic_client,
        )
    finally:
        ragic_client.close()

    log_operation(
        logger,
        case_id=case_id,
        operation_type="renewal_complete",
        message="Renewal case closed",
    )

    return {
        "case_id": case_id,
        "action": "renewal_closed",
        "renewal_site_id": renewal_site_id,
        "new_status": CaseStatus.CASE_CLOSED.value,
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
