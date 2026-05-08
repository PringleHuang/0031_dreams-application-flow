"""Case closure flow handler.

Handles the 完成上線 → 已結案 transition: syncs site data to SunVeillance,
sends account activation notification, writes expiry info, and closes the case.

Requirements: 9.1, 9.2, 9.3, 9.4
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
SUNVEILLANCE_API_URL = os.environ.get("SUNVEILLANCE_API_URL", "")

_lambda_client = None


def _get_lambda_client():
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda")
    return _lambda_client


def handle_case_closure(case_id: str, payload: dict) -> dict:
    """Handle case closure flow (完成上線 → 已結案).

    Flow:
    1. Sync site data to SunVeillance
    2. Send account activation notification to customer
    3. Write expiry info to RAGIC
    4. Transition status to 已結案

    Args:
        case_id: The RAGIC case record ID.
        payload: Webhook payload or case data.

    Returns:
        Result dict.

    Requirements: 9.1, 9.2, 9.3, 9.4
    """
    log_operation(
        logger,
        case_id=case_id,
        operation_type="case_closure_start",
        message="Starting case closure flow",
    )

    # Step 1: Sync site data to SunVeillance
    site_data = _get_site_data(case_id, payload)
    sync_success = _sync_to_sunveillance(case_id, site_data)

    # Step 2: Send account activation notification
    customer_email = payload.get("customer_email", "")
    if not customer_email:
        customer_email = _get_customer_email(case_id)

    if customer_email:
        _invoke_email_service(
            case_id=case_id,
            email_type=EmailType.ACCOUNT_ACTIVATION,
            recipient_email=customer_email,
            template_data={
                "case_id": case_id,
                "customer_name": payload.get("customer_name", ""),
                "site_name": payload.get("site_name", ""),
            },
        )

    # Step 3: Write expiry info to RAGIC
    ragic_client = CloudRagicClient()
    try:
        ragic_client.update_case_record(case_id, {
            "closure_reason": "新約完成上線",
            "sunveillance_synced": "Y" if sync_success else "N",
        })

        # Step 4: Transition to 已結案
        transition_case_status(
            case_id=case_id,
            new_status=CaseStatus.CASE_CLOSED,
            reason="資料同步完成，案件結案",
            current_status=CaseStatus.ONLINE_COMPLETED,
            store=ragic_client,
        )
    finally:
        ragic_client.close()

    log_operation(
        logger,
        case_id=case_id,
        operation_type="case_closure_complete",
        message="Case closed successfully",
    )

    return {
        "case_id": case_id,
        "action": "case_closed",
        "sunveillance_synced": sync_success,
        "email_sent_to": customer_email,
        "new_status": CaseStatus.CASE_CLOSED.value,
    }


def _get_site_data(case_id: str, payload: dict) -> dict:
    """Get site data for SunVeillance sync.

    Args:
        case_id: Case record ID.
        payload: Webhook payload.

    Returns:
        Site data dict.
    """
    # Extract from payload or fetch from RAGIC
    site_data = {
        "case_id": case_id,
        "site_name": payload.get("site_name", ""),
        "site_address": payload.get("site_address", ""),
        "capacity_kw": payload.get("capacity_kw", ""),
        "electricity_number": payload.get("electricity_number", ""),
        "customer_name": payload.get("customer_name", ""),
    }

    # If key fields missing, fetch from RAGIC
    if not site_data["site_name"]:
        try:
            ragic_client = CloudRagicClient()
            try:
                record = ragic_client.get_questionnaire_data(case_id)
                site_data["site_name"] = record.get("1014670", "")
                site_data["site_address"] = record.get("1015399", "")
                site_data["capacity_kw"] = record.get("1015409", "")
                site_data["electricity_number"] = record.get("1015407", "")
            finally:
                ragic_client.close()
        except Exception as e:
            log_operation(
                logger,
                case_id=case_id,
                operation_type="get_site_data_error",
                message=f"Failed to get site data: {e}",
                level="warning",
            )

    return site_data


def _sync_to_sunveillance(case_id: str, site_data: dict) -> bool:
    """Sync site data to SunVeillance system.

    Args:
        case_id: Case record ID.
        site_data: Site data to sync.

    Returns:
        True if sync was successful.
    """
    if not SUNVEILLANCE_API_URL:
        log_operation(
            logger,
            case_id=case_id,
            operation_type="sunveillance_sync_skip",
            message="SUNVEILLANCE_API_URL not configured, skipping sync",
            level="warning",
        )
        return False

    try:
        import requests
        resp = requests.post(
            f"{SUNVEILLANCE_API_URL}/sites",
            json=site_data,
            timeout=30,
        )
        resp.raise_for_status()

        log_operation(
            logger,
            case_id=case_id,
            operation_type="sunveillance_sync_success",
            message="Site data synced to SunVeillance",
        )
        return True

    except Exception as e:
        log_operation(
            logger,
            case_id=case_id,
            operation_type="sunveillance_sync_error",
            message=f"Failed to sync to SunVeillance: {e}",
            level="error",
        )
        return False


def _get_customer_email(case_id: str) -> str:
    """Get customer email from RAGIC."""
    try:
        ragic_client = CloudRagicClient()
        try:
            record = ragic_client.get_questionnaire_data(case_id)
            return record.get("customer_email", record.get("email", ""))
        finally:
            ragic_client.close()
    except Exception:
        return ""


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
