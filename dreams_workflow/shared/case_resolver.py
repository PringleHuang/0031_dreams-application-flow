"""Case context resolver for questionnaire and supplement webhooks.

When webhooks come from work-survey/7 (questionnaire) or work-survey/9 (supplement),
the payload does not contain case status. This module resolves the case context by:
1. Extracting DREAMS_APPLY_ID from the payload
2. Splitting by "-" to get the case ragicId
3. Querying the case management form (business-process2/2/{ragicId}) for status and type
"""

from __future__ import annotations

from dreams_workflow.shared.logger import get_logger, log_operation
from dreams_workflow.shared.ragic_fields_config import get_case_management_fields

logger = get_logger(__name__)


def resolve_ragic_id_from_payload(payload: dict) -> str | None:
    """Extract the case ragicId from a questionnaire/supplement webhook payload.

    The DREAMS_APPLY_ID field (configurable) has format: {shipment_order_id}-{ragicId}
    e.g. "TEST0011-17" → ragicId = "17"

    Args:
        payload: The webhook record_data (from data[0]).

    Returns:
        The ragicId string, or None if not found.
    """
    cm_fields = get_case_management_fields()
    dreams_apply_id_field = cm_fields.get("dreams_apply_id", "1016557")
    dreams_apply_id = payload.get(dreams_apply_id_field, payload.get("dreams_apply_id", ""))

    if not dreams_apply_id:
        return None

    # Split by "-" and take the last segment as ragicId
    parts = str(dreams_apply_id).split("-")
    if len(parts) >= 2:
        return parts[-1]

    # If no "-", the whole value might be the ragicId
    return dreams_apply_id


def resolve_case_context(payload: dict) -> dict:
    """Resolve full case context from a questionnaire/supplement webhook payload.

    Flow:
    1. Extract DREAMS_APPLY_ID from payload → split("-") → ragicId
    2. Query RAGIC API: GET business-process2/2/{ragicId}
    3. Return case status, type, and other relevant fields

    Args:
        payload: The webhook record_data.

    Returns:
        Dict with resolved case context:
        {
            "ragic_id": str,
            "case_status": str,       # e.g. "資訊補件", "台電補件"
            "dreams_apply_id": str,
            "customer_email": str,
            "shipment_order_id": str,
            "resolved": True/False,
            "error": str (if failed),
        }
    """
    ragic_id = resolve_ragic_id_from_payload(payload)

    if not ragic_id:
        log_operation(
            logger,
            case_id="unknown",
            operation_type="resolve_case_context_error",
            message="Cannot resolve ragicId: DREAMS_APPLY_ID not found in payload",
            level="error",
        )
        return {"resolved": False, "error": "DREAMS_APPLY_ID not found in payload"}

    log_operation(
        logger,
        case_id=ragic_id,
        operation_type="resolve_case_context",
        message=f"Resolving case context for ragicId={ragic_id}",
    )

    # Query case management form
    from dreams_workflow.shared.ragic_client import CloudRagicClient

    cm_fields = get_case_management_fields()
    status_field_id = cm_fields.get("case_status", "1015456")
    email_field_id = cm_fields.get("customer_email", "1016558")
    shipment_field_id = cm_fields.get("shipment_order_id", "1015021")
    dreams_apply_id_field = cm_fields.get("dreams_apply_id", "1016557")

    try:
        client = CloudRagicClient()
        try:
            record = client.get_case_record(ragic_id)
        finally:
            client.close()
    except Exception as e:
        log_operation(
            logger,
            case_id=ragic_id,
            operation_type="resolve_case_context_error",
            message=f"Failed to query case record: {e}",
            level="error",
        )
        return {"resolved": False, "ragic_id": ragic_id, "error": str(e)}

    case_status = record.get(status_field_id, "")
    customer_email = record.get(email_field_id, "")
    shipment_order_id = record.get(shipment_field_id, "")
    dreams_apply_id = record.get(dreams_apply_id_field, "")

    log_operation(
        logger,
        case_id=ragic_id,
        operation_type="resolve_case_context_complete",
        message=f"Case context resolved: status='{case_status}', dreams_apply_id='{dreams_apply_id}'",
    )

    return {
        "resolved": True,
        "ragic_id": ragic_id,
        "case_status": case_status,
        "customer_email": customer_email,
        "shipment_order_id": shipment_order_id,
        "dreams_apply_id": dreams_apply_id,
        "full_record": record,
    }
